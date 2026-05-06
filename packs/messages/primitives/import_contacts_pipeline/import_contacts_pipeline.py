#!/usr/bin/env python3
"""Resumable orchestrator for the messages import/contact enrichment pipeline.

The orchestrator is intentionally mechanical. It shells out to the existing
small primitives, records progress in `.powerpacks/messages/import-run.json`,
and exits at explicit approval gates for paid Parallel research and final
upload.

It does not infer approval from stdin. Agents should ask the user, then feed the
confirmation back with:

    python ... import_contacts_pipeline.py approve parallel --approval-id <id> --confirm
    python ... import_contacts_pipeline.py continue

Stdlib-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_LEDGER = Path(".powerpacks/messages/import-run.json")
DEFAULT_CONTACTS = Path(".powerpacks/messages/contacts.csv")
DEFAULT_CANDIDATES = Path(".powerpacks/messages/powerset_contacts.csv")
DEFAULT_RESEARCH_QUEUE = Path(".powerpacks/messages/research_queue.csv")
DEFAULT_RESEARCH_DIR = Path(".powerpacks/messages/research")
DEFAULT_REVIEW_CSV = Path(".powerpacks/messages/research_review.csv")
DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"
DEFAULT_PROCESSOR = "core2x"
DEFAULT_LLM_AUTO_APPROVE_USD = 1.0
DEFAULT_REVIEW_PORT = 8766


class PipelineBlocked(Exception):
    def __init__(self, payload: dict[str, Any], code: int = 20) -> None:
        super().__init__(payload.get("message") or payload.get("status") or "blocked")
        self.payload = payload
        self.code = code


class PipelineFailed(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(repo_root()))
    except ValueError:
        return str(path)


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_ledger(path: Path) -> dict[str, Any]:
    ledger = read_json(path, {}) or {}
    ledger.setdefault("primitive", "import_contacts_pipeline")
    ledger.setdefault("version", 1)
    ledger.setdefault("created_at", now_iso())
    ledger.setdefault("updated_at", now_iso())
    ledger.setdefault("steps", {})
    ledger.setdefault("approvals", {})
    ledger.setdefault("warnings", [])
    ledger.setdefault("artifacts", {})
    return ledger


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = now_iso()
    write_json(path, ledger)


def step_record(ledger: dict[str, Any], step_id: str) -> dict[str, Any]:
    steps = ledger.setdefault("steps", {})
    rec = steps.setdefault(step_id, {"id": step_id, "status": "pending"})
    return rec


def mark_step(
    ledger_path: Path,
    ledger: dict[str, Any],
    step_id: str,
    status: str,
    *,
    summary: dict[str, Any] | None = None,
    command: list[str] | None = None,
    error: str | None = None,
) -> None:
    rec = step_record(ledger, step_id)
    if rec.get("status") != "running" and status == "running":
        rec["started_at"] = now_iso()
    if status in {"completed", "skipped", "failed", "blocked_approval", "blocked_user_action", "warning"}:
        rec["finished_at"] = now_iso()
    rec["status"] = status
    if command is not None:
        rec["command"] = " ".join(shlex.quote(part) for part in command)
    if summary is not None:
        rec["summary"] = summary
    if error:
        rec["error"] = error
    save_ledger(ledger_path, ledger)


def completed(ledger: dict[str, Any], step_id: str) -> bool:
    return (ledger.get("steps") or {}).get(step_id, {}).get("status") == "completed"


def primitive_path(*parts: str) -> str:
    return str(repo_root().joinpath(*parts))


def parse_json_objects(text: str) -> list[Any]:
    out: list[Any] = []
    decoder = json.JSONDecoder()
    i = 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            nxt = text.find("{", i + 1)
            if nxt == -1:
                break
            i = nxt
            continue
        out.append(obj)
        i = end
    return out


def run_command(cmd: list[str], *, timeout: int = 300, env: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=repo_root(),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    json_objects = parse_json_objects(proc.stdout or "")
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "json_objects": json_objects,
        "json": json_objects[-1] if json_objects else None,
    }


def require_ok(result: dict[str, Any], step_id: str) -> dict[str, Any]:
    if result["returncode"] != 0:
        stderr = (result.get("stderr") or "").strip()
        stdout = (result.get("stdout") or "").strip()
        raise PipelineFailed(f"{step_id} failed rc={result['returncode']}: {(stderr or stdout)[-1000:]}")
    return result.get("json") or {}


def approval_id(kind: str, payload: dict[str, Any]) -> str:
    stable = json.dumps({"kind": kind, **payload}, sort_keys=True, default=str)
    digest = hashlib.sha1(stable.encode("utf-8")).hexdigest()[:12]
    return f"{kind}_{digest}"


def is_approved(ledger: dict[str, Any], approval_id_value: str) -> bool:
    return bool((ledger.get("approvals") or {}).get(approval_id_value, {}).get("confirmed"))


def approval_command(args: argparse.Namespace, kind: str, approval_id_value: str) -> str:
    return (
        f"python {rel(Path(__file__).resolve())} approve {kind} "
        f"--ledger {shlex.quote(str(args.ledger))} --approval-id {approval_id_value} --confirm && "
        f"python {rel(Path(__file__).resolve())} continue --ledger {shlex.quote(str(args.ledger))}"
    )


def block_for_approval(
    ledger_path: Path,
    ledger: dict[str, Any],
    args: argparse.Namespace,
    *,
    step_id: str,
    kind: str,
    payload: dict[str, Any],
    message: str,
) -> None:
    aid = approval_id(kind, payload)
    block = {
        "primitive": "import_contacts_pipeline",
        "status": "blocked_approval",
        "approval_id": aid,
        "approval_type": kind,
        "message": message,
        "payload": payload,
        "continue_command": approval_command(args, kind, aid),
        "ledger": str(ledger_path),
    }
    ledger["current_block"] = block
    mark_step(ledger_path, ledger, step_id, "blocked_approval", summary=block)
    raise PipelineBlocked(block)


def load_dotenv_values(path: Path, keys: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    wanted = set(keys)
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        key = key.strip()
        if key not in wanted or key in os.environ:
            continue
        value = raw.strip().strip('"').strip("'")
        if value:
            values[key] = value
    return values


def pipeline_env(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    env.update(load_dotenv_values(repo_root() / args.env_file, ["OPENROUTER_API_KEY", "PARALLEL_API_KEY"]))
    fallback = repo_root() / "../network-search-api/.env"
    env.update(load_dotenv_values(fallback.resolve(), ["OPENROUTER_API_KEY", "PARALLEL_API_KEY"]))
    return env


def read_manifest(path: Path) -> dict[str, Any] | None:
    data = read_json(path, None)
    return data if isinstance(data, dict) else None


def ensure_contacts(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    contacts = Path(args.contacts)
    if contacts.exists():
        ledger.setdefault("artifacts", {})["contacts_csv"] = str(contacts)
        mark_step(ledger_path, ledger, "ensure_contacts", "completed", summary={"contacts": str(contacts)})
        return
    block = {
        "primitive": "import_contacts_pipeline",
        "status": "blocked_user_action",
        "message": f"contacts CSV not found: {contacts}. Run import-imessage/import-whatsapp first, then continue.",
        "ledger": str(ledger_path),
    }
    ledger["current_block"] = block
    mark_step(ledger_path, ledger, "ensure_contacts", "blocked_user_action", summary=block)
    raise PipelineBlocked(block, code=21)


def sync_candidates(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    if completed(ledger, "sync_powerset_candidates") and not args.force_sync_candidates:
        return
    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/sync_powerset_candidates/sync_powerset_candidates.py"),
        "sync",
        "--output", str(args.candidates),
    ]
    mark_step(ledger_path, ledger, "sync_powerset_candidates", "running", command=cmd)
    result = run_command(cmd, timeout=args.timeout, env=pipeline_env(args))
    payload = require_ok(result, "sync_powerset_candidates")
    ledger.setdefault("artifacts", {})["powerset_contacts_csv"] = str(args.candidates)
    mark_step(ledger_path, ledger, "sync_powerset_candidates", "completed", summary=payload, command=cmd)


def match_contacts(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    if completed(ledger, "match_local_contacts") and not args.force_match:
        return
    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/match_local_candidates/match_local_candidates.py"),
        "match",
        "--contacts", str(args.contacts),
        "--candidates", str(args.candidates),
    ]
    mark_step(ledger_path, ledger, "match_local_contacts", "running", command=cmd)
    result = run_command(cmd, timeout=args.timeout, env=pipeline_env(args))
    payload = require_ok(result, "match_local_contacts")
    mark_step(ledger_path, ledger, "match_local_contacts", "completed", summary=payload, command=cmd)


def llm_manifest_path(contacts: Path) -> Path:
    results = contacts.with_suffix(contacts.suffix + ".llm_review.jsonl")
    return results.with_suffix(results.suffix + ".manifest.json")


def llm_review(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    manifest_path = llm_manifest_path(Path(args.contacts))
    manifest = read_manifest(manifest_path)
    if manifest and manifest.get("status") in {"completed", "completed_with_errors"} and not args.rerun_llm:
        mark_step(ledger_path, ledger, "llm_review", "completed", summary={"reused_manifest": str(manifest_path), **manifest})
        return

    estimate_cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/llm_review_contacts/llm_review_contacts.py"),
        "estimate",
        "--input", str(args.contacts),
        "--model", args.model,
    ]
    mark_step(ledger_path, ledger, "llm_estimate", "running", command=estimate_cmd)
    estimate_result = run_command(estimate_cmd, timeout=args.timeout, env=pipeline_env(args))
    estimate_payload = require_ok(estimate_result, "llm_estimate")
    mark_step(ledger_path, ledger, "llm_estimate", "completed", summary=estimate_payload, command=estimate_cmd)

    candidates = int(estimate_payload.get("candidates") or 0)
    estimated_usd = float((estimate_payload.get("estimate") or {}).get("estimated_usd") or 0.0)
    if candidates <= 0:
        mark_step(ledger_path, ledger, "llm_review", "skipped", summary={"reason": "no_candidates"})
        return

    auto_ok = args.model == DEFAULT_MODEL and estimated_usd < float(args.llm_auto_approve_usd)
    payload = {"candidates": candidates, "estimated_usd": estimated_usd, "model": args.model}
    aid = approval_id("llm", payload)
    if not auto_ok and not is_approved(ledger, aid):
        block_for_approval(
            ledger_path,
            ledger,
            args,
            step_id="llm_review",
            kind="llm",
            payload=payload,
            message=f"Run LLM review on {candidates} contacts with {args.model}? Estimated cost: ${estimated_usd:.4f}.",
        )

    review_cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/llm_review_contacts/llm_review_contacts.py"),
        "review",
        "--input", str(args.contacts),
        "--model", args.model,
    ]
    mark_step(ledger_path, ledger, "llm_review", "running", command=review_cmd)
    review_result = run_command(review_cmd, timeout=max(args.timeout, 600), env=pipeline_env(args))
    review_payload = require_ok(review_result, "llm_review")
    if auto_ok:
        review_payload["auto_approved_reason"] = f"{DEFAULT_MODEL}_under_{args.llm_auto_approve_usd}_usd"
    mark_step(ledger_path, ledger, "llm_review", "completed", summary=review_payload, command=review_cmd)


def prepare_queue(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    if completed(ledger, "prepare_research_queue") and not args.force_prepare_queue:
        return
    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/prepare_research_queue/prepare_research_queue.py"),
        "prepare",
        "--input", str(args.contacts),
        "--output", str(args.research_queue),
    ]
    mark_step(ledger_path, ledger, "prepare_research_queue", "running", command=cmd)
    result = run_command(cmd, timeout=args.timeout, env=pipeline_env(args))
    payload = require_ok(result, "prepare_research_queue")
    ledger.setdefault("artifacts", {})["research_queue_csv"] = str(args.research_queue)
    mark_step(ledger_path, ledger, "prepare_research_queue", "completed", summary=payload, command=cmd)


def sync_research_cache(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    if completed(ledger, "sync_messages_research_cache") and not args.force_sync_cache:
        return
    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/sync_messages_research_cache/sync_messages_research_cache.py"),
        "download",
        "--profiles-dir", str(args.research_dir),
    ]
    mark_step(ledger_path, ledger, "sync_messages_research_cache", "running", command=cmd)
    result = run_command(cmd, timeout=max(args.timeout, 600), env=pipeline_env(args))
    payload = result.get("json") or {
        "status": "failed",
        "stdout_tail": (result.get("stdout") or "")[-1000:],
        "stderr_tail": (result.get("stderr") or "")[-1000:],
    }
    if result["returncode"] != 0:
        warning = {
            "step": "sync_messages_research_cache",
            "status": "warning",
            "message": "research cache sync failed; continuing optimistically with local cache",
            "detail": payload,
            "recorded_at": now_iso(),
        }
        ledger.setdefault("warnings", []).append(warning)
        mark_step(ledger_path, ledger, "sync_messages_research_cache", "completed", summary=warning, command=cmd)
        return
    mark_step(ledger_path, ledger, "sync_messages_research_cache", "completed", summary=payload, command=cmd)


def parallel_research(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    if completed(ledger, "deep_research") and not args.rerun_parallel:
        return
    estimate_cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/deep_research_contacts/deep_research_contacts.py"),
        "estimate",
        "--input", str(args.research_queue),
        "--processor", args.processor,
        "--output-dir", str(args.research_dir),
    ]
    mark_step(ledger_path, ledger, "parallel_estimate", "running", command=estimate_cmd)
    estimate_result = run_command(estimate_cmd, timeout=args.timeout, env=pipeline_env(args))
    estimate_payload = require_ok(estimate_result, "parallel_estimate")
    mark_step(ledger_path, ledger, "parallel_estimate", "completed", summary=estimate_payload, command=estimate_cmd)

    would_submit = int(estimate_payload.get("would_submit") or 0)
    estimated_usd = float(estimate_payload.get("estimated_usd") or 0.0)
    if would_submit <= 0:
        mark_step(ledger_path, ledger, "deep_research", "completed", summary={"status": "no_work", **estimate_payload})
        return

    payload = {"would_submit": would_submit, "estimated_usd": estimated_usd, "processor": args.processor, "input": str(args.research_queue)}
    aid = approval_id("parallel", payload)
    if not is_approved(ledger, aid):
        block_for_approval(
            ledger_path,
            ledger,
            args,
            step_id="deep_research",
            kind="parallel",
            payload=payload,
            message=f"Run Parallel deep research on {would_submit} people with processor {args.processor}? Estimated cost: ${estimated_usd:.4f}.",
        )

    run_cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/deep_research_contacts/deep_research_contacts.py"),
        "run",
        "--input", str(args.research_queue),
        "--processor", args.processor,
        "--output-dir", str(args.research_dir),
    ]
    mark_step(ledger_path, ledger, "deep_research", "running", command=run_cmd)
    run_result = run_command(run_cmd, timeout=args.parallel_timeout, env=pipeline_env(args))
    # deep_research_contacts emits submit + poll JSON objects; final object is poll summary.
    payloads = run_result.get("json_objects") or []
    final_payload = payloads[-1] if payloads else None
    if run_result["returncode"] != 0:
        raise PipelineFailed(f"deep_research failed rc={run_result['returncode']}: {((run_result.get('stderr') or run_result.get('stdout') or '').strip())[-1000:]}")
    mark_step(ledger_path, ledger, "deep_research", "completed", summary={"outputs": payloads, "final": final_payload}, command=run_cmd)


def build_review_csv(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    if completed(ledger, "build_research_review_csv") and not args.force_build_review:
        return
    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/build_research_review_csv/build_research_review_csv.py"),
        "build",
        "--research-dir", str(args.research_dir),
        "--queue-csv", str(args.research_queue),
        "--output-csv", str(args.review_csv),
    ]
    mark_step(ledger_path, ledger, "build_research_review_csv", "running", command=cmd)
    result = run_command(cmd, timeout=args.timeout, env=pipeline_env(args))
    payload = require_ok(result, "build_research_review_csv")
    ledger.setdefault("artifacts", {})["research_review_csv"] = str(args.review_csv)
    mark_step(ledger_path, ledger, "build_research_review_csv", "completed", summary=payload, command=cmd)


def review_url(args: argparse.Namespace) -> str:
    query = urllib.parse.urlencode({"tab": "yes"})
    return f"http://{args.review_host}:{args.review_port}/?{query}"


def open_review_server(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    if args.no_open_review:
        mark_step(ledger_path, ledger, "review_research_web", "skipped", summary={"reason": "--no-open-review"})
        return
    server_dir = repo_root() / ".powerpacks/servers"
    server_dir.mkdir(parents=True, exist_ok=True)
    pid_path = server_dir / "research-review-http.pid"
    log_path = server_dir / "research-review-http.log"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            mark_step(ledger_path, ledger, "review_research_web", "completed", summary={"url": review_url(args), "pid": pid, "reused": True})
            return
        except Exception:
            pid_path.unlink(missing_ok=True)

    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/review_research_web/review_research_web.py"),
        "serve",
        "--csv", str(args.review_csv),
        "--research-dir", str(args.research_dir),
        "--host", args.review_host,
        "--port", str(args.review_port),
    ]
    if args.open_browser:
        cmd.append("--open")
    mark_step(ledger_path, ledger, "review_research_web", "running", command=cmd)
    with log_path.open("ab") as log:
        proc = subprocess.Popen(cmd, cwd=repo_root(), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    pid_path.write_text(str(proc.pid) + "\n", encoding="utf-8")
    time.sleep(1)
    mark_step(ledger_path, ledger, "review_research_web", "completed", summary={"url": review_url(args), "pid": proc.pid, "log": str(log_path)}, command=cmd)


def summarize_upload(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/upload_research_review/upload_research_review.py"),
        "summarize",
        "--csv", str(args.review_csv),
    ]
    mark_step(ledger_path, ledger, "summarize_upload", "running", command=cmd)
    result = run_command(cmd, timeout=args.timeout, env=pipeline_env(args))
    payload = require_ok(result, "summarize_upload")
    mark_step(ledger_path, ledger, "summarize_upload", "completed", summary=payload, command=cmd)
    return payload


def upload_review(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    if completed(ledger, "upload_research_review") and not args.rerun_upload:
        return
    summary = summarize_upload(args, ledger_path, ledger)
    payload = {
        "row_count": int(summary.get("row_count") or 0),
        "yes_count": int(summary.get("yes_count") or 0),
        "maybe_count": int(summary.get("maybe_count") or 0),
        "no_count": int(summary.get("no_count") or 0),
        "csv": str(args.review_csv),
    }
    aid = approval_id("upload", payload)
    if not is_approved(ledger, aid):
        block_for_approval(
            ledger_path,
            ledger,
            args,
            step_id="upload_research_review",
            kind="upload",
            payload=payload,
            message=(
                "Upload reviewed messages research artifact to Powerset? "
                f"Rows: {payload['row_count']} (yes={payload['yes_count']}, maybe={payload['maybe_count']}, no={payload['no_count']})."
            ),
        )

    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/upload_research_review/upload_research_review.py"),
        "upload",
        "--csv", str(args.review_csv),
        "--confirm-upload",
    ]
    mark_step(ledger_path, ledger, "upload_research_review", "running", command=cmd)
    result = run_command(cmd, timeout=args.timeout, env=pipeline_env(args))
    upload_payload = require_ok(result, "upload_research_review")
    ledger.setdefault("artifacts", {})["uploaded_artifact_id"] = ((upload_payload.get("response") or {}).get("artifact_id"))
    mark_step(ledger_path, ledger, "upload_research_review", "completed", summary=upload_payload, command=cmd)


def hydrate_args_from_ledger(args: argparse.Namespace, ledger: dict[str, Any]) -> None:
    """Use persisted paths/model on resume unless the caller supplied overrides.

    This keeps approval `continue_command`s short (`--ledger ...` only) while
    still preserving custom artifact paths across exits.
    """
    cfg = ledger.get("config") or {}
    path_defaults = {
        "contacts": DEFAULT_CONTACTS,
        "candidates": DEFAULT_CANDIDATES,
        "research_queue": DEFAULT_RESEARCH_QUEUE,
        "research_dir": DEFAULT_RESEARCH_DIR,
        "review_csv": DEFAULT_REVIEW_CSV,
    }
    for attr, default in path_defaults.items():
        if cfg.get(attr) and Path(getattr(args, attr)) == default:
            setattr(args, attr, Path(cfg[attr]))
    if cfg.get("model") and args.model == DEFAULT_MODEL:
        args.model = cfg["model"]
    if cfg.get("processor") and args.processor == DEFAULT_PROCESSOR:
        args.processor = cfg["processor"]


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    ledger_path = Path(args.ledger)
    ledger = load_ledger(ledger_path)
    hydrate_args_from_ledger(args, ledger)
    ledger["current_block"] = None
    ledger["config"] = {
        "contacts": str(args.contacts),
        "candidates": str(args.candidates),
        "research_queue": str(args.research_queue),
        "research_dir": str(args.research_dir),
        "review_csv": str(args.review_csv),
        "model": args.model,
        "processor": args.processor,
    }
    save_ledger(ledger_path, ledger)

    ensure_contacts(args, ledger_path, ledger)
    sync_candidates(args, ledger_path, ledger)
    match_contacts(args, ledger_path, ledger)
    llm_review(args, ledger_path, ledger)
    prepare_queue(args, ledger_path, ledger)
    sync_research_cache(args, ledger_path, ledger)
    parallel_research(args, ledger_path, ledger)
    build_review_csv(args, ledger_path, ledger)
    open_review_server(args, ledger_path, ledger)
    if args.stop_before_upload:
        payload = {
            "primitive": "import_contacts_pipeline",
            "status": "blocked_user_action",
            "message": f"Review the web UI at {review_url(args)}. Re-run without --stop-before-upload when ready to summarize/upload.",
            "review_url": review_url(args),
            "ledger": str(ledger_path),
        }
        ledger["current_block"] = payload
        save_ledger(ledger_path, ledger)
        raise PipelineBlocked(payload, code=21)
    upload_review(args, ledger_path, ledger)

    payload = {
        "primitive": "import_contacts_pipeline",
        "status": "completed",
        "ledger": str(ledger_path),
        "artifacts": ledger.get("artifacts", {}),
        "warnings": ledger.get("warnings", []),
    }
    save_ledger(ledger_path, ledger)
    return payload


def cmd_run(args: argparse.Namespace) -> int:
    try:
        payload = run_pipeline(args)
        emit(payload)
        return 0
    except PipelineBlocked as exc:
        emit(exc.payload)
        return exc.code
    except (PipelineFailed, subprocess.TimeoutExpired) as exc:
        ledger = load_ledger(Path(args.ledger))
        ledger["last_error"] = str(exc)
        save_ledger(Path(args.ledger), ledger)
        emit({"primitive": "import_contacts_pipeline", "status": "failed", "error": str(exc), "ledger": str(args.ledger)})
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    ledger = load_ledger(Path(args.ledger))
    steps = ledger.get("steps") or {}
    counts: dict[str, int] = {}
    for rec in steps.values():
        counts[rec.get("status", "unknown")] = counts.get(rec.get("status", "unknown"), 0) + 1
    emit({
        "primitive": "import_contacts_pipeline",
        "command": "status",
        "status": "ok",
        "ledger": str(args.ledger),
        "step_counts": counts,
        "current_block": ledger.get("current_block"),
        "artifacts": ledger.get("artifacts", {}),
        "warnings": ledger.get("warnings", []),
    })
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    if not args.confirm:
        emit({
            "primitive": "import_contacts_pipeline",
            "command": "approve",
            "status": "blocked",
            "error": "pass --confirm after the user explicitly approves this gate",
        })
        return 2
    ledger_path = Path(args.ledger)
    ledger = load_ledger(ledger_path)
    current = ledger.get("current_block") or {}
    aid = args.approval_id or current.get("approval_id")
    if not aid:
        emit({"primitive": "import_contacts_pipeline", "command": "approve", "status": "failed", "error": "no approval_id provided and no current block"})
        return 1
    if args.kind not in aid and current.get("approval_type") and current.get("approval_type") != args.kind:
        emit({"primitive": "import_contacts_pipeline", "command": "approve", "status": "failed", "error": f"approval type mismatch: requested {args.kind}, current {current.get('approval_type')}"})
        return 1
    ledger.setdefault("approvals", {})[aid] = {
        "approval_id": aid,
        "type": args.kind,
        "confirmed": True,
        "approved_at": now_iso(),
        "approved_by": "user_confirmed_in_agent_chat",
        "payload": current.get("payload") if current.get("approval_id") == aid else {},
    }
    if current.get("approval_id") == aid:
        ledger["current_block"] = None
    save_ledger(ledger_path, ledger)
    emit({"primitive": "import_contacts_pipeline", "command": "approve", "status": "ok", "approval_id": aid, "type": args.kind, "ledger": str(ledger_path)})
    return 0


def add_pipeline_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--contacts", type=Path, default=DEFAULT_CONTACTS)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--research-queue", type=Path, default=DEFAULT_RESEARCH_QUEUE)
    parser.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH_DIR)
    parser.add_argument("--review-csv", type=Path, default=DEFAULT_REVIEW_CSV)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--processor", default=DEFAULT_PROCESSOR)
    parser.add_argument("--llm-auto-approve-usd", type=float, default=DEFAULT_LLM_AUTO_APPROVE_USD)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--parallel-timeout", type=int, default=7600)
    parser.add_argument("--review-host", default="127.0.0.1")
    parser.add_argument("--review-port", type=int, default=DEFAULT_REVIEW_PORT)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--no-open-review", action="store_true")
    parser.add_argument("--stop-before-upload", action="store_true", help="Stop after opening review UI instead of summarizing/blocking upload")
    parser.add_argument("--force-sync-candidates", action="store_true")
    parser.add_argument("--force-match", action="store_true")
    parser.add_argument("--force-prepare-queue", action="store_true")
    parser.add_argument("--force-sync-cache", action="store_true")
    parser.add_argument("--force-build-review", action="store_true")
    parser.add_argument("--rerun-llm", action="store_true")
    parser.add_argument("--rerun-parallel", action="store_true")
    parser.add_argument("--rerun-upload", action="store_true")


def main() -> int:
    parser = argparse.ArgumentParser(description="Resumable import contacts pipeline orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run until completed or blocked on approval/user action")
    add_pipeline_args(run)
    run.set_defaults(func=cmd_run)

    cont = sub.add_parser("continue", help="Resume from the ledger")
    add_pipeline_args(cont)
    cont.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="Show ledger status")
    status.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    status.set_defaults(func=cmd_status)

    approve = sub.add_parser("approve", help="Record explicit user approval for a blocked gate")
    approve.add_argument("kind", choices=["llm", "parallel", "upload"])
    approve.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    approve.add_argument("--approval-id")
    approve.add_argument("--confirm", action="store_true")
    approve.set_defaults(func=cmd_approve)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
