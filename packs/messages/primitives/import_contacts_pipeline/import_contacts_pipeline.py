#!/usr/bin/env python3
"""Resumable orchestrator for the messages import/contact enrichment pipeline.

The orchestrator is intentionally mechanical. It shells out to the existing
small primitives, records progress in `.powerpacks/messages/import-run.json`,
and exits at explicit approval gates for paid Parallel research and final
upload.

It does not infer approval from stdin. Agents should ask the user, then feed the
confirmation back with:

    uv run --project . python ... import_contacts_pipeline.py approve
    uv run --project . python ... import_contacts_pipeline.py continue

Stdlib-only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_LEDGER = Path(".powerpacks/messages/import-run.json")
DEFAULT_CONTACTS = Path(".powerpacks/messages/contacts.csv")
DEFAULT_IMESSAGE_CONTACTS = Path(".powerpacks/messages/imessage.contacts.csv")
DEFAULT_IMESSAGE_JSONL = Path(".powerpacks/messages/imessage.contacts.raw.jsonl")
DEFAULT_IMESSAGE_NORMALIZED = Path(".powerpacks/messages/imessage.contacts.normalized.jsonl")
DEFAULT_IMESSAGE_MANIFEST = Path(".powerpacks/messages/imessage.manifest.json")
DEFAULT_IMESSAGE_NORMALIZED_MANIFEST = Path(".powerpacks/messages/imessage.contacts.normalized.jsonl.manifest.json")
DEFAULT_WHATSAPP_CONTACTS = Path(".powerpacks/messages/whatsapp.contacts.csv")
DEFAULT_WHATSAPP_JSONL = Path(".powerpacks/messages/whatsapp.contacts.raw.jsonl")
DEFAULT_WHATSAPP_NORMALIZED = Path(".powerpacks/messages/whatsapp.contacts.normalized.jsonl")
DEFAULT_WHATSAPP_MANIFEST = Path(".powerpacks/messages/whatsapp.contacts.csv.manifest.json")
DEFAULT_WHATSAPP_NORMALIZED_MANIFEST = Path(".powerpacks/messages/whatsapp.contacts.normalized.jsonl.manifest.json")
DEFAULT_WHATSAPP_MESSAGE_COUNT_CACHE = Path(".powerpacks/messages/whatsapp.message-count-cache.json")
DEFAULT_CANDIDATES = Path(".powerpacks/messages/powerset_contacts.csv")
DEFAULT_RESEARCH_QUEUE = Path(".powerpacks/messages/research_queue.csv")
DEFAULT_RESEARCH_DIR = Path(".powerpacks/messages/research")
DEFAULT_REVIEW_CSV = Path(".powerpacks/messages/research_review.csv")
DEFAULT_RETARGET_QUEUE = Path(".powerpacks/messages/retarget_queue.csv")
DEFAULT_RETARGET_LEDGER = Path(".powerpacks/messages/retarget_attempts.json")
DEFAULT_RETARGET_RESEARCH_DIR = Path(".powerpacks/messages/research_retarget")
DEFAULT_RETARGET_HARNESS_PROMPT_DIR = Path(".powerpacks/messages/retarget_harness_prompts")
DEFAULT_RETARGET_HARNESS_THRESHOLD = 100
DEFAULT_RETARGET_HARNESS_MAX_WORKERS = 10
DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"
DEFAULT_NETWORK_REVIEW_MODEL = "openai/gpt-4.1"
DEFAULT_PROCESSOR = "core2x"
ALLOWED_PARALLEL_PROCESSORS = ("core", "core2x", "pro")
PARALLEL_LATENCY = {
    "core": {
        "per_task": "60s-5min",
        "rough_wall_clock": "about 1-5 min once submitted",
    },
    "core2x": {
        "per_task": "60s-10min",
        "rough_wall_clock": "about 10-15 min once submitted",
    },
    "pro": {
        "per_task": "2-10min",
        "rough_wall_clock": "about 2-10 min once submitted",
    },
}
DEFAULT_LLM_AUTO_APPROVE_USD = 10.0
NETWORK_REVIEW_TOKEN_ESTIMATE = {"input": 600, "output": 80}
DEFAULT_REVIEW_PORT = 8766
ARCHIVE_ROOT = Path(".powerpacks/messages/archive")
CONTACT_CSV_HEADERS = [
    "phone",
    "name",
    "source",
    "is_in_group_chats",
    "group_names",
    "message_count",
    "imessage_message_count",
    "whatsapp_message_count",
    "last_message",
    "imessage_last_message",
    "whatsapp_last_message",
    "skip",
    "match_status",
    "matched_person_id",
    "matched_name",
    "matched_linkedin_url",
    "match_confidence",
    "match_method",
    "match_reason",
]
REQUIRED_CONTACT_HEADERS = {"phone", "name"}
CONTACT_SCHEMA_DOC = "packs/messages/schemas/contacts-csv.md"
CONTACT_SCHEMA_JSON = "packs/messages/schemas/contacts-csv.schema.json"
REVIEW_CSV_HEADERS = [
    "bucket",
    "handle",
    "full_name",
    "phone_e164",
    "area_code",
    "total_messages",
    "imessage_message_count",
    "whatsapp_message_count",
    "message_source",
    "last_message",
    "imessage_last_message",
    "whatsapp_last_message",
    "group_names",
    "location_city",
    "location_country",
    "top_titles",
    "top_companies",
    "top_title_company_pairs",
    "schools",
    "short_reason",
    "identity_risk",
    "signals",
    "retarget_hint",
    "exclude",
    "enrich_decision",
    "in_network",
    "network_match_status",
    "network_person_id",
    "network_name",
    "network_linkedin_url",
    "network_match_confidence",
    "network_match_method",
    "network_match_reason",
    "review_source",
]


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


def contact_schema_error(path: Path, fieldnames: list[str] | None) -> str:
    fields = ",".join(fieldnames or []) or "<none>"
    header = ",".join(CONTACT_CSV_HEADERS)
    return (
        f"CSV schema mismatch for {path}. Please convert this file into the Powerpacks messages contacts CSV schema before retrying. "
        f"Required input columns: phone,name. Canonical header: {header}. "
        f"Detected columns: {fields}. Schema docs: {CONTACT_SCHEMA_DOC}. JSON schema: {CONTACT_SCHEMA_JSON}. "
        "Common legacy mappings: phone_e164/phone_number -> phone; display_name/full_name -> name; "
        "total_messages -> message_count; imessage_count/imessage_messages -> imessage_message_count; "
        "whatsapp_count/whatsapp_messages -> whatsapp_message_count; message_source/source_channel -> source."
    )


def read_csv_fieldnames(path: Path) -> list[str] | None:
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return csv.DictReader(handle).fieldnames
    except OSError:
        return None


def validate_contacts_csv(path: Path) -> None:
    fieldnames = read_csv_fieldnames(path)
    names = {str(value or "").strip() for value in (fieldnames or [])}
    if not REQUIRED_CONTACT_HEADERS.issubset(names):
        raise PipelineFailed(contact_schema_error(path, fieldnames))


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


WHATSAPP_STEP_MESSAGES = {
    "check_docker_and_waha": {
        "running": "Getting WhatsApp sync ready.",
        "completed": "WhatsApp sync is ready.",
        "blocked_user_action": "WhatsApp sync needs the local helper app running before it can continue.",
    },
    "start_waha_container": {
        "running": "Starting WhatsApp sync.",
        "completed": "WhatsApp sync started.",
    },
    "authenticate_whatsapp": {
        "running": "Connecting WhatsApp.",
        "completed": "WhatsApp is connected.",
        "blocked_user_action": "WhatsApp needs you to scan the QR, then continue the import.",
    },
    "extract_whatsapp": {
        "running": "We're syncing WhatsApp. WhatsApp is taking a bit longer when there are many chats.",
        "completed": "WhatsApp sync finished.",
    },
    "normalize_whatsapp": {
        "running": "Preparing synced WhatsApp contacts.",
        "completed": "WhatsApp contacts are ready.",
    },
}


def user_message_for_step(step_id: str, status: str) -> str | None:
    return WHATSAPP_STEP_MESSAGES.get(step_id, {}).get(status)


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
    user_message = user_message_for_step(step_id, status)
    if user_message:
        rec["user_message"] = user_message
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


def _read_stream(pipe: Any, chunks: list[str], *, forward_to_stderr: bool = False) -> None:
    try:
        for line in iter(pipe.readline, ""):
            chunks.append(line)
            if forward_to_stderr:
                print(line, end="", file=sys.stderr, flush=True)
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def run_command(cmd: list[str], *, timeout: int = 300, env: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=repo_root(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_thread = threading.Thread(target=_read_stream, args=(proc.stdout, stdout_chunks), daemon=True)
    stderr_thread = threading.Thread(
        target=_read_stream,
        args=(proc.stderr, stderr_chunks),
        kwargs={"forward_to_stderr": True},
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        returncode = proc.wait()
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        raise subprocess.TimeoutExpired(
            cmd,
            timeout,
            output="".join(stdout_chunks),
            stderr="".join(stderr_chunks),
        ) from exc
    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    json_objects = parse_json_objects(stdout or "")
    return {
        "cmd": cmd,
        "returncode": returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stdout": stdout,
        "stderr": stderr,
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


def parallel_latency_summary(processor: str, estimate_payload: dict[str, Any]) -> dict[str, Any]:
    latency = estimate_payload.get("estimated_latency")
    if isinstance(latency, dict):
        return latency
    fallback = PARALLEL_LATENCY.get(processor) or PARALLEL_LATENCY[DEFAULT_PROCESSOR]
    return {"processor": processor, **fallback}


def parallel_approval_message(estimated_usd: float, latency: dict[str, Any]) -> str:
    rough = latency.get("rough_wall_clock") or "roughly a few minutes once submitted"
    return f"Estimated deep research cost: ${estimated_usd:.4f}, completion time is {rough}. Approve?"


def retarget_approval_message() -> str:
    return "Feedback found; approve another re-research pass? Completion time is up to 10-15 min."


def approval_command(args: argparse.Namespace, kind: str, approval_id_value: str) -> str:
    return (
        f"uv run --project . python {rel(Path(__file__).resolve())} approve && "
        f"uv run --project . python {rel(Path(__file__).resolve())} continue"
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
    provider_keys = ["OPENROUTER_API_KEY", "OPENAI_API_KEY", "PARALLEL_API_KEY", "RAPIDAPI_LINKEDIN_KEY"]
    env.update(load_dotenv_values(repo_root() / args.env_file, provider_keys))
    return env


def read_manifest(path: Path) -> dict[str, Any] | None:
    data = read_json(path, None)
    return data if isinstance(data, dict) else None


def ensure_artifact_dirs(args: argparse.Namespace) -> None:
    for path in (
        Path(args.ledger),
        Path(args.contacts),
        Path(args.candidates),
        Path(args.research_queue),
        Path(args.review_csv),
        Path(args.retarget_queue),
        Path(args.retarget_ledger),
        DEFAULT_IMESSAGE_CONTACTS,
        DEFAULT_IMESSAGE_JSONL,
        DEFAULT_IMESSAGE_NORMALIZED,
        DEFAULT_WHATSAPP_CONTACTS,
        DEFAULT_WHATSAPP_JSONL,
        DEFAULT_WHATSAPP_NORMALIZED,
        DEFAULT_WHATSAPP_MESSAGE_COUNT_CACHE,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
    Path(args.research_dir).mkdir(parents=True, exist_ok=True)
    Path(args.retarget_research_dir).mkdir(parents=True, exist_ok=True)


def sidecar_manifest(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".manifest.json")


def fresh_run_artifact_paths(args: argparse.Namespace) -> list[Path]:
    """Artifacts owned by a single import run.

    Research profile directories are intentionally excluded: deep research is
    cache-addressed by handle and expensive to rebuild. The WhatsApp
    message-count cache is also excluded so fresh runs can rescan WAHA live
    without recounting unchanged chats.
    """
    base_paths = [
        Path(args.ledger),
        Path(args.contacts),
        Path(args.candidates),
        Path(args.research_queue),
        Path(args.review_csv),
        Path(args.retarget_queue),
        Path(args.retarget_ledger),
        DEFAULT_IMESSAGE_CONTACTS,
        DEFAULT_IMESSAGE_JSONL,
        DEFAULT_IMESSAGE_MANIFEST,
        DEFAULT_IMESSAGE_NORMALIZED,
        DEFAULT_IMESSAGE_NORMALIZED_MANIFEST,
        DEFAULT_WHATSAPP_CONTACTS,
        DEFAULT_WHATSAPP_JSONL,
        DEFAULT_WHATSAPP_MANIFEST,
        DEFAULT_WHATSAPP_NORMALIZED,
        DEFAULT_WHATSAPP_NORMALIZED_MANIFEST,
        Path(args.contacts).with_suffix(Path(args.contacts).suffix + ".llm_review.jsonl"),
    ]
    paths: list[Path] = []
    seen: set[str] = set()
    for path in base_paths:
        for candidate in (path, sidecar_manifest(path)):
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            paths.append(candidate)
    return paths


def archive_destination(archive_dir: Path, path: Path, used: set[str]) -> Path:
    name = path.name
    if str(path).startswith(".powerpacks/messages/"):
        try:
            name = str(path.relative_to(".powerpacks/messages"))
        except ValueError:
            name = path.name
    dest = archive_dir / name
    if str(dest) not in used:
        used.add(str(dest))
        return dest
    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    index = 2
    while True:
        candidate = parent / f"{stem}.{index}{suffix}"
        if str(candidate) not in used:
            used.add(str(candidate))
            return candidate
        index += 1


def fresh_archive_dir() -> Path:
    base = ARCHIVE_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not base.exists():
        return base
    index = 2
    while True:
        candidate = Path(f"{base}.{index}")
        if not candidate.exists():
            return candidate
        index += 1


def archive_existing_run_artifacts(args: argparse.Namespace) -> dict[str, Any] | None:
    if getattr(args, "command", "") != "run":
        return None

    paths = [path for path in fresh_run_artifact_paths(args) if path.exists() and path.is_file()]
    if not paths:
        return None

    archive_dir = fresh_archive_dir()
    moved: list[dict[str, str]] = []
    used: set[str] = set()
    for path in paths:
        dest = archive_destination(archive_dir, path, used)
        dest.parent.mkdir(parents=True, exist_ok=True)
        path.rename(dest)
        moved.append({"from": str(path), "to": str(dest)})

    summary = {
        "status": "archived",
        "reason": "fresh_import_run",
        "archive_dir": str(archive_dir),
        "moved_count": len(moved),
        "moved": moved,
    }
    write_json(archive_dir / "manifest.json", {
        "primitive": "import_contacts_pipeline",
        "created_at": now_iso(),
        **summary,
    })
    return summary


def archived_artifact(summary: dict[str, Any] | None, original_path: Path) -> str | None:
    if not summary:
        return None
    original = str(original_path)
    for moved in summary.get("moved") or []:
        if moved.get("from") == original and moved.get("to"):
            return str(moved["to"])
    return None


def csv_data_rows(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return sum(1 for _ in csv.DictReader(handle))
    except OSError:
        return 0


def review_human_state_rows(path: Path) -> int:
    """Count rows with explicit human review state worth carrying forward."""
    if not path.exists() or not path.is_file():
        return 0
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = csv.DictReader(handle)
            total = 0
            for row in rows:
                explicit = any((row.get(field) or "").strip() for field in ("exclude", "enrich_decision", "retarget_hint"))
                # Older/prepared review artifacts may encode explicit upload choices
                # directly as yes/no buckets. Do not treat confident/medium/review
                # buckets as human state; those are model defaults.
                bucket = (row.get("bucket") or "").strip().lower()
                if explicit or bucket in {"yes", "no"}:
                    total += 1
            return total
    except OSError:
        return 0


def archived_review_candidates() -> list[Path]:
    candidates: list[Path] = []
    roots = [ARCHIVE_ROOT, Path(".powerpacks/archive")]
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("**/research_review.csv"):
            key = str(path)
            if key not in seen:
                seen.add(key)
                candidates.append(path)
    return sorted(candidates, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)


def previous_review_state_sources(
    args: argparse.Namespace,
    ledger: dict[str, Any],
    *,
    include_active_for_rebuild: bool = False,
) -> list[Path]:
    """Return prior review CSVs with human state, newest/highest-priority first.

    Normal fresh runs record `artifacts.previous_research_review_csv` while
    archiving the active review CSV. Older/manual runs may only exist under an
    archive directory, so scan all archived `research_review.csv` files and let
    row-level handle/phone matching decide what applies to the active run.
    """
    active = Path(args.review_csv)
    candidates: list[Path] = []
    configured = (ledger.get("artifacts") or {}).get("previous_research_review_csv")
    if configured:
        candidates.append(Path(configured))
    if include_active_for_rebuild and getattr(args, "force_build_review", False):
        candidates.append(active)
    candidates.extend(archived_review_candidates())

    out: list[Path] = []
    seen: set[str] = set()
    active_resolved = active.resolve() if active.exists() else active.absolute()
    for candidate in candidates:
        try:
            resolved = candidate.resolve() if candidate.exists() else candidate.absolute()
        except OSError:
            resolved = candidate.absolute()
        if not include_active_for_rebuild and resolved == active_resolved:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if review_human_state_rows(candidate):
            out.append(candidate)
    return out


def fallback_previous_review_csv(args: argparse.Namespace, ledger: dict[str, Any]) -> str | None:
    sources = previous_review_state_sources(args, ledger, include_active_for_rebuild=True)
    return str(sources[0]) if sources else None


def truthy(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def falsy(value: str) -> bool:
    return (value or "").strip().lower() in {"0", "false", "no", "n", "off"}


def previous_review_state(row: dict[str, str]) -> dict[str, str]:
    """Normalize human state from prior review CSV rows.

    Native review UI stores explicit decisions in `exclude` (`no` means upload
    yes, `yes` means upload no). Older artifacts may have final yes/no directly
    in `bucket`, so treat those as explicit decisions too. Model buckets such as
    confident/medium/review are not human state.
    """
    state = {
        "exclude": (row.get("exclude") or "").strip(),
        "enrich_decision": (row.get("enrich_decision") or "").strip(),
        "retarget_hint": (row.get("retarget_hint") or "").strip(),
    }
    bucket = (row.get("bucket") or "").strip().lower()
    if not state["exclude"] and bucket == "yes":
        state["exclude"] = "no"
    elif not state["exclude"] and bucket == "no":
        state["exclude"] = "yes"
    return state


def load_previous_review_state(path: Path | None) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]], int]:
    if path is None or not path.exists() or not path.is_file():
        return {}, {}, 0
    by_handle: dict[str, dict[str, str]] = {}
    by_phone: dict[str, dict[str, str]] = {}
    count = 0
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                state = previous_review_state({key: value or "" for key, value in row.items()})
                if not any(state.values()):
                    continue
                count += 1
                row_handle = (row.get("handle") or "").strip()
                phone = (row.get("phone_e164") or "").strip()
                if row_handle:
                    by_handle[row_handle] = state
                if phone:
                    by_phone[phone] = state
    except OSError:
        return {}, {}, 0
    return by_handle, by_phone, count


def load_previous_review_states(paths: list[Path]) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]], int]:
    """Merge human state from multiple prior runs; newer sources win."""
    merged_by_handle: dict[str, dict[str, str]] = {}
    merged_by_phone: dict[str, dict[str, str]] = {}
    total_rows = 0
    # Sources are newest/highest-priority first. Apply older first, then newer
    # overwrites by handle/phone for stable "latest decision wins" behavior.
    for path in reversed(paths):
        by_handle, by_phone, rows = load_previous_review_state(path)
        total_rows += rows
        merged_by_handle.update(by_handle)
        merged_by_phone.update(by_phone)
    return merged_by_handle, merged_by_phone, total_rows


def reapply_previous_review_state(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    """Patch the active review CSV with prior human decisions/feedback.

    This runs even when `build_research_review_csv` was already completed, so a
    plain `continue` cannot regress prior yes/no decisions back to model buckets.
    Existing current-session explicit decisions win over archived state.
    """
    review_csv = Path(args.review_csv)
    sources = previous_review_state_sources(args, ledger)
    if not sources or not review_csv.exists():
        mark_step(ledger_path, ledger, "reapply_previous_review_state", "skipped", summary={"reason": "no_previous_review_state"})
        return
    by_handle, by_phone, previous_rows = load_previous_review_states(sources)
    if previous_rows == 0:
        mark_step(
            ledger_path,
            ledger,
            "reapply_previous_review_state",
            "skipped",
            summary={"reason": "previous_reviews_have_no_human_state", "previous_csvs": [str(path) for path in sources]},
        )
        return

    with review_csv.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or REVIEW_CSV_HEADERS)
        rows = [{key: value or "" for key, value in row.items()} for row in reader]
    for column in ("exclude", "enrich_decision", "retarget_hint"):
        if column not in fieldnames:
            fieldnames.append(column)
            for row in rows:
                row[column] = ""

    decisions_applied = 0
    feedback_applied = 0
    matched = 0
    for row in rows:
        state = by_handle.get((row.get("handle") or "").strip()) or by_phone.get((row.get("phone_e164") or "").strip())
        if not state:
            continue
        matched += 1
        current_exclude = (row.get("exclude") or "").strip()
        if not current_exclude and state.get("exclude"):
            row["exclude"] = state["exclude"]
            decisions_applied += 1
        current_enrich = (row.get("enrich_decision") or "").strip()
        if not current_enrich and state.get("enrich_decision"):
            row["enrich_decision"] = state["enrich_decision"]
        current_hint = (row.get("retarget_hint") or "").strip()
        if not current_hint and state.get("retarget_hint"):
            row["retarget_hint"] = state["retarget_hint"]
            feedback_applied += 1

    tmp = review_csv.with_suffix(review_csv.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(review_csv)
    summary = {
        "previous_csv": str(sources[0]) if sources else None,
        "previous_csvs": [str(path) for path in sources],
        "previous_run_count": len(sources),
        "previous_human_state_rows": previous_rows,
        "matched_rows": matched,
        "decisions_applied": decisions_applied,
        "feedback_applied": feedback_applied,
        "review_csv": str(review_csv),
    }
    mark_step(ledger_path, ledger, "reapply_previous_review_state", "completed", summary=summary)


def write_empty_csv(path: Path, fieldnames: list[str], *, manifest_reason: str | None = None) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=fieldnames).writeheader()
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    manifest = {
        "primitive": "import_contacts_pipeline",
        "created_at": now_iso(),
        "output": str(path),
        "manifest_path": str(manifest_path),
        "counts": {"rows_written": 0},
        "status": "ok",
    }
    if manifest_reason:
        manifest["reason"] = manifest_reason
    write_json(manifest_path, manifest)
    return manifest


def write_empty_jsonl(path: Path, *, manifest: Path | None = None, reason: str = "empty") -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    manifest_path = manifest or path.with_suffix(path.suffix + ".manifest.json")
    payload = {
        "primitive": "import_contacts_pipeline",
        "created_at": now_iso(),
        "output": str(path),
        "manifest_path": str(manifest_path),
        "counts": {"rows_written": 0},
        "status": "ok",
        "reason": reason,
    }
    write_json(manifest_path, payload)
    return payload


def remove_stale_empty_contacts(args: argparse.Namespace) -> None:
    """Drop the orchestrator's old empty-bootstrap artifact before re-merge."""
    contacts = Path(args.contacts)
    manifest = read_manifest(contacts.with_suffix(contacts.suffix + ".manifest.json")) or {}
    if (
        contacts.exists()
        and csv_data_rows(contacts) == 0
        and manifest.get("primitive") == "import_contacts_pipeline"
        and manifest.get("reason") == "no_channel_contact_exports_found"
        and (DEFAULT_IMESSAGE_CONTACTS.exists() or DEFAULT_WHATSAPP_CONTACTS.exists())
    ):
        contacts.unlink()


def extract_imessage(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    if DEFAULT_IMESSAGE_CONTACTS.exists() and not args.force_imessage:
        ledger.setdefault("artifacts", {})["imessage_contacts_csv"] = str(DEFAULT_IMESSAGE_CONTACTS)
        mark_step(ledger_path, ledger, "extract_imessage", "completed", summary={"reused": str(DEFAULT_IMESSAGE_CONTACTS)})
        return

    check_cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py"),
        "check",
        "--strict",
    ]
    mark_step(ledger_path, ledger, "check_imessage", "running", command=check_cmd)
    check_result = run_command(check_cmd, timeout=args.timeout, env=pipeline_env(args))
    check_payload = check_result.get("json") or {}
    if check_result["returncode"] != 0:
        payload = {
            "primitive": "import_contacts_pipeline",
            "status": "blocked_user_action",
            "message": "Enable macOS Full Disk Access / Contacts access for this terminal, then continue.",
            "detail": check_payload or (check_result.get("stderr") or check_result.get("stdout") or "")[-1000:],
            "ledger": str(ledger_path),
        }
        ledger["current_block"] = payload
        mark_step(ledger_path, ledger, "check_imessage", "blocked_user_action", summary=payload, command=check_cmd)
        raise PipelineBlocked(payload, code=21)
    mark_step(ledger_path, ledger, "check_imessage", "completed", summary=check_payload, command=check_cmd)

    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py"),
        "extract",
        "--output-csv", str(DEFAULT_IMESSAGE_CONTACTS),
        "--output-jsonl", str(DEFAULT_IMESSAGE_JSONL),
        "--manifest", str(DEFAULT_IMESSAGE_MANIFEST),
    ]
    mark_step(ledger_path, ledger, "extract_imessage", "running", command=cmd)
    result = run_command(cmd, timeout=max(args.timeout, 600), env=pipeline_env(args))
    payload = require_ok(result, "extract_imessage")
    ledger.setdefault("artifacts", {})["imessage_contacts_csv"] = str(DEFAULT_IMESSAGE_CONTACTS)
    mark_step(ledger_path, ledger, "extract_imessage", "completed", summary=payload, command=cmd)


def normalize_channel(
    args: argparse.Namespace,
    ledger_path: Path,
    ledger: dict[str, Any],
    *,
    step_id: str,
    input_csv: Path,
    output_jsonl: Path,
    manifest: Path,
    force: bool,
) -> None:
    if output_jsonl.exists() and not force:
        mark_step(ledger_path, ledger, step_id, "completed", summary={"reused": str(output_jsonl)})
        return
    if not input_csv.exists():
        payload = write_empty_jsonl(output_jsonl, manifest=manifest, reason=f"missing_input:{input_csv}")
        mark_step(ledger_path, ledger, step_id, "completed", summary=payload)
        return
    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/normalize_message_contacts/normalize_message_contacts.py"),
        "normalize",
        "--input", str(input_csv),
        "--out-jsonl", str(output_jsonl),
        "--manifest", str(manifest),
    ]
    mark_step(ledger_path, ledger, step_id, "running", command=cmd)
    result = run_command(cmd, timeout=args.timeout, env=pipeline_env(args))
    payload = require_ok(result, step_id)
    mark_step(ledger_path, ledger, step_id, "completed", summary=payload, command=cmd)


def extract_whatsapp(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    if completed(ledger, "extract_whatsapp") and DEFAULT_WHATSAPP_CONTACTS.exists() and not args.force_whatsapp:
        ledger.setdefault("artifacts", {})["whatsapp_contacts_csv"] = str(DEFAULT_WHATSAPP_CONTACTS)
        ledger.setdefault("artifacts", {})["whatsapp_message_count_cache"] = str(DEFAULT_WHATSAPP_MESSAGE_COUNT_CACHE)
        mark_step(
            ledger_path,
            ledger,
            "extract_whatsapp",
            "completed",
            summary={"reused": str(DEFAULT_WHATSAPP_CONTACTS), "reason": "active_run_completed"},
        )
        return

    check_cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/waha_runtime/waha_runtime.py"),
        "check",
    ]
    mark_step(ledger_path, ledger, "check_docker_and_waha", "running", command=check_cmd)
    check_result = run_command(check_cmd, timeout=args.timeout, env=pipeline_env(args))
    check_payload = check_result.get("json") or {}
    if check_result["returncode"] != 0:
        payload = {
            "primitive": "import_contacts_pipeline",
            "status": "blocked_user_action",
            "message": "Start Docker Desktop or Colima so WhatsApp sync can run, then continue the import.",
            "detail": check_payload or (check_result.get("stderr") or check_result.get("stdout") or "")[-1000:],
            "ledger": str(ledger_path),
        }
        ledger["current_block"] = payload
        mark_step(ledger_path, ledger, "check_docker_and_waha", "blocked_user_action", summary=payload, command=check_cmd)
        raise PipelineBlocked(payload, code=21)
    mark_step(ledger_path, ledger, "check_docker_and_waha", "completed", summary=check_payload, command=check_cmd)

    up_cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/waha_runtime/waha_runtime.py"),
        "up",
    ]
    mark_step(ledger_path, ledger, "start_waha_container", "running", command=up_cmd)
    up_result = run_command(up_cmd, timeout=max(args.timeout, 600), env=pipeline_env(args))
    up_payload = require_ok(up_result, "start_waha_container")
    mark_step(ledger_path, ledger, "start_waha_container", "completed", summary=up_payload, command=up_cmd)

    session_cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/waha_session/waha_session.py"),
        "start",
        "--open",
        "--wait",
    ]
    mark_step(ledger_path, ledger, "authenticate_whatsapp", "running", command=session_cmd)
    session_result = run_command(session_cmd, timeout=max(args.timeout, 600), env=pipeline_env(args))
    session_payload = session_result.get("json") or {}
    if session_result["returncode"] != 0:
        if session_payload.get("status") == "timeout":
            payload = {
                "primitive": "import_contacts_pipeline",
                "status": "blocked_user_action",
                "message": "WhatsApp needs you to scan the QR, then continue the import.",
                "qr_path": str(Path(".powerpacks/messages/whatsapp/qr.png")),
                "continue_command": f"uv run --project . python {rel(Path(__file__).resolve())} continue",
                "ledger": str(ledger_path),
            }
            ledger["current_block"] = payload
            mark_step(ledger_path, ledger, "authenticate_whatsapp", "blocked_user_action", summary=payload, command=session_cmd)
            raise PipelineBlocked(payload, code=21)
        raise PipelineFailed(f"authenticate_whatsapp failed rc={session_result['returncode']}: {((session_result.get('stderr') or session_result.get('stdout') or '').strip())[-1000:]}")
    mark_step(ledger_path, ledger, "authenticate_whatsapp", "completed", summary=session_payload, command=session_cmd)

    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/extract_whatsapp_contacts/extract_whatsapp_contacts.py"),
        "extract",
        "--output-csv", str(DEFAULT_WHATSAPP_CONTACTS),
        "--output-jsonl", str(DEFAULT_WHATSAPP_JSONL),
        "--manifest", str(DEFAULT_WHATSAPP_MANIFEST),
        "--message-count-cache", str(DEFAULT_WHATSAPP_MESSAGE_COUNT_CACHE),
    ]
    mark_step(ledger_path, ledger, "extract_whatsapp", "running", command=cmd)
    result = run_command(cmd, timeout=args.parallel_timeout, env=pipeline_env(args))
    payload = require_ok(result, "extract_whatsapp")
    ledger.setdefault("artifacts", {})["whatsapp_contacts_csv"] = str(DEFAULT_WHATSAPP_CONTACTS)
    ledger.setdefault("artifacts", {})["whatsapp_message_count_cache"] = str(DEFAULT_WHATSAPP_MESSAGE_COUNT_CACHE)
    mark_step(ledger_path, ledger, "extract_whatsapp", "completed", summary=payload, command=cmd)


def write_empty_contacts_csv(path: Path) -> dict[str, Any]:
    manifest = write_empty_csv(path, CONTACT_CSV_HEADERS, manifest_reason="no_channel_contact_exports_found")
    manifest["command"] = "ensure_contacts"
    manifest["counts"] = {
        "rows_written": 0,
        "unique_phones": 0,
        "cross_channel_phones": 0,
        "by_source": {},
    }
    write_json(Path(manifest["manifest_path"]), manifest)
    return manifest


def ensure_contacts(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    remove_stale_empty_contacts(args)
    contacts = Path(args.contacts)
    if contacts.exists():
        try:
            validate_contacts_csv(contacts)
        except PipelineFailed as exc:
            mark_step(ledger_path, ledger, "ensure_contacts", "failed", summary={
                "contacts": str(contacts),
                "schema_docs": CONTACT_SCHEMA_DOC,
                "schema_json": CONTACT_SCHEMA_JSON,
            }, error=str(exc))
            raise
        ledger.setdefault("artifacts", {})["contacts_csv"] = str(contacts)
        mark_step(ledger_path, ledger, "ensure_contacts", "completed", summary={"contacts": str(contacts)})
        return

    source_paths = [path for path in (DEFAULT_IMESSAGE_CONTACTS, DEFAULT_WHATSAPP_CONTACTS) if path.exists()]
    if source_paths:
        cmd = [
            sys.executable,
            primitive_path("packs/messages/primitives/merge_message_contacts/merge_message_contacts.py"),
            "merge",
        ]
        for source_path in source_paths:
            cmd.extend(["--input", str(source_path)])
        cmd.extend(["--output", str(contacts)])
        mark_step(ledger_path, ledger, "ensure_contacts", "running", command=cmd)
        result = run_command(cmd, timeout=args.timeout, env=pipeline_env(args))
        payload = require_ok(result, "ensure_contacts")
        ledger.setdefault("artifacts", {})["contacts_csv"] = str(contacts)
        mark_step(
            ledger_path,
            ledger,
            "ensure_contacts",
            "completed",
            summary={"contacts": str(contacts), "source": "merged_channel_exports", "merge": payload},
            command=cmd,
        )
        return

    payload = write_empty_contacts_csv(contacts)
    ledger.setdefault("artifacts", {})["contacts_csv"] = str(contacts)
    mark_step(
        ledger_path,
        ledger,
        "ensure_contacts",
        "completed",
        summary={"contacts": str(contacts), "source": "empty_bootstrap", **payload},
    )


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
        "--batch-size", str(args.llm_batch_size),
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

    auto_ok = estimated_usd < float(args.llm_auto_approve_usd)
    payload = {"estimated_usd": estimated_usd}
    aid = approval_id("llm", payload)
    if not auto_ok and not is_approved(ledger, aid):
        block_for_approval(
            ledger_path,
            ledger,
            args,
            step_id="llm_review",
            kind="llm",
            payload=payload,
            message=f"Estimated OpenRouter cost: ${estimated_usd:.4f}. Approve?",
        )

    review_cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/llm_review_contacts/llm_review_contacts.py"),
        "review",
        "--input", str(args.contacts),
        "--model", args.model,
        "--batch-size", str(args.llm_batch_size),
        "--max-workers", str(args.llm_max_workers),
    ]
    mark_step(ledger_path, ledger, "llm_review", "running", command=review_cmd)
    review_result = run_command(review_cmd, timeout=max(args.timeout, 600), env=pipeline_env(args))
    review_payload = require_ok(review_result, "llm_review")
    if auto_ok:
        review_payload["auto_approved_reason"] = f"openrouter_under_{args.llm_auto_approve_usd}_usd"
    mark_step(ledger_path, ledger, "llm_review", "completed", summary=review_payload, command=review_cmd)


def llm_pricing(model: str) -> tuple[float, float]:
    return {
        "anthropic/claude-sonnet-4-6": (3.00, 15.00),
        "anthropic/claude-haiku-4-5": (0.80, 4.00),
        "openai/gpt-4.1": (2.00, 8.00),
        "openai/gpt-4.1-mini": (0.40, 1.60),
        "openai/gpt-4.1-nano": (0.10, 0.40),
    }.get(model, (2.00, 8.00))


def estimate_network_review_scoring(args: argparse.Namespace) -> dict[str, Any]:
    research_dir = Path(args.research_dir)
    model = DEFAULT_NETWORK_REVIEW_MODEL
    candidates = 0
    for research_packet in research_dir.glob("*/01_research_parallel.json"):
        profile_dir = research_packet.parent
        if (profile_dir / "03_network_review.json").exists():
            continue
        candidates += 1
    input_price, output_price = llm_pricing(model)
    input_tokens = candidates * NETWORK_REVIEW_TOKEN_ESTIMATE["input"]
    output_tokens = candidates * NETWORK_REVIEW_TOKEN_ESTIMATE["output"]
    estimated_usd = round((input_tokens / 1e6) * input_price + (output_tokens / 1e6) * output_price, 4)
    return {
        "candidates": candidates,
        "model": model,
        "estimated_tokens": {"input": input_tokens, "output": output_tokens},
        "estimated_usd": estimated_usd,
    }


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
    latency = parallel_latency_summary(args.processor, estimate_payload)
    if would_submit <= 0:
        mark_step(ledger_path, ledger, "deep_research", "completed", summary={"status": "no_work", **estimate_payload})
        return

    payload = {
        "estimated_usd": estimated_usd,
        "estimated_latency": latency,
        "processor": args.processor,
        "would_submit": would_submit,
    }
    aid = approval_id("parallel", payload)
    if not is_approved(ledger, aid):
        block_for_approval(
            ledger_path,
            ledger,
            args,
            step_id="deep_research",
            kind="parallel",
            payload=payload,
            message=parallel_approval_message(estimated_usd, latency),
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
    if completed(ledger, "build_raw_review_csv") and Path(args.review_csv).exists() and not args.force_build_review:
        mark_step(
            ledger_path,
            ledger,
            "build_research_review_csv",
            "skipped",
            summary={"reason": "raw_review_csv_active", "review_csv": str(args.review_csv)},
        )
        return
    research_dir = Path(args.research_dir)
    research_dir.mkdir(parents=True, exist_ok=True)
    research_packets = list(research_dir.glob("*/01_research_parallel.json"))
    if not research_packets:
        write_empty_csv(Path(args.review_csv), REVIEW_CSV_HEADERS, manifest_reason="no_research_packets")
        mark_step(
            ledger_path,
            ledger,
            "build_research_review_csv",
            "skipped",
            summary={"reason": "no_research_packets", "research_dir": str(args.research_dir), "review_csv": str(args.review_csv)},
        )
        return
    scoring_estimate: dict[str, Any] | None = None
    auto_ok = False
    scoring_estimate = estimate_network_review_scoring(args)
    if int(scoring_estimate.get("candidates") or 0) > 0:
        auto_threshold = float(getattr(args, "llm_auto_approve_usd", DEFAULT_LLM_AUTO_APPROVE_USD))
        auto_ok = float(scoring_estimate.get("estimated_usd") or 0.0) < auto_threshold
        payload = {"phase": "network_review", **scoring_estimate}
        aid = approval_id("llm", payload)
        if not auto_ok and not is_approved(ledger, aid):
            block_for_approval(
                ledger_path,
                ledger,
                args,
                step_id="build_research_review_csv",
                kind="llm",
                payload=payload,
                message=(
                    "Estimated OpenRouter cost for deep-research approval scoring: "
                    f"${float(scoring_estimate.get('estimated_usd') or 0.0):.4f}. Approve?"
                )
            )

    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/build_research_review_csv/build_research_review_csv.py"),
        "build",
        "--research-dir", str(args.research_dir),
        "--queue-csv", str(args.research_queue),
        "--output-csv", str(args.review_csv),
        "--model", DEFAULT_NETWORK_REVIEW_MODEL,
    ]
    previous_review_csv = fallback_previous_review_csv(args, ledger)
    if previous_review_csv and Path(previous_review_csv).exists():
        cmd.extend(["--previous-csv", str(previous_review_csv)])
    mark_step(ledger_path, ledger, "build_research_review_csv", "running", command=cmd)
    result = run_command(cmd, timeout=max(args.timeout, 3600), env=pipeline_env(args))
    payload = require_ok(result, "build_research_review_csv")
    if scoring_estimate is not None:
        payload["scoring_estimate"] = scoring_estimate
    if auto_ok:
        payload["auto_approved_reason"] = f"openrouter_under_{getattr(args, 'llm_auto_approve_usd', DEFAULT_LLM_AUTO_APPROVE_USD)}_usd"
    ledger.setdefault("artifacts", {})["research_review_csv"] = str(args.review_csv)
    mark_step(ledger_path, ledger, "build_research_review_csv", "completed", summary=payload, command=cmd)


def migrate_review_schema(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    step_id = "migrate_review_schema"
    review_csv = Path(args.review_csv)
    contacts_csv = Path(args.contacts)
    if completed(ledger, step_id) and review_csv.exists() and not args.force_build_review:
        fields = set(read_csv_fieldnames(review_csv) or [])
        if {"in_network", "network_person_id", "review_source"}.issubset(fields):
            return
    if not review_csv.exists() or csv_data_rows(review_csv) == 0:
        mark_step(ledger_path, ledger, step_id, "skipped", summary={"reason": "no_review_csv", "review_csv": str(review_csv)})
        return
    if not contacts_csv.exists():
        mark_step(ledger_path, ledger, step_id, "skipped", summary={"reason": "no_contacts_csv", "contacts_csv": str(contacts_csv)})
        return
    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/migrate_review_schema/migrate_review_schema.py"),
        "migrate",
        "--artifacts-dir", str(review_csv.parent),
        "--review-csv", str(review_csv),
        "--contacts-csv", str(contacts_csv),
    ]
    mark_step(ledger_path, ledger, step_id, "running", command=cmd)
    result = run_command(cmd, timeout=args.timeout, env=pipeline_env(args))
    payload = require_ok(result, "migrate_review_schema")
    ledger.setdefault("artifacts", {})["research_review_csv"] = str(review_csv)
    mark_step(ledger_path, ledger, step_id, "completed", summary=payload, command=cmd)


def has_research_review(args: argparse.Namespace) -> bool:
    return Path(args.review_csv).exists() and csv_data_rows(Path(args.review_csv)) > 0


def review_url(args: argparse.Namespace) -> str:
    query = urllib.parse.urlencode({"tab": "yes"})
    return f"http://{args.review_host}:{args.review_port}/?{query}"


def review_health_url(args: argparse.Namespace) -> str:
    return f"http://{args.review_host}:{args.review_port}/healthz"


def read_review_server_health(args: argparse.Namespace) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(review_health_url(args), timeout=1) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def review_server_matches_current_csv(args: argparse.Namespace) -> bool:
    health = read_review_server_health(args)
    if not health or health.get("status") != "ok":
        return False
    served = health.get("csv")
    if not served:
        return False
    try:
        return Path(served).resolve() == Path(args.review_csv).resolve()
    except OSError:
        return False


def process_command(pid: int) -> str:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return ""
    return completed.stdout.strip()


def stop_review_server_process(pid: int) -> None:
    if "review_research_web.py" not in process_command(pid):
        return
    try:
        os.kill(pid, 15)
    except OSError:
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def maybe_open_browser(args: argparse.Namespace) -> None:
    if getattr(args, "open_browser", False):
        webbrowser.open(review_url(args))


def wait_for_review_server(args: argparse.Namespace, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if review_server_matches_current_csv(args):
            return True
        time.sleep(0.1)
    return False


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
            if review_server_matches_current_csv(args):
                maybe_open_browser(args)
                mark_step(ledger_path, ledger, "review_research_web", "completed", summary={"url": review_url(args), "pid": pid, "reused": True, "csv": str(Path(args.review_csv).resolve())})
                return
            stop_review_server_process(pid)
            pid_path.unlink(missing_ok=True)
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
    if getattr(args, "open_browser", False):
        cmd.append("--open")
    mark_step(ledger_path, ledger, "review_research_web", "running", command=cmd)
    with log_path.open("ab") as log:
        proc = subprocess.Popen(cmd, cwd=repo_root(), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    pid_path.write_text(str(proc.pid) + "\n", encoding="utf-8")
    if not wait_for_review_server(args):
        mark_step(ledger_path, ledger, "review_research_web", "failed", summary={"url": review_url(args), "pid": proc.pid, "log": str(log_path)}, command=cmd, error="review_server_not_ready")
        raise PipelineFailed(f"Review server did not become ready at {review_url(args)}")
    mark_step(ledger_path, ledger, "review_research_web", "completed", summary={"url": review_url(args), "pid": proc.pid, "log": str(log_path), "csv": str(Path(args.review_csv).resolve())}, command=cmd)


def raw_review_url(args: argparse.Namespace) -> str:
    return f"http://{args.review_host}:{args.review_port}/"


def open_raw_contacts_review_server(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    if args.no_open_review:
        mark_step(ledger_path, ledger, "review_contacts_web_fallback", "skipped", summary={"reason": "--no-open-review"})
        return
    server_dir = repo_root() / ".powerpacks/servers"
    server_dir.mkdir(parents=True, exist_ok=True)
    pid_path = server_dir / "contacts-review-http.pid"
    log_path = server_dir / "contacts-review-http.log"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            mark_step(ledger_path, ledger, "review_contacts_web_fallback", "completed", summary={"url": raw_review_url(args), "pid": pid, "reused": True})
            return
        except Exception:
            pid_path.unlink(missing_ok=True)

    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/review_contacts_web/review_contacts_web.py"),
        "serve",
        "--contacts", str(args.contacts),
        "--host", args.review_host,
        "--port", str(args.review_port),
    ]
    if getattr(args, "open_browser", False):
        cmd.append("--open")
    mark_step(ledger_path, ledger, "review_contacts_web_fallback", "running", command=cmd)
    with log_path.open("ab") as log:
        proc = subprocess.Popen(cmd, cwd=repo_root(), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    pid_path.write_text(str(proc.pid) + "\n", encoding="utf-8")
    time.sleep(1)
    mark_step(ledger_path, ledger, "review_contacts_web_fallback", "completed", summary={"url": raw_review_url(args), "pid": proc.pid, "log": str(log_path)}, command=cmd)


def digits_only(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def phone_area_code(phone: str) -> str:
    digits = digits_only(phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[:3] if len(digits) >= 10 else ""


def phone_handle(phone: str, name: str = "") -> str:
    digits = digits_only(phone)
    if len(digits) >= 10:
        return f"phone-{digits[-10:]}"
    safe_name = "".join(ch.lower() if ch.isalnum() else "-" for ch in (name or "unknown")).strip("-")
    while "--" in safe_name:
        safe_name = safe_name.replace("--", "-")
    return safe_name or "unknown"


def load_queue_rows_by_phone(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                phone = digits_only(row.get("phone_e164", ""))
                if phone:
                    out[phone] = {key: value or "" for key, value in row.items()}
    except OSError:
        return {}
    return out


def raw_decision(row: dict[str, str]) -> tuple[str, str, str]:
    explicit = (row.get("enrich_decision") or "").strip().lower()
    skip = (row.get("skip") or "").strip().lower()
    if explicit == "yes" or skip in {"false", "0", "no"}:
        return "yes", "false", "yes"
    if explicit == "no" or skip in {"true", "1", "yes"}:
        return "no", "true", "no"
    return "maybe", "", ""


def build_raw_review_csv(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    if completed(ledger, "build_raw_review_csv") and Path(args.review_csv).exists() and not args.force_build_review:
        return
    contacts_path = Path(args.contacts)
    queue_by_phone = load_queue_rows_by_phone(Path(args.research_queue))
    rows: list[dict[str, str]] = []
    if contacts_path.exists():
        with contacts_path.open(newline="", encoding="utf-8-sig") as handle:
            for contact in csv.DictReader(handle):
                contact = {key: value or "" for key, value in contact.items()}
                phone = contact.get("phone", "")
                name = contact.get("matched_name") or contact.get("name") or ""
                queue_row = queue_by_phone.get(digits_only(phone), {})
                bucket, exclude, enrich_decision = raw_decision(contact)
                rows.append({
                    "bucket": bucket,
                    "handle": queue_row.get("handle") or phone_handle(phone, name),
                    "full_name": queue_row.get("display_name") or name,
                    "phone_e164": queue_row.get("phone_e164") or phone,
                    "area_code": queue_row.get("area_code") or phone_area_code(phone),
                    "total_messages": queue_row.get("total_messages") or contact.get("message_count", ""),
                    "imessage_message_count": queue_row.get("imessage_message_count") or contact.get("imessage_message_count", ""),
                    "whatsapp_message_count": queue_row.get("whatsapp_message_count") or contact.get("whatsapp_message_count", ""),
                    "message_source": queue_row.get("message_source") or contact.get("source", ""),
                    "last_message": queue_row.get("last_message") or contact.get("last_message", ""),
                    "imessage_last_message": queue_row.get("imessage_last_message") or contact.get("imessage_last_message", ""),
                    "whatsapp_last_message": queue_row.get("whatsapp_last_message") or contact.get("whatsapp_last_message", ""),
                    "group_names": queue_row.get("group_names") or contact.get("group_names", ""),
                    "location_city": "",
                    "location_country": "",
                    "top_titles": "",
                    "top_companies": "",
                    "top_title_company_pairs": "",
                    "schools": "",
                    "short_reason": "Raw contact review fallback",
                    "identity_risk": "",
                    "signals": "",
                    "retarget_hint": "",
                    "exclude": exclude,
                    "enrich_decision": enrich_decision,
                    "in_network": "",
                    "network_match_status": "",
                    "network_person_id": "",
                    "network_name": "",
                    "network_linkedin_url": "",
                    "network_match_confidence": "",
                    "network_match_method": "",
                    "network_match_reason": "",
                    "review_source": "raw_review",
                })
    review_csv = Path(args.review_csv)
    review_csv.parent.mkdir(parents=True, exist_ok=True)
    with review_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)
    manifest = {
        "primitive": "import_contacts_pipeline",
        "command": "build_raw_review_csv",
        "created_at": now_iso(),
        "contacts": str(contacts_path),
        "output": str(review_csv),
        "counts": {
            "rows_written": len(rows),
            "yes": sum(1 for row in rows if row["bucket"] == "yes"),
            "maybe": sum(1 for row in rows if row["bucket"] == "maybe"),
            "no": sum(1 for row in rows if row["bucket"] == "no"),
        },
        "status": "ok",
    }
    write_json(review_csv.with_suffix(review_csv.suffix + ".manifest.json"), manifest)
    ledger.setdefault("artifacts", {})["research_review_csv"] = str(review_csv)
    mark_step(ledger_path, ledger, "build_raw_review_csv", "completed", summary=manifest)


def retarget_rows_from_payload(payload: dict[str, Any]) -> int:
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    return int(payload.get("rows_written") or counts.get("queued") or 0)


def estimate_retarget_linkedin_profiles(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    retarget_status = ((ledger.get("steps") or {}).get("retarget_research") or {}).get("status")
    step_id = "retarget_rapidapi_estimate"
    if completed(ledger, step_id) and retarget_status == "blocked_approval":
        return ((ledger.get("steps") or {}).get(step_id) or {}).get("summary") or {}

    estimate_cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/refresh_retarget_linkedin_profiles/refresh_retarget_linkedin_profiles.py"),
        "estimate",
        "--review-csv", str(args.review_csv),
        "--retarget-output-dir", str(args.retarget_research_dir),
    ]
    mark_step(ledger_path, ledger, step_id, "running", command=estimate_cmd)
    estimate_result = run_command(estimate_cmd, timeout=args.timeout, env=pipeline_env(args))
    estimate_payload = require_ok(estimate_result, step_id)
    mark_step(ledger_path, ledger, step_id, "completed", summary=estimate_payload, command=estimate_cmd)
    return estimate_payload


def run_retarget_linkedin_refresh(
    args: argparse.Namespace,
    ledger_path: Path,
    ledger: dict[str, Any],
    estimate_payload: dict[str, Any],
) -> dict[str, Any]:
    step_id = "refresh_retarget_linkedin_profiles"
    if completed(ledger, step_id):
        return ((ledger.get("steps") or {}).get(step_id) or {}).get("summary") or {}
    would_fetch = int(estimate_payload.get("would_fetch") or 0)
    if would_fetch <= 0:
        payload = {"status": "no_work", "estimate": estimate_payload}
        mark_step(ledger_path, ledger, step_id, "completed", summary=payload)
        return payload
    if not estimate_payload.get("api_key_present"):
        payload = {"status": "skipped", "reason": "missing_rapidapi_key", "estimate": estimate_payload}
        mark_step(ledger_path, ledger, step_id, "completed", summary=payload)
        return payload

    run_cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/refresh_retarget_linkedin_profiles/refresh_retarget_linkedin_profiles.py"),
        "run",
        "--review-csv", str(args.review_csv),
        "--ledger", str(args.retarget_ledger),
        "--retarget-output-dir", str(args.retarget_research_dir),
        "--max-workers", str(getattr(args, "retarget_rapidapi_max_workers", DEFAULT_RETARGET_HARNESS_MAX_WORKERS)),
    ]
    mark_step(ledger_path, ledger, step_id, "running", command=run_cmd)
    run_result = run_command(run_cmd, timeout=args.timeout, env=pipeline_env(args))
    payload = require_ok(run_result, step_id)
    mark_step(ledger_path, ledger, step_id, "completed", summary=payload, command=run_cmd)
    return payload


def estimate_retarget_parallel(
    args: argparse.Namespace,
    ledger_path: Path,
    ledger: dict[str, Any],
    *,
    step_id: str = "retarget_parallel_estimate",
) -> dict[str, Any]:
    estimate_cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/deep_research_contacts/deep_research_contacts.py"),
        "estimate",
        "--input", str(args.retarget_queue),
        "--processor", args.processor,
        "--output-dir", str(args.retarget_research_dir),
    ]
    mark_step(ledger_path, ledger, step_id, "running", command=estimate_cmd)
    estimate_result = run_command(estimate_cmd, timeout=args.timeout, env=pipeline_env(args))
    estimate_payload = require_ok(estimate_result, step_id)
    mark_step(ledger_path, ledger, step_id, "completed", summary=estimate_payload, command=estimate_cmd)
    return estimate_payload


def prepare_retarget_queue(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    step_id = "prepare_retarget_queue"
    retarget_status = ((ledger.get("steps") or {}).get("retarget_research") or {}).get("status")
    if completed(ledger, step_id) and retarget_status == "blocked_approval":
        return ((ledger.get("steps") or {}).get(step_id) or {}).get("summary") or {}
    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/prepare_retarget_queue/prepare_retarget_queue.py"),
        "prepare",
        "--review-csv", str(args.review_csv),
        "--base-queue", str(args.research_queue),
        "--output", str(args.retarget_queue),
        "--ledger", str(args.retarget_ledger),
        "--retarget-output-dir", str(args.retarget_research_dir),
    ]
    mark_step(ledger_path, ledger, step_id, "running", command=cmd)
    result = run_command(cmd, timeout=args.timeout, env=pipeline_env(args))
    payload = require_ok(result, step_id)
    ledger.setdefault("artifacts", {})["retarget_queue_csv"] = str(args.retarget_queue)
    ledger.setdefault("artifacts", {})["retarget_attempts_json"] = str(args.retarget_ledger)
    ledger.setdefault("artifacts", {})["retarget_research_dir"] = str(args.retarget_research_dir)
    mark_step(ledger_path, ledger, step_id, "completed", summary=payload, command=cmd)
    return payload


def mark_retarget_completed(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/prepare_retarget_queue/prepare_retarget_queue.py"),
        "mark-completed",
        "--ledger", str(args.retarget_ledger),
        "--retarget-output-dir", str(args.retarget_research_dir),
        "--review-csv", str(args.review_csv),
    ]
    mark_step(ledger_path, ledger, "retarget_mark_completed", "running", command=cmd)
    result = run_command(cmd, timeout=args.timeout, env=pipeline_env(args))
    payload = require_ok(result, "retarget_mark_completed")
    mark_step(ledger_path, ledger, "retarget_mark_completed", "completed", summary=payload, command=cmd)
    return payload


def merge_cached_retarget_results(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/prepare_retarget_queue/prepare_retarget_queue.py"),
        "merge-cached",
        "--ledger", str(args.retarget_ledger),
        "--retarget-output-dir", str(args.retarget_research_dir),
        "--review-csv", str(args.review_csv),
    ]
    mark_step(ledger_path, ledger, "retarget_merge_cached", "running", command=cmd)
    result = run_command(cmd, timeout=args.timeout, env=pipeline_env(args))
    payload = require_ok(result, "retarget_merge_cached")
    mark_step(ledger_path, ledger, "retarget_merge_cached", "completed", summary=payload, command=cmd)
    return payload


def run_harness_retarget_research(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/harness_retarget_research/harness_retarget_research.py"),
        "run",
        "--input", str(args.retarget_queue),
        "--output-dir", str(args.retarget_research_dir),
        "--prompt-dir", str(getattr(args, "retarget_harness_prompt_dir", DEFAULT_RETARGET_HARNESS_PROMPT_DIR)),
        "--harness", getattr(args, "retarget_harness", "off"),
        "--timeout", str(getattr(args, "retarget_harness_timeout", 900)),
        "--max-workers", str(getattr(args, "retarget_harness_max_workers", DEFAULT_RETARGET_HARNESS_MAX_WORKERS)),
    ]
    mark_step(ledger_path, ledger, "retarget_harness_research", "running", command=cmd)
    rows = max(1, csv_data_rows(Path(args.retarget_queue)))
    max_workers = max(1, int(getattr(args, "retarget_harness_max_workers", DEFAULT_RETARGET_HARNESS_MAX_WORKERS) or 1))
    batches = max(1, (rows + max_workers - 1) // max_workers)
    result = run_command(cmd, timeout=(getattr(args, "retarget_harness_timeout", 900) * batches) + 60, env=pipeline_env(args))
    payload = require_ok(result, "retarget_harness_research")
    if payload.get("status") == "prepared":
        mark_step(ledger_path, ledger, "retarget_harness_research", "failed", summary=payload, command=cmd, error="no_cli_harness_available")
        raise PipelineFailed(
            "No Codex/Claude CLI harness was available; retarget prompts were prepared. "
            "Run them manually or set --retarget-harness off to use Parallel."
        )
    mark_step(ledger_path, ledger, "retarget_harness_research", "completed", summary=payload, command=cmd)
    return payload


def retarget_research_after_review(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    refresh_estimate = estimate_retarget_linkedin_profiles(args, ledger_path, ledger)
    prepare_payload = prepare_retarget_queue(args, ledger_path, ledger)
    rows_written = retarget_rows_from_payload(prepare_payload)
    rapidapi_would_fetch = int(refresh_estimate.get("would_fetch") or 0) if refresh_estimate.get("api_key_present") else 0
    if rows_written <= 0 and rapidapi_would_fetch <= 0:
        mark_payload = mark_retarget_completed(args, ledger_path, ledger)
        merged = int(mark_payload.get("review_rows_merged") or 0)
        mark_step(ledger_path, ledger, "retarget_research", "completed", summary={
            "status": "cached_results_merged" if merged > 0 else "no_work",
            "reason": "retarget_results_already_cached" if merged > 0 else "no_new_retarget_hints",
            "refresh_retarget_linkedin_profiles": {"status": "no_work", "estimate": refresh_estimate},
            "prepare_retarget_queue": prepare_payload,
            "mark_completed": mark_payload,
        })
        return

    pre_estimate_payload: dict[str, Any] = {}
    would_submit = 0
    estimated_usd = 0.0
    latency: dict[str, Any] = {"rough_wall_clock": "roughly a few minutes once submitted"}
    if rows_written > 0:
        pre_estimate_payload = estimate_retarget_parallel(args, ledger_path, ledger)
        would_submit = int(pre_estimate_payload.get("would_submit") or 0)
        estimated_usd = float(pre_estimate_payload.get("estimated_usd") or 0.0)
        latency = parallel_latency_summary(args.processor, pre_estimate_payload)

    approval_payload = {
        "kind": "retarget_research",
        "estimated_usd": estimated_usd,
        "estimated_latency": latency,
        "processor": args.processor,
        "would_submit": would_submit,
        "feedback_rows": rows_written,
        "rapidapi_would_fetch": rapidapi_would_fetch,
    }
    aid = approval_id("parallel", approval_payload)
    if not is_approved(ledger, aid):
        block_for_approval(
            ledger_path,
            ledger,
            args,
            step_id="retarget_research",
            kind="parallel",
            payload=approval_payload,
            message=retarget_approval_message(),
        )

    refresh_payload = run_retarget_linkedin_refresh(args, ledger_path, ledger, refresh_estimate)

    retarget_harness = getattr(args, "retarget_harness", "off")
    retarget_harness_threshold = getattr(args, "retarget_harness_threshold", DEFAULT_RETARGET_HARNESS_THRESHOLD)
    rapidapi_refreshed = int(refresh_payload.get("refreshed") or 0)
    if retarget_harness != "off" and rapidapi_refreshed <= 0 and rows_written < retarget_harness_threshold:
        harness_payload = run_harness_retarget_research(args, ledger_path, ledger)
        mark_payload = mark_retarget_completed(args, ledger_path, ledger)
        merged = int(mark_payload.get("review_rows_merged") or 0)
        if merged <= 0:
            raise PipelineFailed("retarget harness completed but no review rows were merged")
        mark_step(ledger_path, ledger, "retarget_research", "completed", summary={
            "mode": "harness",
            "threshold": retarget_harness_threshold,
            "refresh_retarget_linkedin_profiles": refresh_payload,
            "prepare_retarget_queue": prepare_payload,
            "harness": harness_payload,
            "mark_completed": mark_payload,
        })
        return

    estimate_payload = estimate_retarget_parallel(args, ledger_path, ledger, step_id="retarget_parallel_estimate_after_refresh")
    would_submit = int(estimate_payload.get("would_submit") or 0)
    if would_submit <= 0:
        mark_payload = mark_retarget_completed(args, ledger_path, ledger)
        mark_step(ledger_path, ledger, "retarget_research", "completed", summary={
            "status": "no_work",
            "reason": "retarget_results_already_cached",
            "refresh_retarget_linkedin_profiles": refresh_payload,
            "prepare_retarget_queue": prepare_payload,
            "pre_refresh_estimate": pre_estimate_payload,
            "estimate": estimate_payload,
            "mark_completed": mark_payload,
        })
        return

    run_cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/deep_research_contacts/deep_research_contacts.py"),
        "run",
        "--input", str(args.retarget_queue),
        "--processor", args.processor,
        "--output-dir", str(args.retarget_research_dir),
    ]
    mark_step(ledger_path, ledger, "retarget_research", "running", command=run_cmd)
    run_result = run_command(run_cmd, timeout=args.parallel_timeout, env=pipeline_env(args))
    payloads = run_result.get("json_objects") or []
    final_payload = payloads[-1] if payloads else None
    if run_result["returncode"] != 0:
        raise PipelineFailed(f"retarget deep_research failed rc={run_result['returncode']}: {((run_result.get('stderr') or run_result.get('stdout') or '').strip())[-1000:]}")
    mark_payload = mark_retarget_completed(args, ledger_path, ledger)
    mark_step(ledger_path, ledger, "retarget_research", "completed", summary={
        "refresh_retarget_linkedin_profiles": refresh_payload,
        "pre_refresh_estimate": pre_estimate_payload,
        "outputs": payloads,
        "final": final_payload,
        "mark_completed": mark_payload,
    }, command=run_cmd)


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
        "approved_count": int(summary.get("approved_count") or 0),
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
                "Upload approved contacts to Powerset? "
                f"uploading {payload['approved_count']}."
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
    response = upload_payload.get("response") or {}
    upload_count = (
        upload_payload.get("approved_count")
        or response.get("approved_count")
        or (upload_payload.get("prepared_summary") or {}).get("approved_count")
    )
    if upload_count is not None:
        upload_payload["approved_count"] = int(upload_count)
        upload_payload["user_message"] = f"Uploaded {int(upload_count)} approved contacts"
    ledger.setdefault("artifacts", {})["uploaded_artifact_id"] = response.get("artifact_id")
    mark_step(ledger_path, ledger, "upload_research_review", "completed", summary=upload_payload, command=cmd)


def sync_contact_datalake(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any]) -> None:
    if completed(ledger, "sync_contact_datalake") and not args.rerun_upload:
        return
    cmd = [
        sys.executable,
        primitive_path("packs/messages/primitives/sync_contact_datalake/sync_contact_datalake.py"),
        "sync",
        "--csv", str(args.review_csv),
        "--research-dir", str(args.research_dir),
        "--confirm-sync",
    ]
    mark_step(ledger_path, ledger, "sync_contact_datalake", "running", command=cmd)
    result = run_command(cmd, timeout=args.timeout, env=pipeline_env(args))
    payload = require_ok(result, "sync_contact_datalake")
    mark_step(ledger_path, ledger, "sync_contact_datalake", "completed", summary=payload, command=cmd)


def fill_arg_defaults(args: argparse.Namespace) -> None:
    defaults = {
        "retarget_queue": DEFAULT_RETARGET_QUEUE,
        "retarget_ledger": DEFAULT_RETARGET_LEDGER,
        "retarget_research_dir": DEFAULT_RETARGET_RESEARCH_DIR,
        "timeout": 300,
        "parallel_timeout": 7600,
        "env_file": ".env",
        "processor": DEFAULT_PROCESSOR,
    }
    for attr, value in defaults.items():
        if not hasattr(args, attr):
            setattr(args, attr, value)


def hydrate_args_from_ledger(args: argparse.Namespace, ledger: dict[str, Any]) -> None:
    """Use persisted paths/model on resume unless the caller supplied overrides.

    This keeps resume commands short while still preserving custom artifact
    paths across exits.
    """
    cfg = ledger.get("config") or {}
    path_defaults = {
        "contacts": DEFAULT_CONTACTS,
        "candidates": DEFAULT_CANDIDATES,
        "research_queue": DEFAULT_RESEARCH_QUEUE,
        "research_dir": DEFAULT_RESEARCH_DIR,
        "review_csv": DEFAULT_REVIEW_CSV,
        "retarget_queue": DEFAULT_RETARGET_QUEUE,
        "retarget_ledger": DEFAULT_RETARGET_LEDGER,
        "retarget_research_dir": DEFAULT_RETARGET_RESEARCH_DIR,
    }
    for attr, default in path_defaults.items():
        if cfg.get(attr) and Path(getattr(args, attr)) == default:
            setattr(args, attr, Path(cfg[attr]))
    if cfg.get("model") and args.model == DEFAULT_MODEL:
        args.model = cfg["model"]
    if cfg.get("processor") and args.processor == DEFAULT_PROCESSOR:
        args.processor = cfg["processor"]


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    fill_arg_defaults(args)
    ledger_path = Path(args.ledger)
    ensure_artifact_dirs(args)
    fresh_archive = archive_existing_run_artifacts(args)
    ledger = load_ledger(ledger_path)
    hydrate_args_from_ledger(args, ledger)
    ensure_artifact_dirs(args)
    if args.processor not in ALLOWED_PARALLEL_PROCESSORS:
        raise PipelineFailed(
            f"processor '{args.processor}' is blocked for Powerpacks contact research; "
            f"allowed processors: {', '.join(ALLOWED_PARALLEL_PROCESSORS)}"
        )
    ledger["current_block"] = None
    ledger["config"] = {
        "contacts": str(args.contacts),
        "candidates": str(args.candidates),
        "research_queue": str(args.research_queue),
        "research_dir": str(args.research_dir),
        "review_csv": str(args.review_csv),
        "retarget_queue": str(args.retarget_queue),
        "retarget_ledger": str(args.retarget_ledger),
        "retarget_research_dir": str(args.retarget_research_dir),
        "model": args.model,
        "network_review_model": DEFAULT_NETWORK_REVIEW_MODEL,
        "processor": args.processor,
    }
    if fresh_archive:
        previous_review_csv = archived_artifact(fresh_archive, Path(args.review_csv))
        if previous_review_csv:
            fresh_archive["previous_review_csv"] = previous_review_csv
            ledger.setdefault("artifacts", {})["previous_research_review_csv"] = previous_review_csv
        ledger["fresh_run"] = fresh_archive
        ledger.setdefault("warnings", []).append({
            "step": "fresh_run_start",
            "status": "archived",
            "message": "Started a fresh import run and moved previous run artifacts out of the way.",
            "archive_dir": fresh_archive["archive_dir"],
            "moved_count": fresh_archive["moved_count"],
            "recorded_at": now_iso(),
        })
    save_ledger(ledger_path, ledger)

    extract_imessage(args, ledger_path, ledger)
    normalize_channel(
        args,
        ledger_path,
        ledger,
        step_id="normalize_imessage",
        input_csv=DEFAULT_IMESSAGE_CONTACTS,
        output_jsonl=DEFAULT_IMESSAGE_NORMALIZED,
        manifest=DEFAULT_IMESSAGE_NORMALIZED_MANIFEST,
        force=args.force_imessage,
    )
    extract_whatsapp(args, ledger_path, ledger)
    normalize_channel(
        args,
        ledger_path,
        ledger,
        step_id="normalize_whatsapp",
        input_csv=DEFAULT_WHATSAPP_CONTACTS,
        output_jsonl=DEFAULT_WHATSAPP_NORMALIZED,
        manifest=DEFAULT_WHATSAPP_NORMALIZED_MANIFEST,
        force=args.force_whatsapp,
    )
    ensure_contacts(args, ledger_path, ledger)
    sync_candidates(args, ledger_path, ledger)
    match_contacts(args, ledger_path, ledger)
    llm_review(args, ledger_path, ledger)
    prepare_queue(args, ledger_path, ledger)
    parallel_research(args, ledger_path, ledger)
    build_review_csv(args, ledger_path, ledger)
    if has_research_review(args):
        migrate_review_schema(args, ledger_path, ledger)
        reapply_previous_review_state(args, ledger_path, ledger)
        merge_cached_retarget_results(args, ledger_path, ledger)
        if not completed(ledger, "review_research_web") or args.stop_before_upload:
            open_review_server(args, ledger_path, ledger)
            payload = {
                "primitive": "import_contacts_pipeline",
                "status": "blocked_user_action",
                "message": f"Review opened: {review_url(args)}. When done, say: done with review, upload",
                "review_url": review_url(args),
                "ledger": str(ledger_path),
            }
            ledger["current_block"] = payload
            save_ledger(ledger_path, ledger)
            raise PipelineBlocked(payload, code=21)
    else:
        if completed(ledger, "review_contacts_web_fallback") or args.no_open_review:
            build_raw_review_csv(args, ledger_path, ledger)
            migrate_review_schema(args, ledger_path, ledger)
        else:
            open_raw_contacts_review_server(args, ledger_path, ledger)
            payload = {
                "primitive": "import_contacts_pipeline",
                "status": "blocked_user_action",
                "message": f"Review the web UI at {raw_review_url(args)}. When you're done, tell the agent: 'done with review, upload'. The agent will summarize counts and ask for explicit upload/datalake approval before syncing anything.",
                "review_url": raw_review_url(args),
                "ledger": str(ledger_path),
            }
            ledger["current_block"] = payload
            save_ledger(ledger_path, ledger)
            raise PipelineBlocked(payload, code=21)
    if has_research_review(args):
        migrate_review_schema(args, ledger_path, ledger)
        reapply_previous_review_state(args, ledger_path, ledger)
    retarget_research_after_review(args, ledger_path, ledger)
    upload_review(args, ledger_path, ledger)
    sync_contact_datalake(args, ledger_path, ledger)

    payload = {
        "primitive": "import_contacts_pipeline",
        "status": "completed",
        "ledger": str(ledger_path),
        "artifacts": ledger.get("artifacts", {}),
        "sync": (ledger.get("steps") or {}).get("sync_contact_datalake", {}).get("summary"),
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


def cmd_approve(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    ledger = load_ledger(ledger_path)
    current = ledger.get("current_block") or {}
    aid = args.approval_id or current.get("approval_id")
    if not aid:
        emit({"primitive": "import_contacts_pipeline", "command": "approve", "status": "failed", "error": "no approval_id provided and no current block"})
        return 1
    kind = args.kind or current.get("approval_type")
    if not kind:
        emit({"primitive": "import_contacts_pipeline", "command": "approve", "status": "failed", "error": "no approval type in current block"})
        return 1
    if kind not in aid and current.get("approval_type") and current.get("approval_type") != kind:
        emit({"primitive": "import_contacts_pipeline", "command": "approve", "status": "failed", "error": f"approval type mismatch: requested {args.kind}, current {current.get('approval_type')}"})
        return 1
    ledger.setdefault("approvals", {})[aid] = {
        "approval_id": aid,
        "type": kind,
        "confirmed": True,
        "approved_at": now_iso(),
        "approved_by": "user_confirmed_in_agent_chat",
        "payload": current.get("payload") if current.get("approval_id") == aid else {},
    }
    if current.get("approval_id") == aid:
        ledger["current_block"] = None
    save_ledger(ledger_path, ledger)
    emit({"primitive": "import_contacts_pipeline", "command": "approve", "status": "ok", "approval_id": aid, "type": kind, "ledger": str(ledger_path)})
    return 0


def add_hidden_arg(parser: argparse.ArgumentParser, *names: str, **kwargs: Any) -> None:
    kwargs.setdefault("help", argparse.SUPPRESS)
    parser.add_argument(*names, **kwargs)


def add_pipeline_args(parser: argparse.ArgumentParser) -> None:
    add_hidden_arg(parser, "--ledger", type=Path, default=DEFAULT_LEDGER)
    add_hidden_arg(parser, "--contacts", type=Path, default=DEFAULT_CONTACTS)
    add_hidden_arg(parser, "--candidates", type=Path, default=DEFAULT_CANDIDATES)
    add_hidden_arg(parser, "--research-queue", type=Path, default=DEFAULT_RESEARCH_QUEUE)
    add_hidden_arg(parser, "--research-dir", type=Path, default=DEFAULT_RESEARCH_DIR)
    add_hidden_arg(parser, "--review-csv", type=Path, default=DEFAULT_REVIEW_CSV)
    add_hidden_arg(parser, "--retarget-queue", type=Path, default=DEFAULT_RETARGET_QUEUE)
    add_hidden_arg(parser, "--retarget-ledger", type=Path, default=DEFAULT_RETARGET_LEDGER)
    add_hidden_arg(parser, "--retarget-research-dir", type=Path, default=DEFAULT_RETARGET_RESEARCH_DIR)
    add_hidden_arg(parser, "--model", default=DEFAULT_MODEL)
    add_hidden_arg(parser, "--processor", default=DEFAULT_PROCESSOR, choices=ALLOWED_PARALLEL_PROCESSORS)
    add_hidden_arg(parser, "--llm-auto-approve-usd", type=float, default=DEFAULT_LLM_AUTO_APPROVE_USD)
    add_hidden_arg(parser, "--llm-batch-size", type=int, default=20)
    add_hidden_arg(parser, "--llm-max-workers", type=int, default=4)
    add_hidden_arg(parser, "--env-file", default=".env")
    add_hidden_arg(parser, "--timeout", type=int, default=300)
    add_hidden_arg(parser, "--parallel-timeout", type=int, default=7600)
    add_hidden_arg(parser, "--retarget-harness", default="auto", choices=("auto", "codex", "claude", "manual", "off"))
    add_hidden_arg(parser, "--retarget-harness-threshold", type=int, default=DEFAULT_RETARGET_HARNESS_THRESHOLD)
    add_hidden_arg(parser, "--retarget-harness-timeout", type=int, default=900)
    add_hidden_arg(parser, "--retarget-harness-max-workers", type=int, default=DEFAULT_RETARGET_HARNESS_MAX_WORKERS)
    add_hidden_arg(parser, "--retarget-harness-prompt-dir", type=Path, default=DEFAULT_RETARGET_HARNESS_PROMPT_DIR)
    add_hidden_arg(parser, "--retarget-rapidapi-max-workers", type=int, default=10)
    add_hidden_arg(parser, "--review-host", default="127.0.0.1")
    add_hidden_arg(parser, "--review-port", type=int, default=DEFAULT_REVIEW_PORT)
    parser.set_defaults(open_browser=True)
    add_hidden_arg(parser, "--open-browser", dest="open_browser", action="store_true")
    add_hidden_arg(parser, "--no-open-browser", dest="open_browser", action="store_false")
    add_hidden_arg(parser, "--no-open-review", action="store_true")
    add_hidden_arg(parser, "--stop-before-upload", action="store_true")
    add_hidden_arg(parser, "--force-imessage", action="store_true")
    add_hidden_arg(parser, "--force-whatsapp", action="store_true")
    add_hidden_arg(parser, "--force-sync-candidates", action="store_true")
    add_hidden_arg(parser, "--force-match", action="store_true")
    add_hidden_arg(parser, "--force-prepare-queue", action="store_true")
    add_hidden_arg(parser, "--force-build-review", action="store_true")
    add_hidden_arg(parser, "--rerun-llm", action="store_true")
    add_hidden_arg(parser, "--rerun-parallel", action="store_true")
    add_hidden_arg(parser, "--rerun-upload", action="store_true")


def main() -> int:
    parser = argparse.ArgumentParser(description="Resumable import contacts pipeline orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run until completed or blocked on approval/user action")
    add_pipeline_args(run)
    run.set_defaults(func=cmd_run)

    cont = sub.add_parser("continue", help="Resume from the ledger")
    add_pipeline_args(cont)
    cont.set_defaults(func=cmd_run)

    approve = sub.add_parser("approve", help="Approve the current blocked gate")
    approve.add_argument("kind", nargs="?", choices=["llm", "parallel", "upload"], help=argparse.SUPPRESS)
    add_hidden_arg(approve, "--ledger", type=Path, default=DEFAULT_LEDGER)
    add_hidden_arg(approve, "--approval-id")
    add_hidden_arg(approve, "--confirm", action="store_true")
    approve.set_defaults(func=cmd_approve)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
