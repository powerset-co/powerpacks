#!/usr/bin/env python3
"""Orchestrate local network ingestion sources into merged CSVs + DuckDB.

Sources handled here:
- LinkedIn Connections.csv -> linkedin_network_import
- Gmail msgvault SQLite -> gmail_network_import msgvault
- Existing message contacts and Twitter artifacts are picked up by merge discovery

This orchestrator is local-first. It does not upload or mutate production. Child
primitives remain responsible for their own approval gates.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LEDGER = Path(".powerpacks/network-import/import-network-run.json")
DEFAULT_BASE_DIR = Path(".powerpacks/network-import")
DEFAULT_MSGVAULT_DB = Path.home() / ".msgvault" / "msgvault.db"
DEFAULT_CHILD_TIMEOUT_SECONDS = int(os.environ.get("POWERPACKS_IMPORT_NETWORK_CHILD_TIMEOUT_SECONDS", str(6 * 60 * 60)))


SOURCE_NAMES = ["gmail", "linkedin_csv", "twitter", "messages"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def emit_progress(message: str) -> None:
    print(f"[import-network] {message}", file=sys.stderr, flush=True)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def collect_artifact_paths(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            paths.extend(collect_artifact_paths(item))
    elif isinstance(value, list):
        for item in value:
            paths.extend(collect_artifact_paths(item))
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith(".powerpacks/") or text.startswith("/"):
            paths.append(text)
    return paths


def check_artifact_paths(ledger: dict[str, Any]) -> dict[str, Any]:
    seen: set[str] = set()
    existing = 0
    missing: list[str] = []
    for path_text in collect_artifact_paths({"artifacts": ledger.get("artifacts", {}), "steps": ledger.get("steps", {})}):
        if path_text in seen:
            continue
        seen.add(path_text)
        path = Path(path_text)
        if path.exists():
            existing += 1
        else:
            missing.append(path_text)
    return {"checked": len(seen), "existing": existing, "missing": missing[:50], "missing_count": len(missing)}


def unique_strings(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw = [values]
    elif isinstance(values, list):
        raw = values
    else:
        raw = [values]
    out: list[str] = []
    for value in raw:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def source_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip().lower()).strip("-._")
    return slug or "source"


def truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def skip_msgvault_sync(input_cfg: dict[str, Any]) -> bool:
    return bool(input_cfg.get("skip_msgvault_sync") or truthy_env("POWERPACKS_SKIP_MSGVAULT_SYNC"))


def ordered_records(records: list[dict[str, Any]], account_order: list[str] | None = None) -> list[dict[str, Any]]:
    order = {email: index for index, email in enumerate(account_order or []) if email}
    return sorted(
        records,
        key=lambda record: (
            order.get(str(record.get("account_email") or ""), len(order)),
            str(record.get("account_email") or record.get("slug") or record.get("people_csv") or record.get("queue_csv") or ""),
        ),
    )


def account_channels(path: str) -> dict[str, Any]:
    if not path:
        return {}
    data = read_json(Path(path), {}) or {}
    channels = data.get("accounts") or data.get("channels") or {}
    return channels if isinstance(channels, dict) else {}


def account_record_is_linked(record: dict[str, Any]) -> bool:
    """Return true for both v2 boolean records and handoff/status records.

    Registry v2 writes explicit ``linked`` / ``skipped`` booleans, while setup
    summaries and older tests may use ``status: linked``. Legacy v1 registries
    have neither field and are considered linked when they carry source values.
    """
    if not isinstance(record, dict) or record.get("skipped") is True:
        return False
    linked = record.get("linked")
    if isinstance(linked, bool):
        return linked
    status = record.get("status")
    if isinstance(status, str):
        return status == "linked"
    cfg = record.get("config") if isinstance(record.get("config"), dict) else {}
    return bool(record.get("usernames") or record.get("artifacts") or any(cfg.values()))


def gmail_record_has_import_identity(record: dict[str, Any]) -> bool:
    if not isinstance(record, dict):
        return False
    cfg = record.get("config") if isinstance(record.get("config"), dict) else {}
    return bool(cfg.get("selected_accounts") or cfg.get("account_emails") or record.get("usernames") or record.get("artifacts"))


def extract_accounts_path_from_setup(path: str) -> str:
    if not path:
        return ""
    data = read_json(Path(path), {}) or {}
    for key in ["accounts", "accounts_path"]:
        value = data.get(key)
        if isinstance(value, str):
            return value
    handoff = data.get("handoff") if isinstance(data.get("handoff"), dict) else {}
    for key in ["accounts", "accounts_path"]:
        value = handoff.get(key)
        if isinstance(value, str):
            return value
    commands = handoff.get("commands") if isinstance(handoff.get("commands"), dict) else {}
    cmd = str(commands.get("import_network_run") or "")
    if "--from-accounts" in cmd:
        parts = cmd.split()
        try:
            return parts[parts.index("--from-accounts") + 1]
        except (ValueError, IndexError):
            return ""
    return ""


def apply_account_sources(args: argparse.Namespace) -> argparse.Namespace:
    accounts_path = str(getattr(args, "from_accounts", "") or "").strip()
    if not accounts_path:
        accounts_path = extract_accounts_path_from_setup(str(getattr(args, "from_setup", "") or "").strip())
        if accounts_path:
            setattr(args, "from_accounts", accounts_path)
    channels = account_channels(accounts_path)
    gmail = channels.get("gmail") if isinstance(channels.get("gmail"), dict) else {}
    if gmail and (not account_record_is_linked(gmail) or not gmail_record_has_import_identity(gmail)):
        gmail = {}
    gmail_cfg = gmail.get("config") if isinstance(gmail.get("config"), dict) else {}
    linkedin = channels.get("linkedin_csv") if isinstance(channels.get("linkedin_csv"), dict) else {}
    if linkedin and not account_record_is_linked(linkedin):
        linkedin = {}
    linkedin_cfg = linkedin.get("config") if isinstance(linkedin.get("config"), dict) else {}
    twitter = channels.get("twitter") if isinstance(channels.get("twitter"), dict) else {}
    if twitter and not account_record_is_linked(twitter):
        twitter = {}
    twitter_cfg = twitter.get("config") if isinstance(twitter.get("config"), dict) else {}
    messages = channels.get("messages") if isinstance(channels.get("messages"), dict) else {}
    if messages and not account_record_is_linked(messages):
        messages = {}
    messages_cfg = messages.get("config") if isinstance(messages.get("config"), dict) else {}

    if not getattr(args, "msgvault_db", "") and gmail_cfg.get("msgvault_db"):
        args.msgvault_db = str(gmail_cfg.get("msgvault_db") or "")
    emails = unique_strings(getattr(args, "gmail_account_emails", []))
    if getattr(args, "gmail_account_email", ""):
        emails = unique_strings([args.gmail_account_email, *emails])
    if not emails:
        emails = unique_strings(gmail_cfg.get("selected_accounts") or gmail_cfg.get("account_emails") or gmail.get("usernames"))
    args.gmail_account_emails = emails
    args.gmail_account_email = args.gmail_account_email or (emails[0] if len(emails) == 1 else "")
    if not getattr(args, "linkedin_csv", ""):
        args.linkedin_csv = str(linkedin_cfg.get("csv_path") or "")
        if not args.linkedin_csv and linkedin.get("artifacts"):
            args.linkedin_csv = str((linkedin.get("artifacts") or [""])[0])
    if not getattr(args, "linkedin_source_user", ""):
        args.linkedin_source_user = str(linkedin_cfg.get("source_label") or "")
        if not args.linkedin_source_user and linkedin.get("usernames"):
            args.linkedin_source_user = str((linkedin.get("usernames") or [""])[0])
    if not getattr(args, "twitter_handle", ""):
        args.twitter_handle = str(twitter_cfg.get("handle") or "")
        if not args.twitter_handle and twitter.get("usernames"):
            args.twitter_handle = str((twitter.get("usernames") or [""])[0])
    if not getattr(args, "messages_contacts_csv", ""):
        args.messages_contacts_csv = str(messages_cfg.get("contacts_csv") or "")
        if not args.messages_contacts_csv and messages.get("artifacts"):
            args.messages_contacts_csv = str((messages.get("artifacts") or [""])[0])
    return args


def resolve_msgvault_db(args: argparse.Namespace) -> str:
    explicit = str(getattr(args, "msgvault_db", "") or "").strip()
    if explicit:
        return explicit
    if str(getattr(args, "gmail_account_email", "") or "").strip() or unique_strings(getattr(args, "gmail_account_emails", [])):
        return str(DEFAULT_MSGVAULT_DB)
    return ""


def parse_last_json(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    idx = 0
    last: dict[str, Any] = {}
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        try:
            value, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        if isinstance(value, dict):
            last = value
        idx = end
    return last


def run_cmd(cmd: list[str], *, timeout: int | None = None) -> tuple[int, dict[str, Any], str]:
    effective_timeout = DEFAULT_CHILD_TIMEOUT_SECONDS if timeout is None else timeout
    proc = subprocess.Popen(
        cmd,
        cwd=Path(__file__).resolve().parents[4],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def read_stdout() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            stdout_chunks.append(line)

    def read_stderr() -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            stderr_chunks.append(line)
            sys.stderr.write(line)
            sys.stderr.flush()

    threads = [
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=read_stderr, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        code = proc.wait(timeout=effective_timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
        timeout_message = f"child command timed out after {effective_timeout} seconds: {' '.join(cmd)}"
        stderr_chunks.append(timeout_message + "\n")
        emit_progress(timeout_message)
    for thread in threads:
        thread.join(timeout=1)
    return code, parse_last_json("".join(stdout_chunks)), "".join(stderr_chunks)


def py_cmd(script: str, *args: str) -> list[str]:
    return [sys.executable, script, *args]


def load_ledger(path: Path) -> dict[str, Any]:
    ledger = read_json(path, {}) or {}
    ledger.setdefault("primitive", "import_network_pipeline")
    ledger.setdefault("version", 1)
    ledger.setdefault("created_at", now_iso())
    ledger.setdefault("updated_at", now_iso())
    ledger.setdefault("steps", {})
    ledger.setdefault("artifacts", {})
    return ledger


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = now_iso()
    write_json(path, ledger)


def mark_step(ledger: dict[str, Any], step: str, status: str, **extra: Any) -> None:
    rec = ledger.setdefault("steps", {}).setdefault(step, {"id": step})
    if status == "running" and "started_at" not in rec:
        rec["started_at"] = now_iso()
    if status in {"completed", "failed", "blocked", "skipped"}:
        rec["finished_at"] = now_iso()
    rec["status"] = status
    rec.update({k: v for k, v in extra.items() if v is not None})


def begin_step(ledger_path: Path, ledger: dict[str, Any], step: str, message: str) -> None:
    mark_step(ledger, step, "running")
    save_ledger(ledger_path, ledger)
    emit_progress(message)


def run_linkedin_child(ledger: dict[str, Any], mode: str) -> dict[str, Any]:
    input_cfg = ledger.get("input", {})
    child_ledger = Path(ledger["run_dir"]) / "linkedin.ledger.json"
    if mode == "run":
        cmd = py_cmd(
            "packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py",
            "run",
            "--csv", input_cfg["linkedin_csv"],
            "--source-user", input_cfg.get("linkedin_source_user") or "local",
            "--operator-id", input_cfg.get("operator_id") or "local",
            "--output-dir", str(DEFAULT_BASE_DIR),
            "--ledger", str(child_ledger),
            "--run-id", f"{ledger['run_id']}-linkedin",
            "--force",
        )
        if input_cfg.get("linkedin_limit") is not None:
            cmd.extend(["--limit", str(input_cfg["linkedin_limit"])])
    else:
        cmd = py_cmd("packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py", "continue", "--ledger", str(child_ledger))
    code, payload, stderr = run_cmd(cmd)
    return {"id": "linkedin_csv", "source": "linkedin_csv", "child_ledger": str(child_ledger), "command": cmd, "code": code, "payload": payload, "stderr": stderr}


def record_linkedin_worker_result(ledger_path: Path, ledger: dict[str, Any], result: dict[str, Any]) -> bool:
    code = int(result.get("code") or 0)
    payload = result.get("payload") or {}
    stderr = result.get("stderr") or ""
    child_ledger = result.get("child_ledger") or str(Path(ledger["run_dir"]) / "linkedin.ledger.json")
    ledger.setdefault("artifacts", {})["linkedin_ledger"] = str(child_ledger)
    if code == 20 or payload.get("status") == "blocked_approval":
        ledger["blocked"] = {"step_id": "linkedin", "child_ledger": str(child_ledger), "child": payload}
        mark_step(ledger, "linkedin", "blocked", payload=payload)
        save_ledger(ledger_path, ledger)
        emit({"status": "blocked_approval", "step_id": "linkedin", "ledger": str(ledger_path), "child": payload})
        return False
    if code != 0:
        mark_step(ledger, "linkedin", "failed", error=stderr or payload.get("error") or payload)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "linkedin", "error": stderr or payload})
        return False
    mark_step(ledger, "linkedin", "completed", payload=payload)
    for key, value in (payload.get("artifacts") or {}).items():
        ledger.setdefault("artifacts", {})[f"linkedin_{key}"] = value
    emit_progress("LinkedIn import completed.")
    return True


def run_linkedin(ledger_path: Path, ledger: dict[str, Any], mode: str) -> bool:
    input_cfg = ledger.get("input", {})
    if not input_cfg.get("linkedin_csv"):
        mark_step(ledger, "linkedin", "skipped", reason="no --linkedin-csv")
        return True
    begin_step(ledger_path, ledger, "linkedin", "Importing LinkedIn CSV and enriching profiles.")
    return record_linkedin_worker_result(ledger_path, ledger, run_linkedin_child(ledger, mode))


def run_gmail_msgvault_account(ledger: dict[str, Any], email: str, index: int = 0) -> dict[str, Any]:
    input_cfg = ledger.get("input", {})
    db = input_cfg.get("msgvault_db") or str(DEFAULT_MSGVAULT_DB)
    run_id = f"{ledger['run_id']}-gmail-{source_slug(email or 'all') or index}"
    sync_command: list[str] = []
    sync_skipped_reason = ""
    if email and skip_msgvault_sync(input_cfg):
        sync_skipped_reason = "msgvault sync skipped by configuration; using existing msgvault DB"
    elif email and shutil.which("msgvault"):
        sync_cmd = ["msgvault"]
        db_home = Path(db).expanduser().parent
        default_home = Path(DEFAULT_MSGVAULT_DB).expanduser().parent
        if db_home != default_home:
            sync_cmd.extend(["--home", str(db_home)])
        sync_cmd.extend(["sync-full", email])
        sync_command = sync_cmd
        sync_code, sync_payload, sync_stderr = run_cmd(sync_cmd)
        if sync_code != 0:
            return {
                "id": f"gmail:{email}",
                "source": "gmail",
                "account_email": email,
                "run_id": run_id,
                "sync_command": sync_cmd,
                "code": sync_code,
                "payload": sync_payload,
                "stderr": sync_stderr,
                "phase": "msgvault_sync",
            }
    elif email:
        sync_skipped_reason = "msgvault command not found; using existing msgvault DB if present"
    cmd = py_cmd(
        "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py",
        "msgvault",
        "--db", db,
        "--operator-id", input_cfg.get("operator_id") or "local",
        "--run-id", run_id,
    )
    if email:
        cmd.extend(["--account-email", email])
    if input_cfg.get("gmail_limit") is not None:
        cmd.extend(["--limit", str(input_cfg["gmail_limit"])])
    if input_cfg.get("include_automated_gmail"):
        cmd.append("--include-automated")
    code, payload, stderr = run_cmd(cmd)
    return {"id": f"gmail:{email or 'all'}", "source": "gmail", "account_email": email, "run_id": run_id, "sync_command": sync_command, "sync_skipped_reason": sync_skipped_reason, "command": cmd, "code": code, "payload": payload, "stderr": stderr, "phase": "gmail_network_import"}


def record_gmail_worker_result(ledger: dict[str, Any], result: dict[str, Any]) -> bool:
    email = result.get("account_email") or "all"
    step_id = f"gmail_msgvault:{source_slug(email)}"
    payload = result.get("payload") or {}
    code = int(result.get("code") or 0)
    if code != 0:
        mark_step(ledger, step_id, "failed", error=result.get("stderr") or payload.get("error") or payload, account_email=email, phase=result.get("phase"))
        ledger["status"] = "failed"
        return False
    mark_step(ledger, step_id, "completed", payload=payload, account_email=email, sync_command=result.get("sync_command"), sync_skipped_reason=result.get("sync_skipped_reason"))
    ledger.setdefault("source_imports", {})[step_id] = {"status": "completed", "source": "gmail", "account_email": email, "run_id": result.get("run_id"), "sync_command": result.get("sync_command"), "sync_skipped_reason": result.get("sync_skipped_reason")}
    slug = source_slug(email)
    people_csv = ""
    for key, value in (payload.get("artifacts") or {}).items():
        ledger.setdefault("artifacts", {})[f"gmail_{slug}_{key}"] = value
        if key == "people_csv":
            people_csv = str(value or "")
            ledger.setdefault("artifacts", {})["gmail_people_csv"] = value
            ledger.setdefault("artifacts", {}).setdefault("gmail_people_csvs", []).append(value)
            ledger.setdefault("artifacts", {}).setdefault("gmail_people_records", []).append({"account_email": email, "people_csv": people_csv, "slug": slug})
    for key, value in (payload.get("artifacts") or {}).items():
        if key == "linkedin_resolution_queue_csv":
            queue_record = {"account_email": email, "queue_csv": value, "people_csv": people_csv, "slug": slug}
            ledger.setdefault("artifacts", {}).setdefault("gmail_linkedin_resolution_queue_csvs", []).append(queue_record)
            if "gmail_linkedin_resolution_queue_csv" not in ledger.setdefault("artifacts", {}):
                ledger["artifacts"]["gmail_linkedin_resolution_queue_csv"] = value
    counts = payload.get("counts") or {}
    if counts:
        emit_progress(f"Gmail metadata import completed for {email}: {counts.get('contacts_written', 0)} contacts from {counts.get('contacts_seen', 0)} seen.")
    else:
        emit_progress(f"Gmail metadata import completed for {email}.")
    return True


def run_gmail_msgvault(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    input_cfg = ledger.get("input", {})
    emails = unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email"))
    if not emails and input_cfg.get("msgvault_db"):
        emails = [""]
    if not emails:
        mark_step(ledger, "gmail_msgvault", "skipped", reason="no Gmail account emails/msgvault DB")
        return True
    begin_step(ledger_path, ledger, "gmail_msgvault", f"Importing Gmail metadata for {len(emails)} msgvault account(s).")
    ok = True
    max_workers = min(len(emails), int(os.environ.get("POWERPACKS_IMPORT_NETWORK_GMAIL_MAX_WORKERS", "4"))) or 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(run_gmail_msgvault_account, ledger, email, index) for index, email in enumerate(emails)]
        results = [future.result() for future in futures]
    for result in results:
        if not record_gmail_worker_result(ledger, result):
            ok = False
    if ok:
        mark_step(ledger, "gmail_msgvault", "completed", accounts=emails, parallelizable=True)
    return ok


def source_worker_group(input_cfg: dict[str, Any], run_id: str) -> dict[str, Any]:
    jobs: list[dict[str, Any]] = []
    gmail_emails = unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email"))
    if gmail_emails or input_cfg.get("msgvault_db"):
        emails = gmail_emails or [""]
        for email in emails:
            jobs.append({
                "id": f"gmail:{email or 'all'}",
                "source": "gmail",
                "account_email": email,
                "step_id": f"gmail_msgvault:{source_slug(email or 'all')}",
                "artifact_root": str(Path(DEFAULT_BASE_DIR) / "gmail" / f"{run_id}-gmail-{source_slug(email or 'all')}"),
                "parallelizable": True,
                "reason": "local msgvault metadata read with isolated output run id",
            })
    if input_cfg.get("linkedin_csv"):
        jobs.append({
            "id": "linkedin_csv",
            "source": "linkedin_csv",
            "step_id": "linkedin",
            "ledger": str(Path(DEFAULT_BASE_DIR) / "network-runs" / run_id / "linkedin.ledger.json"),
            "artifact_root": str(Path(DEFAULT_BASE_DIR) / "linkedin" / f"{run_id}-linkedin"),
            "parallelizable": True,
            "reason": "CSV conversion/enrichment uses its own child ledger and cache gates",
            "requires_approval": ["rapidapi_linkedin_profile_enrichment"],
        })
    if input_cfg.get("twitter_handle"):
        jobs.append({
            "id": "twitter",
            "source": "twitter",
            "handle": input_cfg.get("twitter_handle"),
            "parallelizable": True,
            "reason": "twitter import has no dependency on Gmail/LinkedIn but remains approval-gated",
            "requires_approval": ["rapidapi_twitter", "openai_moe", "rapidapi_linkedin_validation"],
            "status": "existing_artifacts_or_explicit_import_required",
        })
    if input_cfg.get("messages_contacts_csv") or input_cfg.get("include_existing_artifacts"):
        jobs.append({
            "id": "messages",
            "source": "messages",
            "contacts_csv": input_cfg.get("messages_contacts_csv") or ".powerpacks/messages/contacts.csv",
            "parallelizable": True,
            "reason": "messages/iMessage/WhatsApp artifacts are merged when present; research/upload is not implicit",
            "requires_approval": ["whatsapp_qr", "messages_research_upload"],
            "status": "existing_artifacts_only",
        })
    return {"parallel": True, "fan_in": "merge_network_sources_then_duckdb_after_nonblocked_workers", "jobs": jobs}


def run_source_import_workers(ledger_path: Path, ledger: dict[str, Any], *, resume: bool = False) -> bool:
    input_cfg = ledger.get("input", {})
    group = source_worker_group(input_cfg, ledger["run_id"])
    ledger["worker_groups"] = {"import": group}
    selected = set(unique_strings(input_cfg.get("only_sources")))
    runnable_sources = {"gmail", "linkedin_csv"}
    if selected:
        runnable_sources &= selected
    mark_step(ledger, "source_imports", "running", worker_group=group)
    save_ledger(ledger_path, ledger)
    futures: dict[concurrent.futures.Future[dict[str, Any]], str] = {}
    gmail_emails = unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email"))
    if not gmail_emails and input_cfg.get("msgvault_db"):
        gmail_emails = [""]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, len(gmail_emails) + (1 if input_cfg.get("linkedin_csv") else 0) or 1))) as executor:
        if "linkedin_csv" in runnable_sources and input_cfg.get("linkedin_csv") and ledger.get("steps", {}).get("linkedin", {}).get("status") not in {"completed", "skipped"}:
            futures[executor.submit(run_linkedin_child, ledger, "continue" if resume else "run")] = "linkedin_csv"
        if "gmail" in runnable_sources and ledger.get("steps", {}).get("gmail_msgvault", {}).get("status") not in {"completed", "skipped"} and gmail_emails:
            begin_step(ledger_path, ledger, "gmail_msgvault", f"Importing Gmail metadata for {len(gmail_emails)} msgvault account(s).")
            for index, email in enumerate(gmail_emails):
                futures[executor.submit(run_gmail_msgvault_account, ledger, email, index)] = "gmail"
        for future in concurrent.futures.as_completed(futures):
            source = futures[future]
            result = future.result()
            if source == "linkedin_csv":
                if not record_linkedin_worker_result(ledger_path, ledger, result):
                    mark_step(ledger, "source_imports", "blocked" if ledger.get("blocked") else "failed", worker_group=group)
                    save_ledger(ledger_path, ledger)
                    return False
            elif source == "gmail":
                if not record_gmail_worker_result(ledger, result):
                    emit({"status": "failed", "step_id": f"gmail_msgvault:{source_slug(result.get('account_email') or 'all')}", "error": result.get("stderr") or result.get("payload")})
                    mark_step(ledger, "source_imports", "failed", worker_group=group)
                    save_ledger(ledger_path, ledger)
                    return False
                save_ledger(ledger_path, ledger)
    if gmail_emails and "gmail" in runnable_sources:
        mark_step(ledger, "gmail_msgvault", "completed", accounts=gmail_emails, parallelizable=True)
    # Mark skipped sources after parallel fan-out finishes.
    if "linkedin_csv" in runnable_sources and not input_cfg.get("linkedin_csv"):
        mark_step(ledger, "linkedin", "skipped", reason="no --linkedin-csv")
    if "gmail" in runnable_sources and not (unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")) or input_cfg.get("msgvault_db")):
        mark_step(ledger, "gmail_msgvault", "skipped", reason="no Gmail account emails/msgvault DB")
    for source in ["twitter", "messages"]:
        if not selected or source in selected:
            if source == "twitter" and not input_cfg.get("twitter_handle"):
                continue
            if source == "messages" and not (input_cfg.get("messages_contacts_csv") or input_cfg.get("include_existing_artifacts")):
                continue
            if source == "messages" and input_cfg.get("messages_contacts_csv"):
                ledger.setdefault("artifacts", {})["messages_contacts_csv"] = input_cfg.get("messages_contacts_csv")
                mark_step(ledger, source, "completed", reason="linked contacts CSV will be included in fan-in merge", contacts_csv=input_cfg.get("messages_contacts_csv"))
            else:
                mark_step(ledger, source, "skipped", reason="existing artifacts only; dedicated import skill owns crawl/research/upload")
    mark_step(ledger, "source_imports", "completed", worker_group=group)
    save_ledger(ledger_path, ledger)
    return True


def run_gmail_linkedin_resolution(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    input_cfg = ledger.get("input", {})
    provider = input_cfg.get("gmail_linkedin_provider") or "off"
    artifacts = ledger.setdefault("artifacts", {})
    queue_records = artifacts.get("gmail_linkedin_resolution_queue_csvs") or []
    if not queue_records and artifacts.get("gmail_linkedin_resolution_queue_csv"):
        queue_records = [{"account_email": "", "queue_csv": artifacts.get("gmail_linkedin_resolution_queue_csv"), "people_csv": artifacts.get("gmail_people_csv"), "slug": "all"}]
    if provider == "off" or not queue_records:
        mark_step(ledger, "gmail_linkedin_resolution", "skipped", reason="provider off or no queue")
        return True
    queue_records = ordered_records([record for record in queue_records if isinstance(record, dict)], unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")))
    by_slug = artifacts.setdefault("gmail_linkedin_resolutions_by_slug", {})
    artifacts["gmail_linkedin_resolutions_csvs"] = []
    artifacts["gmail_linkedin_resolution_ledgers"] = []
    begin_step(ledger_path, ledger, "gmail_linkedin_resolution", f"Resolving Gmail contacts to LinkedIn for {len(queue_records)} account queue(s).")
    results = []
    for index, record in enumerate(queue_records):
        queue = record.get("queue_csv")
        if not queue:
            continue
        slug = source_slug(record.get("account_email") or record.get("slug") or f"queue-{index}")
        existing = by_slug.get(slug) if isinstance(by_slug.get(slug), dict) else {}
        if existing.get("resolutions_csv"):
            artifacts["gmail_linkedin_resolutions_csvs"].append(existing)
            if existing.get("ledger"):
                artifacts["gmail_linkedin_resolution_ledgers"].append(existing["ledger"])
            results.append(existing)
            continue
        child_ledger = Path(ledger["run_dir"]) / f"gmail-linkedin-resolution.{slug}.ledger.json"
        out_dir = Path(ledger["run_dir"]) / f"gmail-linkedin-resolution-{slug}"
        cmd = py_cmd(
            "packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py",
            "run",
            "--provider", provider,
            "--input", str(queue),
            "--output-dir", str(out_dir),
            "--ledger", str(child_ledger),
        )
        if input_cfg.get("gmail_linkedin_limit") is not None:
            cmd.extend(["--limit", str(input_cfg["gmail_linkedin_limit"])])
        code, payload, stderr = run_cmd(cmd)
        artifacts.setdefault("gmail_linkedin_resolution_ledgers", []).append(str(child_ledger))
        if "gmail_linkedin_resolution_ledger" not in artifacts:
            artifacts["gmail_linkedin_resolution_ledger"] = str(child_ledger)
        if code == 20 or payload.get("status") == "blocked_approval":
            ledger["blocked"] = {"step_id": "gmail_linkedin_resolution", "child_ledger": str(child_ledger), "child": payload, "account_email": record.get("account_email") if isinstance(record, dict) else ""}
            mark_step(ledger, "gmail_linkedin_resolution", "blocked", payload=payload)
            save_ledger(ledger_path, ledger)
            emit({"status": "blocked_approval", "step_id": "gmail_linkedin_resolution", "ledger": str(ledger_path), "child": payload})
            return False
        if code != 0:
            mark_step(ledger, "gmail_linkedin_resolution", "failed", error=stderr or payload)
            ledger["status"] = "failed"
            save_ledger(ledger_path, ledger)
            emit({"status": "failed", "step_id": "gmail_linkedin_resolution", "error": stderr or payload})
            return False
        result = dict(record)
        result.update({"payload": payload, "resolutions_csv": payload.get("output"), "ledger": str(child_ledger)})
        results.append(result)
        if payload.get("output"):
            by_slug[slug] = result
            artifacts.setdefault("gmail_linkedin_resolutions_csvs", []).append(result)
            if "gmail_linkedin_resolutions_csv" not in artifacts:
                artifacts["gmail_linkedin_resolutions_csv"] = payload.get("output")
        if payload.get("prompts_jsonl"):
            artifacts.setdefault("gmail_linkedin_harness_prompts_jsonls", []).append(payload.get("prompts_jsonl"))
            artifacts.setdefault("gmail_linkedin_harness_prompts_jsonl", payload.get("prompts_jsonl"))
        if payload.get("instructions"):
            artifacts.setdefault("gmail_linkedin_harness_instructions", payload.get("instructions"))
    mark_step(ledger, "gmail_linkedin_resolution", "completed", payload={"results": results})
    emit_progress("Gmail LinkedIn resolution completed.")
    return True


def run_gmail_apply_and_enrich(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    input_cfg = ledger.get("input", {})
    artifacts = ledger.setdefault("artifacts", {})
    resolution_records = []
    if input_cfg.get("gmail_resolutions_csv"):
        people_csvs = unique_strings(artifacts.get("gmail_final_people_csvs") or artifacts.get("gmail_people_csvs") or artifacts.get("gmail_people_csv"))
        if len(people_csvs) > 1:
            message = "--gmail-resolutions-csv is ambiguous with multiple Gmail people CSVs; provide per-account resolution outputs or run Gmail LinkedIn resolution first."
            mark_step(ledger, "gmail_apply_enrich", "failed", error=message)
            ledger["status"] = "failed"
            save_ledger(ledger_path, ledger)
            emit({"status": "failed", "step_id": "gmail_apply_enrich", "error": message})
            return False
        resolution_records = [{
            "account_email": "",
            "resolutions_csv": input_cfg.get("gmail_resolutions_csv"),
            "people_csv": artifacts.get("gmail_people_csv"),
            "slug": "all",
        }]
    else:
        resolution_records = artifacts.get("gmail_linkedin_resolutions_csvs") or []
        if not resolution_records and artifacts.get("gmail_linkedin_resolutions_csv"):
            resolution_records = [{
                "account_email": "",
                "resolutions_csv": artifacts.get("gmail_linkedin_resolutions_csv"),
                "people_csv": artifacts.get("gmail_people_csv"),
                "slug": "all",
            }]
    resolution_records = [record for record in resolution_records if isinstance(record, dict) and record.get("resolutions_csv") and record.get("people_csv")]
    if not resolution_records:
        mark_step(ledger, "gmail_apply_enrich", "skipped", reason="no gmail resolutions")
        return True
    resolution_records = ordered_records(resolution_records, unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")))
    by_slug = artifacts.setdefault("gmail_apply_enrich_by_slug", {})
    artifacts["gmail_resolved_people_csvs"] = []
    artifacts["gmail_enrich_people_ledgers"] = []
    artifacts["gmail_final_people_csvs"] = []
    begin_step(ledger_path, ledger, "gmail_apply_enrich", f"Applying Gmail LinkedIn matches for {len(resolution_records)} account file(s).")
    results = []
    final_people_csvs = []
    for index, record in enumerate(resolution_records):
        slug = source_slug(record.get("account_email") or record.get("slug") or f"account-{index}")
        existing = by_slug.get(slug) if isinstance(by_slug.get(slug), dict) else {}
        if existing.get("final_people_csv"):
            final_people_csvs.append(existing["final_people_csv"])
            artifacts["gmail_final_people_csvs"].append(existing["final_people_csv"])
            if existing.get("people_csv"):
                artifacts["gmail_resolved_people_csvs"].append(existing["people_csv"])
            if existing.get("enrich_ledger"):
                artifacts["gmail_enrich_people_ledgers"].append(existing["enrich_ledger"])
            results.append(existing)
            continue
        apply_cmd = py_cmd(
            "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py",
            "apply-resolutions",
            "--people-csv", str(record["people_csv"]),
            "--resolutions-csv", str(record["resolutions_csv"]),
            "--output-dir", str(DEFAULT_BASE_DIR),
            "--run-id", f"{ledger['run_id']}-gmail-resolved-{slug}",
        )
        code, payload, stderr = run_cmd(apply_cmd)
        if code != 0:
            mark_step(ledger, "gmail_apply_enrich", "failed", error=stderr or payload)
            ledger["status"] = "failed"
            save_ledger(ledger_path, ledger)
            emit({"status": "failed", "step_id": "gmail_apply_enrich", "error": stderr or payload})
            return False
        resolved_people = payload.get("people_csv") or record["people_csv"]
        artifacts.setdefault("gmail_resolved_people_csvs", []).append(resolved_people)
        artifacts["gmail_resolved_people_csv"] = resolved_people
        result = {"account_email": record.get("account_email", ""), "slug": slug, "apply": payload, "people_csv": resolved_people}
        if int(payload.get("resolved") or 0) > 0:
            emit_progress(f"Enriching {payload.get('resolved')} resolved Gmail LinkedIn profiles for {record.get('account_email') or slug}.")
            child_ledger = Path(ledger["run_dir"]) / f"gmail-enrich-people.{slug}.ledger.json"
            enrich_cmd = py_cmd(
                "packs/ingestion/primitives/enrich_people/enrich_people.py",
                "run",
                "--input", str(resolved_people),
                "--ledger", str(child_ledger),
                "--run-id", f"{ledger['run_id']}-gmail-enrich-{slug}",
            )
            code, enrich_payload, stderr = run_cmd(enrich_cmd)
            artifacts.setdefault("gmail_enrich_people_ledgers", []).append(str(child_ledger))
            artifacts.setdefault("gmail_enrich_people_ledger", str(child_ledger))
            result["enrich_ledger"] = str(child_ledger)
            if code == 20 or enrich_payload.get("status") == "blocked_approval":
                ledger["blocked"] = {"step_id": "gmail_apply_enrich", "child_ledger": str(child_ledger), "child": enrich_payload, "account_email": record.get("account_email", "")}
                mark_step(ledger, "gmail_apply_enrich", "blocked", payload=enrich_payload)
                save_ledger(ledger_path, ledger)
                emit({"status": "blocked_approval", "step_id": "gmail_apply_enrich", "ledger": str(ledger_path), "child": enrich_payload})
                return False
            if code != 0:
                mark_step(ledger, "gmail_apply_enrich", "failed", error=stderr or enrich_payload)
                ledger["status"] = "failed"
                save_ledger(ledger_path, ledger)
                emit({"status": "failed", "step_id": "gmail_apply_enrich", "error": stderr or enrich_payload})
                return False
            for key, value in (enrich_payload.get("artifacts") or {}).items():
                artifacts[f"gmail_{slug}_enriched_{key}"] = value
            if enrich_payload.get("artifacts", {}).get("people_csv"):
                resolved_people = enrich_payload["artifacts"]["people_csv"]
            result["enrich"] = enrich_payload
        final_people_csvs.append(resolved_people)
        artifacts["gmail_people_csv"] = resolved_people
        result["final_people_csv"] = resolved_people
        by_slug[slug] = result
        results.append(result)
    artifacts["gmail_final_people_csvs"] = final_people_csvs
    mark_step(ledger, "gmail_apply_enrich", "completed", payload={"results": results})
    emit_progress("Gmail LinkedIn matches applied and enrichment completed.")
    return True


def run_merge(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    begin_step(ledger_path, ledger, "merge", "Merging network sources.")
    merge_dir = Path(ledger["run_dir"]) / "merged"
    cmd = py_cmd(
        "packs/ingestion/primitives/merge_network_sources/merge_network_sources.py",
        "run",
        "--base-dir", ".powerpacks",
        "--output-dir", str(merge_dir),
    )
    if not ledger.get("input", {}).get("include_existing_artifacts"):
        account_order = unique_strings(ledger.get("input", {}).get("gmail_account_emails") or ledger.get("input", {}).get("gmail_account_email"))
        gmail_inputs = ledger.get("artifacts", {}).get("gmail_final_people_csvs") or []
        if not gmail_inputs and ledger.get("artifacts", {}).get("gmail_people_records"):
            gmail_inputs = [record.get("people_csv") for record in ordered_records(ledger["artifacts"]["gmail_people_records"], account_order)]
        if not gmail_inputs:
            gmail_inputs = sorted(str(path) for path in ledger.get("artifacts", {}).get("gmail_people_csvs", []) if path)
        explicit_inputs = [
            value for key, value in sorted(ledger.get("artifacts", {}).items())
            if key in {"linkedin_people_csv"} and value
        ]
        if gmail_inputs:
            explicit_inputs.extend(str(path) for path in gmail_inputs if path)
        elif ledger.get("artifacts", {}).get("gmail_people_csv"):
            explicit_inputs.append(str(ledger["artifacts"]["gmail_people_csv"]))
        messages_contacts = ledger.get("artifacts", {}).get("messages_contacts_csv") or ledger.get("input", {}).get("messages_contacts_csv")
        if messages_contacts:
            message_input = Path(messages_contacts)
            # `merge_network_sources` recognizes message contact CSVs by a
            # `/messages/contacts.csv` path segment. A linked source may live
            # elsewhere, so copy it into this run's fan-in scratch area before
            # passing it as an explicit input.
            if message_input.exists():
                scratch = merge_dir / "source-inputs" / "messages" / "contacts.csv"
                scratch.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(message_input, scratch)
                explicit_inputs.append(str(scratch))
            else:
                explicit_inputs.append(str(message_input))
        explicit_inputs = list(dict.fromkeys(explicit_inputs))
        for input_path in explicit_inputs:
            cmd.extend(["--input", str(input_path)])
    code, payload, stderr = run_cmd(cmd)
    if code != 0:
        mark_step(ledger, "merge", "failed", error=stderr or payload)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "merge", "error": stderr or payload})
        return False
    mark_step(ledger, "merge", "completed", payload=payload)
    ledger.setdefault("artifacts", {}).update({
        "merged_people_csv": payload.get("people_csv"),
        "network_contacts_csv": payload.get("network_contacts_csv"),
        "network_contact_sources_csv": payload.get("network_contact_sources_csv"),
        "network_companies_csv": payload.get("network_companies_csv"),
        "merge_manifest": payload.get("manifest"),
    })
    emit_progress(f"Merged network sources: {payload.get('merged_rows', 0)} people.")
    return True


def run_duckdb(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    begin_step(ledger_path, ledger, "duckdb", "Building local network DuckDB.")
    merge_dir = Path(ledger["run_dir"]) / "merged"
    duckdb_dir = Path(ledger["run_dir"]) / "duckdb"
    cmd = py_cmd(
        "packs/ingestion/primitives/build_network_duckdb/build_network_duckdb.py",
        "--network-dir", str(merge_dir),
        "--output-dir", str(duckdb_dir),
        "--flavor", ledger["run_id"],
        "--force",
    )
    code, payload, stderr = run_cmd(cmd)
    if code != 0:
        mark_step(ledger, "duckdb", "failed", error=stderr or payload)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "duckdb", "error": stderr or payload})
        return False
    mark_step(ledger, "duckdb", "completed", payload=payload)
    ledger.setdefault("artifacts", {}).update({"duckdb": payload.get("duckdb"), "duckdb_manifest": payload.get("manifest")})
    emit_progress("Local network DuckDB is ready.")
    return True


def run_pipeline(ledger_path: Path, *, resume: bool = False) -> int:
    ledger = load_ledger(ledger_path)
    if not ledger.get("input", {}).get("fan_in_only") and ledger.get("steps", {}).get("source_imports", {}).get("status") not in {"completed", "skipped"}:
        if not run_source_import_workers(ledger_path, ledger, resume=resume):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if ledger.get("input", {}).get("only_sources") and not ledger.get("input", {}).get("fan_in_only"):
        ledger["status"] = "source_import_completed"
        save_ledger(ledger_path, ledger)
        emit({"status": "source_import_completed", "ledger": str(ledger_path), "run_dir": ledger["run_dir"], "steps": ledger.get("steps", {}), "artifacts": ledger.get("artifacts", {})})
        return 0
    if ledger.get("steps", {}).get("gmail_linkedin_resolution", {}).get("status") not in {"completed", "skipped"}:
        if not run_gmail_linkedin_resolution(ledger_path, ledger):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if ledger.get("steps", {}).get("gmail_apply_enrich", {}).get("status") not in {"completed", "skipped"}:
        if not run_gmail_apply_and_enrich(ledger_path, ledger):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if not run_merge(ledger_path, ledger):
        return 1
    save_ledger(ledger_path, ledger)
    if not run_duckdb(ledger_path, ledger):
        return 1
    ledger["status"] = "completed"
    ledger.pop("blocked", None)
    save_ledger(ledger_path, ledger)
    emit({"status": "completed", "ledger": str(ledger_path), "run_dir": ledger["run_dir"], "artifacts": ledger.get("artifacts", {})})
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    args = apply_account_sources(args)
    run_id = args.run_id or f"network-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    run_dir = DEFAULT_BASE_DIR / "network-runs" / run_id
    ledger_path = Path(args.ledger)
    if args.dry_run or args.estimate:
        emit(dry_run_plan(args, ledger_path, run_id, run_dir))
        return 0
    if ledger_path.exists() and not args.force:
        existing = load_ledger(ledger_path)
        if existing.get("status") == "completed":
            emit({
                "status": "completed",
                "cached": True,
                "ledger": str(ledger_path),
                "run_dir": existing.get("run_dir"),
                "message": "Existing completed import-network run found; no work was run.",
                "artifact_check": check_artifact_paths(existing),
                "artifacts": existing.get("artifacts", {}),
            })
            return 0
        if existing.get("status") not in {"failed"}:
            emit({"status": "active_run_exists", "ledger": str(ledger_path), "message": "Use continue/approve or --force."})
            return 0
    ledger = {
        "primitive": "import_network_pipeline",
        "version": 1,
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "ledger": str(ledger_path),
        "input": {
            "operator_id": args.operator_id,
            "linkedin_csv": args.linkedin_csv,
            "linkedin_source_user": args.linkedin_source_user,
            "linkedin_limit": args.linkedin_limit,
            "msgvault_db": resolve_msgvault_db(args),
            "gmail_account_email": args.gmail_account_email,
            "gmail_account_emails": unique_strings(getattr(args, "gmail_account_emails", [])),
            "gmail_limit": args.gmail_limit,
            "include_automated_gmail": args.include_automated_gmail,
            "gmail_linkedin_provider": args.gmail_linkedin_provider,
            "gmail_linkedin_limit": args.gmail_linkedin_limit,
            "gmail_resolutions_csv": args.gmail_resolutions_csv,
            "include_existing_artifacts": args.include_existing_artifacts,
            "skip_msgvault_sync": args.skip_msgvault_sync,
            "from_accounts": args.from_accounts,
            "from_setup": args.from_setup,
            "only_sources": unique_strings(getattr(args, "only_source", [])),
            "fan_in_only": args.fan_in_only,
            "twitter_handle": getattr(args, "twitter_handle", ""),
            "messages_contacts_csv": getattr(args, "messages_contacts_csv", ""),
        },
        "steps": {},
        "artifacts": {},
    }
    save_ledger(ledger_path, ledger)
    return run_pipeline(ledger_path, resume=False)


def dry_run_plan(args: argparse.Namespace, ledger_path: Path, run_id: str, run_dir: Path) -> dict[str, Any]:
    args = apply_account_sources(args)
    if ledger_path.exists():
        ledger = load_ledger(ledger_path)
        steps = ledger.get("steps", {}) or {}
        would_run = [
            step for step in ["linkedin", "gmail_msgvault", "gmail_linkedin_resolution", "gmail_apply_enrich", "merge", "duckdb"]
            if (steps.get(step) or {}).get("status") not in {"completed", "skipped"}
        ]
        child_paid = {}
        for name, path_text in (ledger.get("artifacts") or {}).items():
            if not name.endswith("ledger") or not path_text:
                continue
            child = read_json(Path(path_text), {}) or {}
            if "paid_call_count" in child:
                child_paid[name] = child.get("paid_call_count", 0)
        return {
            "status": "dry_run",
            "ledger": str(ledger_path),
            "run_id": ledger.get("run_id") or run_id,
            "run_dir": ledger.get("run_dir") or str(run_dir),
            "existing_status": ledger.get("status", "unknown"),
            "would_run_steps": would_run,
            "estimated_paid_calls": 0 if not would_run else "unknown_without_running_child_stage_plans",
            "child_paid_call_counts": child_paid,
            "artifact_check": check_artifact_paths(ledger),
        }
    would_run = []
    if args.linkedin_csv:
        would_run.append("linkedin")
    input_cfg = {
        "linkedin_csv": args.linkedin_csv,
        "msgvault_db": resolve_msgvault_db(args),
        "gmail_account_email": args.gmail_account_email,
        "gmail_account_emails": unique_strings(getattr(args, "gmail_account_emails", [])),
        "twitter_handle": getattr(args, "twitter_handle", ""),
        "messages_contacts_csv": getattr(args, "messages_contacts_csv", ""),
        "include_existing_artifacts": getattr(args, "include_existing_artifacts", False),
    }
    if args.gmail_account_email or unique_strings(getattr(args, "gmail_account_emails", [])) or resolve_msgvault_db(args):
        would_run.append("gmail_msgvault")
    if getattr(args, "gmail_linkedin_provider", "off") != "off":
        would_run.append("gmail_linkedin_resolution")
    if getattr(args, "gmail_resolutions_csv", ""):
        would_run.append("gmail_apply_enrich")
    would_run.extend(["merge", "duckdb"])
    return {
        "status": "dry_run",
        "ledger": str(ledger_path),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "existing_status": "missing",
        "would_run_steps": would_run,
        "worker_groups": {"import": source_worker_group(input_cfg, run_id)},
        "estimated_paid_calls": "unknown_without_existing_stage_outputs",
        "message": "No existing import-network ledger was found; running would execute the listed stages until any child approval gate.",
    }


def cmd_continue(args: argparse.Namespace) -> int:
    if not Path(args.ledger).exists():
        emit({"status": "missing_ledger", "ledger": args.ledger})
        return 2
    return run_pipeline(Path(args.ledger), resume=True)


def cmd_approve(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    ledger = load_ledger(ledger_path)
    blocked = ledger.get("blocked") or {}
    if blocked.get("step_id") == "linkedin" and blocked.get("child_ledger"):
        code, payload, stderr = run_cmd(py_cmd("packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py", "approve", "--ledger", blocked["child_ledger"]))
        if code != 0:
            emit({"status": "failed", "step_id": "approve", "error": stderr or payload})
            return 1
        ledger.pop("blocked", None)
        save_ledger(ledger_path, ledger)
        emit({"status": "approved", "ledger": str(ledger_path), "child": payload})
        return 0
    if blocked.get("step_id") == "gmail_linkedin_resolution" and blocked.get("child_ledger"):
        code, payload, stderr = run_cmd(py_cmd("packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py", "approve", "--ledger", blocked["child_ledger"]))
        if code != 0:
            emit({"status": "failed", "step_id": "approve", "error": stderr or payload})
            return 1
        ledger.pop("blocked", None)
        save_ledger(ledger_path, ledger)
        emit({"status": "approved", "ledger": str(ledger_path), "child": payload})
        return 0
    if blocked.get("step_id") == "gmail_apply_enrich" and blocked.get("child_ledger"):
        code, payload, stderr = run_cmd(py_cmd("packs/ingestion/primitives/enrich_people/enrich_people.py", "approve", "--ledger", blocked["child_ledger"]))
        if code != 0:
            emit({"status": "failed", "step_id": "approve", "error": stderr or payload})
            return 1
        ledger.pop("blocked", None)
        save_ledger(ledger_path, ledger)
        emit({"status": "approved", "ledger": str(ledger_path), "child": payload})
        return 0
    emit({"status": "no_pending_approval", "ledger": str(ledger_path)})
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    ledger = load_ledger(Path(args.ledger))
    emit({
        "status": ledger.get("status", "unknown"),
        "ledger": args.ledger,
        "run_dir": ledger.get("run_dir"),
        "blocked": ledger.get("blocked"),
        "steps": ledger.get("steps", {}),
        "artifacts": ledger.get("artifacts", {}),
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local network ingestion orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    run.add_argument("--from-accounts", default="", help="Account registry path produced by onboarding; fills source-specific args unless explicit flags override it")
    run.add_argument("--from-setup", default="", help="Setup ledger/handoff path containing an accounts path")
    run.add_argument("--run-id")
    run.add_argument("--operator-id", default="local")
    run.add_argument("--linkedin-csv", default="")
    run.add_argument("--linkedin-source-user", default="")
    run.add_argument("--linkedin-limit", type=int)
    run.add_argument("--msgvault-db", default="", help=f"msgvault SQLite DB; defaults to {DEFAULT_MSGVAULT_DB} when --gmail-account-email is set")
    run.add_argument("--gmail-account-email", default="")
    run.add_argument("--gmail-account-emails", action="append", default=[], help="Gmail/msgvault account email to import; may be repeated")
    run.add_argument("--gmail-limit", type=int)
    run.add_argument("--include-automated-gmail", action="store_true")
    run.add_argument("--gmail-linkedin-provider", choices=["off", "harness", "parallel"], default="off", help="Prepare/run Gmail email-to-LinkedIn resolution before merge. harness is local prompt prep; parallel is spend-bearing and approval-gated.")
    run.add_argument("--gmail-linkedin-limit", type=int, help=argparse.SUPPRESS)
    run.add_argument("--gmail-resolutions-csv", default="", help="Existing linkedin_resolutions.csv to apply to Gmail people before shared enrich_people")
    run.add_argument("--include-existing-artifacts", action="store_true", help="Merge all discovered existing LinkedIn/Gmail/Twitter/message artifacts instead of only artifacts produced by this run")
    run.add_argument("--skip-msgvault-sync", action="store_true", help="Skip import-time msgvault sync-full and read the existing DB as-is")
    run.add_argument("--twitter-handle", default="", help=argparse.SUPPRESS)
    run.add_argument("--messages-contacts-csv", default="", help=argparse.SUPPRESS)
    run.add_argument("--only-source", action="append", default=[], choices=SOURCE_NAMES, help="Run only a source import worker; skips fan-in merge unless --fan-in-only is set separately")
    run.add_argument("--fan-in-only", action="store_true", help="Skip source import workers and run merge/DuckDB fan-in from existing artifacts")
    run.add_argument("--dry-run", action="store_true", help="Inspect existing ledger/stage outputs and report work that would run")
    run.add_argument("--estimate", action="store_true", help="Alias for --dry-run")
    run.add_argument("--force", action="store_true")
    run.set_defaults(func=cmd_run)

    cont = sub.add_parser("continue")
    cont.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    cont.set_defaults(func=cmd_continue)

    approve = sub.add_parser("approve")
    approve.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    approve.set_defaults(func=cmd_approve)

    status = sub.add_parser("status")
    status.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.func(args)
    except KeyboardInterrupt:
        emit({"status": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
