#!/usr/bin/env python3
"""Orchestrate local network ingestion sources into merged CSVs + DuckDB.

The source-specific discovery work lives in sibling modules:
- gmail.py for msgvault/Gmail sync, directory matching, and LinkedIn resolution
- messages.py for reviewed iMessage/WhatsApp contact promotion and enrichment
- directory.py for directory.csv and people.csv materialization helpers

This file intentionally keeps orchestration, CLI, and fan-in/merge control flow.
"""

from __future__ import annotations

import argparse
import copy
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.primitives.discover_contacts_pipeline import common, directory, gmail, linkedin, messages
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.primitives.discover_contacts_pipeline import common, directory, gmail, linkedin, messages

DEFAULT_BASE_DIR = common.DEFAULT_BASE_DIR
DEFAULT_DISCOVER_DIR = common.DEFAULT_DISCOVER_DIR
DEFAULT_LEDGER = common.DEFAULT_LEDGER
DEFAULT_DIRECTORY_CSV = common.DEFAULT_DIRECTORY_CSV
DEFAULT_MSGVAULT_DB = common.DEFAULT_MSGVAULT_DB
DEFAULT_CHILD_TIMEOUT_SECONDS = common.DEFAULT_CHILD_TIMEOUT_SECONDS
DEFAULT_GMAIL_ESTIMATE_MAX_PAGES = gmail.DEFAULT_GMAIL_ESTIMATE_MAX_PAGES

SOURCE_NAMES = ["gmail", "linkedin_csv", "twitter", "messages"]
MERGED_ARTIFACT_KEYS = {
    "merged_people_csv",
    "network_contacts_csv",
    "network_contact_sources_csv",
    "network_companies_csv",
    "merge_manifest",
    "duckdb",
    "duckdb_manifest",
}
SOURCE_ARTIFACT_PREFIXES = {
    "gmail": ("gmail_",),
    "linkedin_csv": ("linkedin_",),
    "twitter": ("twitter_",),
    "messages": ("messages_",),
}
SOURCE_STEP_PREFIXES = {
    "gmail": ("gmail_msgvault", "gmail_directory", "gmail_linkedin_resolution", "gmail_apply_enrich"),
    "linkedin_csv": ("linkedin",),
    "twitter": ("twitter",),
    "messages": ("messages", "messages_enrich_people"),
}
MESSAGES_REVIEW_GATE_REASON = messages.MESSAGES_REVIEW_GATE_REASON
GMAIL_ENRICHMENT_ARTIFACT_KEYS = {"gmail_directory_queue_csv", "gmail_linkedin_resolution_csv", "gmail_directory_csv", "gmail_merged_people_csv"}

# Shared helper aliases keep the orchestrator readable and preserve direct CLI semantics.
artifact_dir_from_ledger = common.artifact_dir_from_ledger
begin_step = common.begin_step
check_artifact_paths = common.check_artifact_paths
child_error = common.child_error
collect_artifact_paths = common.collect_artifact_paths
csv_row_count = common.csv_row_count
default_artifact_dir = common.default_artifact_dir
discover_source_dir = common.discover_source_dir
emit = common.emit
emit_progress = common.emit_progress
load_ledger = common.load_ledger
mark_step = common.mark_step
now_iso = common.now_iso
parse_last_json = common.parse_last_json
py_cmd = common.py_cmd
read_json = common.read_json
run_cmd = common.run_cmd
save_ledger = common.save_ledger
sha = common.sha
source_slug = common.source_slug
unique_strings = common.unique_strings
write_csv_rows = common.write_csv_rows
write_json = common.write_json

commit_people_csv_to_directory = directory.commit_people_csv_to_directory

gmail_excluded_labels = gmail.gmail_excluded_labels
gmail_sync_after = gmail.gmail_sync_after
gmail_sync_query = gmail.gmail_sync_query
estimate_gmail_accounts_via_api = gmail.estimate_gmail_accounts_via_api
normalize_label_names = gmail.normalize_label_names
run_gmail_apply_and_enrich = gmail.run_gmail_apply_and_enrich
run_gmail_directory = gmail.run_gmail_directory
run_gmail_linkedin_resolution = gmail.run_gmail_linkedin_resolution
run_gmail_msgvault = gmail.run_gmail_msgvault
summarize_gmail_estimates = gmail.summarize_gmail_estimates

materialize_approved_messages_review = messages.materialize_approved_messages_review
run_messages_enrichment = messages.run_messages_enrichment

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
    cmd = str(commands.get("discover_contacts_run") or "")
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
    if not getattr(args, "messages_review_csv", ""):
        args.messages_review_csv = str(messages_cfg.get("review_csv") or "")
    return args


def resolve_msgvault_db(args: argparse.Namespace) -> str:
    explicit = str(getattr(args, "msgvault_db", "") or "").strip()
    if explicit:
        return explicit
    if str(getattr(args, "gmail_account_email", "") or "").strip() or unique_strings(getattr(args, "gmail_account_emails", [])):
        return str(DEFAULT_MSGVAULT_DB)
    return ""
def run_linkedin_child(ledger: dict[str, Any], mode: str) -> dict[str, Any]:
    input_cfg = ledger.get("input", {})
    artifact_dir = discover_source_dir("linkedin_csv")
    child_ledger = artifact_dir / "linkedin.ledger.json"
    if mode == "run":
        cmd = py_cmd(
            "packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py",
            "run",
            "--csv", input_cfg["linkedin_csv"],
            "--source-user", input_cfg.get("linkedin_source_user") or "local",
            "--operator-id", input_cfg.get("operator_id") or "local",
            "--output-dir", str(DEFAULT_DISCOVER_DIR),
            "--ledger", str(child_ledger),
            "--force",
        )
        if input_cfg.get("linkedin_limit") is not None:
            cmd.extend(["--limit", str(input_cfg["linkedin_limit"])])
        if input_cfg.get("source_import_only"):
            cmd.append("--convert-only")
    else:
        cmd = py_cmd("packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py", "continue", "--ledger", str(child_ledger))
    code, payload, stderr = run_cmd(cmd)
    return {"id": "linkedin_csv", "source": "linkedin_csv", "child_ledger": str(child_ledger), "command": cmd, "code": code, "payload": payload, "stderr": stderr}


def record_linkedin_worker_result(ledger_path: Path, ledger: dict[str, Any], result: dict[str, Any]) -> bool:
    code = int(result.get("code") or 0)
    payload = result.get("payload") or {}
    stderr = result.get("stderr") or ""
    child_ledger = result.get("child_ledger") or str(artifact_dir_from_ledger(ledger) / "linkedin.ledger.json")
    ledger.setdefault("artifacts", {})["linkedin_ledger"] = str(child_ledger)
    if code == 20 or payload.get("status") == "blocked_approval":
        ledger["blocked"] = {"step_id": "linkedin", "child_ledger": str(child_ledger), "child": payload}
        mark_step(ledger, "linkedin", "blocked", payload=payload)
        save_ledger(ledger_path, ledger)
        emit({"status": "blocked_approval", "step_id": "linkedin", "ledger": str(ledger_path), "child": payload})
        return False
    if code != 0:
        error = child_error(payload, stderr)
        mark_step(ledger, "linkedin", "failed", error=error)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "linkedin", "error": error})
        return False
    mark_step(ledger, "linkedin", "completed", payload=payload)
    for key, value in (payload.get("artifacts") or {}).items():
        ledger.setdefault("artifacts", {})[f"linkedin_{key}"] = value
    people_csv = (payload.get("artifacts") or {}).get("people_csv")
    if people_csv:
        checkpoint = commit_people_csv_to_directory(
            ledger.get("input", {}),
            ledger.setdefault("artifacts", {}),
            str(people_csv),
            source="linkedin_csv",
            source_account=str(ledger.get("input", {}).get("linkedin_source_user") or "local"),
        )
        ledger.setdefault("artifacts", {})["linkedin_directory_checkpoint"] = checkpoint
    emit_progress("LinkedIn import completed.")
    return True


def run_linkedin(ledger_path: Path, ledger: dict[str, Any], mode: str) -> bool:
    input_cfg = ledger.get("input", {})
    if not input_cfg.get("linkedin_csv"):
        mark_step(ledger, "linkedin", "skipped", reason="no --linkedin-csv")
        return True
    begin_step(ledger_path, ledger, "linkedin", "Importing LinkedIn CSV and enriching profiles.")
    return record_linkedin_worker_result(ledger_path, ledger, run_linkedin_child(ledger, mode))
def source_worker_group(input_cfg: dict[str, Any]) -> dict[str, Any]:
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
                "artifact_root": str(DEFAULT_DISCOVER_DIR / "gmail" / source_slug(email or "all")),
                "sync_query": gmail_sync_query(input_cfg),
                "sync_after": gmail_sync_after(input_cfg),
                "excluded_labels": gmail_excluded_labels(input_cfg),
                "parallelizable": True,
                "reason": "local msgvault metadata read into a stable discover folder",
            })
    if input_cfg.get("linkedin_csv"):
        jobs.append({
            "id": "linkedin_csv",
            "source": "linkedin_csv",
            "step_id": "linkedin",
            "ledger": str(DEFAULT_DISCOVER_DIR / "linkedin" / "linkedin.ledger.json"),
            "artifact_root": str(DEFAULT_DISCOVER_DIR / "linkedin"),
            "parallelizable": True,
            "reason": "CSV conversion/enrichment writes into a stable discover folder",
            "requires_approval": ["rapidapi_linkedin_profile_enrichment"],
        })
    if input_cfg.get("twitter_handle"):
        jobs.append({
            "id": "twitter",
            "source": "twitter",
            "handle": input_cfg.get("twitter_handle"),
            "parallelizable": True,
            "reason": "twitter import has no dependency on Gmail/LinkedIn but still requires spend confirmation",
            "requires_approval": ["rapidapi_twitter", "openai_moe", "rapidapi_linkedin_validation"],
            "status": "existing_artifacts_or_explicit_import_required",
        })
    messages_review_csv = input_cfg.get("messages_review_csv") or ""
    if messages_review_csv and not Path(str(messages_review_csv)).exists():
        messages_review_csv = ""
    if input_cfg.get("messages_contacts_csv") or messages_review_csv:
        jobs.append({
            "id": "messages",
            "source": "messages",
            "review_csv": messages_review_csv,
            "contacts_csv": input_cfg.get("messages_contacts_csv") or ".powerpacks/messages/contacts.csv",
            "parallelizable": True,
            "reason": "reviewed Messages LinkedIn rows are materialized locally, then hydrated through enrich_people before fan-in" if messages_review_csv else MESSAGES_REVIEW_GATE_REASON,
            "requires_approval": ["rapidapi_linkedin_profile_enrichment"] if messages_review_csv else ["messages_review_flow"],
            "status": "approved_review_artifact" if messages_review_csv else "review_required",
        })
    return {"parallel": True, "fan_in": "merge_network_sources_then_duckdb_after_nonblocked_workers", "jobs": jobs}


def run_source_import_workers(ledger_path: Path, ledger: dict[str, Any], *, resume: bool = False) -> bool:
    input_cfg = ledger.get("input", {})
    group = source_worker_group(input_cfg)
    ledger["worker_groups"] = {"import": group}
    selected = set(unique_strings(input_cfg.get("only_sources")))
    runnable_sources = {"gmail", "linkedin_csv", "messages"}
    if selected:
        runnable_sources &= selected
    mark_step(ledger, "source_imports", "running", worker_group=group)
    save_ledger(ledger_path, ledger)
    accounts_path = Path(str(input_cfg.get("from_accounts") or ".powerpacks/ingestion/accounts.json"))
    artifacts = ledger.setdefault("artifacts", {})

    if "gmail" in runnable_sources:
        begin_step(ledger_path, ledger, "gmail_msgvault", "Discovering Gmail contacts from existing msgvault metadata.")
        payload = gmail.discover(
            accounts_path=accounts_path,
            ledger_path=DEFAULT_BASE_DIR / "gmail" / "ledger.json",
            output_dir=DEFAULT_BASE_DIR / "gmail",
            operator_id=str(input_cfg.get("operator_id") or "local"),
        )
        if payload.get("status") == "failed":
            mark_step(ledger, "gmail_msgvault", "failed", payload=payload)
            mark_step(ledger, "source_imports", "failed", worker_group=group)
            save_ledger(ledger_path, ledger)
            return False
        mark_step(ledger, "gmail_msgvault", "completed" if payload.get("status") == "completed" else "skipped", payload=payload)
        if payload.get("contacts_csv"):
            artifacts["gmail_contacts_csv"] = payload["contacts_csv"]
        if payload.get("linkedin_resolution_queue_csv"):
            artifacts["gmail_linkedin_resolution_queue_csv"] = payload["linkedin_resolution_queue_csv"]
            artifacts["gmail_linkedin_resolution_queue_csvs"] = [{
                "account_email": "all",
                "queue_csv": payload["linkedin_resolution_queue_csv"],
                "people_csv": payload.get("contacts_csv", ""),
            }]
        save_ledger(ledger_path, ledger)

    if "linkedin_csv" in runnable_sources:
        begin_step(ledger_path, ledger, "linkedin", "Discovering LinkedIn Connections.csv contacts.")
        payload = linkedin.discover(
            accounts_path=accounts_path,
            ledger_path=DEFAULT_BASE_DIR / "linkedin" / "ledger.json",
            output_dir=DEFAULT_BASE_DIR / "linkedin",
        )
        if payload.get("status") == "failed":
            mark_step(ledger, "linkedin", "failed", payload=payload)
            mark_step(ledger, "source_imports", "failed", worker_group=group)
            save_ledger(ledger_path, ledger)
            return False
        mark_step(ledger, "linkedin", "completed" if payload.get("status") == "completed" else "skipped", payload=payload)
        if payload.get("contacts_csv"):
            artifacts["linkedin_contacts_csv"] = payload["contacts_csv"]
        save_ledger(ledger_path, ledger)

    if "messages" in runnable_sources:
        begin_step(ledger_path, ledger, "messages", "Discovering iMessage/WhatsApp contacts.")
        payload = messages.discover(
            accounts_path=accounts_path,
            ledger_path=DEFAULT_BASE_DIR / "messages" / "ledger.json",
            output_dir=DEFAULT_BASE_DIR / "messages",
        )
        status = str(payload.get("status") or "")
        if status in {"blocked_user_action", "blocked_approval"}:
            ledger["blocked"] = {"step_id": "messages", "child": payload}
            mark_step(ledger, "messages", "blocked", payload=payload)
            mark_step(ledger, "source_imports", "blocked", worker_group=group)
            save_ledger(ledger_path, ledger)
            emit({"status": status, "step_id": "messages", "ledger": str(ledger_path), "child": payload})
            return False
        if status == "failed":
            mark_step(ledger, "messages", "failed", payload=payload)
            mark_step(ledger, "source_imports", "failed", worker_group=group)
            save_ledger(ledger_path, ledger)
            return False
        mark_step(ledger, "messages", "completed" if status == "completed" else "skipped", payload=payload)
        if payload.get("contacts_csv"):
            artifacts["messages_contacts_csv"] = payload["contacts_csv"]
        save_ledger(ledger_path, ledger)

    for source in ["twitter"]:
        if not selected or source in selected:
            if source == "twitter" and not input_cfg.get("twitter_handle"):
                continue
            mark_step(ledger, source, "skipped", reason="Twitter/X discovery is not wired into local setup yet.")
    mark_step(ledger, "source_imports", "completed", worker_group=group)
    save_ledger(ledger_path, ledger)
    return True
def merge_input_paths(ledger: dict[str, Any], merge_dir: Path) -> list[str]:
    input_cfg = ledger.get("input", {}) or {}
    artifacts = ledger.get("artifacts", {}) or {}
    include_existing = bool(input_cfg.get("include_existing_artifacts"))
    explicit_inputs: list[str] = []
    canonical_people = DEFAULT_BASE_DIR / "merged" / "people.csv"
    if include_existing and canonical_people.exists():
        explicit_inputs.append(str(canonical_people))

    gmail_inputs = unique_strings(artifacts.get("gmail_final_people_csvs") or [])
    if not gmail_inputs and artifacts.get("gmail_merged_people_csv"):
        gmail_inputs = [str(artifacts["gmail_merged_people_csv"])]

    explicit_inputs.extend(
        value for key, value in sorted(artifacts.items())
        if key in {"linkedin_people_csv"} and value
    )
    if gmail_inputs:
        explicit_inputs.extend(str(path) for path in gmail_inputs if path)

    messages_people_inputs = unique_strings(artifacts.get("messages_final_people_csvs") or [])
    if not messages_people_inputs and artifacts.get("messages_merged_people_csv"):
        messages_people_inputs = [str(artifacts["messages_merged_people_csv"])]
    explicit_inputs.extend(str(path) for path in messages_people_inputs if path)

    messages_contacts = ""
    if input_cfg.get("allow_unreviewed_messages"):
        messages_contacts = artifacts.get("messages_contacts_csv") or input_cfg.get("messages_contacts_csv")
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

    return list(dict.fromkeys(explicit_inputs))


def run_merge(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    begin_step(ledger_path, ledger, "merge", "Merging network sources.")
    merge_dir = artifact_dir_from_ledger(ledger) / "merged"
    cmd = py_cmd(
        "packs/ingestion/primitives/merge_network_sources/merge_network_sources.py",
        "run",
        "--output-dir", str(merge_dir),
    )
    explicit_inputs = merge_input_paths(ledger, merge_dir)
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
    merge_dir = artifact_dir_from_ledger(ledger) / "merged"
    duckdb_dir = artifact_dir_from_ledger(ledger) / "duckdb"
    cmd = py_cmd(
        "packs/ingestion/primitives/build_network_duckdb/build_network_duckdb.py",
        "--network-dir", str(merge_dir),
        "--output-dir", str(duckdb_dir),
        "--flavor", "local",
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
    if ledger.get("input", {}).get("source_import_only"):
        ledger["status"] = "source_import_completed"
        save_ledger(ledger_path, ledger)
        emit({"status": "source_import_completed", "ledger": str(ledger_path), "artifact_dir": str(artifact_dir_from_ledger(ledger)), "steps": ledger.get("steps", {}), "artifacts": ledger.get("artifacts", {})})
        return 0
    selected_sources = set(unique_strings(ledger.get("input", {}).get("only_sources")))
    enrichment_only = bool(ledger.get("input", {}).get("enrichment_only"))
    merge_only = bool(ledger.get("input", {}).get("merge_only"))
    selected_fan_in_sources = selected_sources if ledger.get("input", {}).get("fan_in_only") or enrichment_only else set()
    run_gmail_enrichment = (not selected_fan_in_sources or "gmail" in selected_fan_in_sources) and not merge_only
    run_messages_profile_enrichment = (not selected_fan_in_sources or "messages" in selected_fan_in_sources) and not merge_only
    if enrichment_only:
        if run_gmail_enrichment:
            if not run_gmail_directory(ledger_path, ledger):
                return 1
            save_ledger(ledger_path, ledger)
            if not run_gmail_linkedin_resolution(ledger_path, ledger):
                return 20 if ledger.get("blocked") else 1
            save_ledger(ledger_path, ledger)
            if not run_gmail_apply_and_enrich(ledger_path, ledger):
                return 20 if ledger.get("blocked") else 1
            save_ledger(ledger_path, ledger)
        if run_messages_profile_enrichment:
            if not run_messages_enrichment(ledger_path, ledger):
                return 20 if ledger.get("blocked") else 1
            save_ledger(ledger_path, ledger)
        ledger["status"] = "source_enrichment_completed"
        ledger.pop("blocked", None)
        save_ledger(ledger_path, ledger)
        emit({"status": "source_enrichment_completed", "ledger": str(ledger_path), "artifact_dir": str(artifact_dir_from_ledger(ledger)), "steps": ledger.get("steps", {}), "artifacts": ledger.get("artifacts", {})})
        return 0
    if ledger.get("input", {}).get("only_sources") and not ledger.get("input", {}).get("fan_in_only"):
        if "messages" in selected_sources and ledger.get("steps", {}).get("messages_enrich_people", {}).get("status") not in {"completed", "skipped"}:
            if not run_messages_enrichment(ledger_path, ledger):
                return 20 if ledger.get("blocked") else 1
            save_ledger(ledger_path, ledger)
        ledger["status"] = "source_import_completed"
        save_ledger(ledger_path, ledger)
        emit({"status": "source_import_completed", "ledger": str(ledger_path), "artifact_dir": str(artifact_dir_from_ledger(ledger)), "steps": ledger.get("steps", {}), "artifacts": ledger.get("artifacts", {})})
        return 0
    if run_gmail_enrichment:
        if not run_gmail_directory(ledger_path, ledger):
            return 1
        save_ledger(ledger_path, ledger)
    if run_gmail_enrichment:
        if not run_gmail_linkedin_resolution(ledger_path, ledger):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if run_gmail_enrichment:
        if not run_gmail_apply_and_enrich(ledger_path, ledger):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if run_messages_profile_enrichment:
        if not run_messages_enrichment(ledger_path, ledger):
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
    emit({"status": "completed", "ledger": str(ledger_path), "artifact_dir": str(artifact_dir_from_ledger(ledger)), "artifacts": ledger.get("artifacts", {})})
    return 0


def step_matches_source(step_id: str, selected_sources: set[str]) -> bool:
    for source in selected_sources:
        for prefix in SOURCE_STEP_PREFIXES.get(source, ()):
            if step_id == prefix or step_id.startswith(f"{prefix}:"):
                return True
    return False


def artifact_matches_source(key: str, selected_sources: set[str]) -> bool:
    for source in selected_sources:
        for prefix in SOURCE_ARTIFACT_PREFIXES.get(source, ()):
            if key.startswith(prefix):
                return True
    return False


def preserved_state_for_source_refresh(existing: dict[str, Any], selected_sources: set[str]) -> dict[str, Any]:
    """Carry untouched source outputs across one-source refreshes on the shared setup ledger."""
    if not existing:
        return {}
    artifacts = {
        key: copy.deepcopy(value)
        for key, value in (existing.get("artifacts") or {}).items()
        if key not in MERGED_ARTIFACT_KEYS and not artifact_matches_source(key, selected_sources)
    }
    steps = {
        key: copy.deepcopy(value)
        for key, value in (existing.get("steps") or {}).items()
        if key not in {"source_imports", "merge", "duckdb"} and not step_matches_source(key, selected_sources)
    }
    source_imports = {
        key: copy.deepcopy(value)
        for key, value in (existing.get("source_imports") or {}).items()
        if not step_matches_source(key, selected_sources)
    }
    return {"artifacts": artifacts, "steps": steps, "source_imports": source_imports}


GMAIL_ENRICHMENT_ARTIFACT_KEYS = {
    "gmail_directory_resolution_records",
    "gmail_unresolved_linkedin_resolution_queue_csvs",
    "gmail_cached_negative_linkedin_resolution_queue_csvs",
    "gmail_linkedin_resolutions_csvs",
    "gmail_linkedin_resolution_ledgers",
    "gmail_linkedin_resolution_ledger",
    "gmail_linkedin_resolutions_csv",
    "gmail_linkedin_raw_resolutions_csv",
    "gmail_linkedin_resolutions_by_slug",
    "gmail_linkedin_harness_prompts_jsonls",
    "gmail_linkedin_harness_prompts_jsonl",
    "gmail_linkedin_harness_instructions",
    "gmail_resolved_people_csvs",
    "gmail_resolved_people_csv",
    "gmail_enrich_people_ledgers",
    "gmail_enrich_people_ledger",
    "gmail_final_people_csvs",
    "gmail_account_final_people_csvs",
    "gmail_merged_people_csv",
    "gmail_merged_people",
    "gmail_combined_resolutions_csvs",
    "gmail_apply_enrich_by_slug",
}


def reset_selected_fan_in_state(preserved: dict[str, Any], selected_sources: set[str]) -> dict[str, Any]:
    if not preserved or not selected_sources:
        return preserved
    steps = preserved.setdefault("steps", {})
    artifacts = preserved.setdefault("artifacts", {})
    if "gmail" in selected_sources:
        for step in ["gmail_directory", "gmail_linkedin_resolution", "gmail_apply_enrich"]:
            steps.pop(step, None)
        for key in list(artifacts):
            if key in GMAIL_ENRICHMENT_ARTIFACT_KEYS or key.startswith("gmail_directory_by_slug") or key.startswith("gmail_") and "_enriched_" in key:
                artifacts.pop(key, None)
    if "messages" in selected_sources:
        steps.pop("messages_enrich_people", None)
        for key in list(artifacts):
            if key.startswith("messages_enriched_") or key in {"messages_people_csv", "messages_people_csvs", "messages_final_people_csvs", "messages_merged_people_csv", "messages_merged_people", "messages_people_input_csv", "messages_people_input_manifest", "messages_enrich_people_ledger"}:
                artifacts.pop(key, None)
    return preserved


def cmd_run(args: argparse.Namespace) -> int:
    args = apply_account_sources(args)
    selected_sources = set(unique_strings(getattr(args, "only_source", [])))
    artifact_dir = default_artifact_dir(args, selected_sources)
    ledger_path = Path(args.ledger)
    if args.dry_run or args.estimate:
        emit(dry_run_plan(args, ledger_path, artifact_dir))
        return 0
    existing = load_ledger(ledger_path) if ledger_path.exists() else {}
    if ledger_path.exists() and not args.force:
        if existing.get("status") == "completed":
            emit({
                "status": "completed",
                "cached": True,
                "ledger": str(ledger_path),
                "artifact_dir": existing.get("artifact_dir") or existing.get("run_dir"),
                "message": "Existing completed discover-contacts ledger found; no work was run.",
                "artifact_check": check_artifact_paths(existing),
                "artifacts": existing.get("artifacts", {}),
            })
            return 0
        if existing.get("status") not in {"failed"}:
            emit({"status": "active_run_exists", "ledger": str(ledger_path), "message": "Use continue/approve or --force."})
            return 0
    preserve_sources = set() if args.fan_in_only else selected_sources
    preserved = preserved_state_for_source_refresh(existing, preserve_sources) if args.force and (selected_sources or args.fan_in_only) else {}
    if args.force and args.fan_in_only and selected_sources:
        preserved = reset_selected_fan_in_state(preserved, selected_sources)
    ledger = {
        "primitive": "discover_contacts_pipeline",
        "version": 1,
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "artifact_dir": str(artifact_dir),
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
            "gmail_exclude_labels": normalize_label_names(getattr(args, "gmail_exclude_label", [])),
            "include_category_mail": bool(getattr(args, "include_category_mail", False)),
            "gmail_sync_query": str(getattr(args, "gmail_sync_query", "") or "").strip(),
            "gmail_sync_after": gmail_sync_after({"gmail_sync_after": getattr(args, "gmail_sync_after", "")}),
            "skip_gmail_estimate": bool(getattr(args, "skip_gmail_estimate", False)),
            "gmail_estimate_max_pages": int(getattr(args, "gmail_estimate_max_pages", DEFAULT_GMAIL_ESTIMATE_MAX_PAGES) or DEFAULT_GMAIL_ESTIMATE_MAX_PAGES),
            "gmail_linkedin_provider": args.gmail_linkedin_provider,
            "resolve_gmail_linkedin": args.resolve_gmail_linkedin,
            "approve_parallel_spend": bool(getattr(args, "approve_parallel_spend", False)),
            "gmail_linkedin_limit": args.gmail_linkedin_limit,
            "gmail_resolutions_csv": args.gmail_resolutions_csv,
            "linkedin_directory_csv": args.linkedin_directory_csv,
            "linkedin_directory_source_csvs": unique_strings(getattr(args, "linkedin_directory_source_csv", [])),
            "linkedin_directory_use_defaults": not bool(getattr(args, "no_default_linkedin_directory_sources", False)),
            "include_existing_artifacts": args.include_existing_artifacts,
            "skip_msgvault_sync": args.skip_msgvault_sync,
            "from_accounts": args.from_accounts,
            "from_setup": args.from_setup,
            "only_sources": unique_strings(getattr(args, "only_source", [])),
            "fan_in_only": args.fan_in_only,
            "source_import_only": args.source_import_only,
            "enrichment_only": bool(getattr(args, "enrichment_only", False)),
            "merge_only": bool(getattr(args, "merge_only", False)),
            "twitter_handle": getattr(args, "twitter_handle", ""),
            "messages_review_csv": getattr(args, "messages_review_csv", ""),
            "messages_contacts_csv": getattr(args, "messages_contacts_csv", ""),
            "allow_unreviewed_messages": bool(getattr(args, "allow_unreviewed_messages", False)),
        },
        "steps": {},
        "artifacts": {},
    }
    if preserved:
        ledger["steps"].update(preserved.get("steps") or {})
        ledger["artifacts"].update(preserved.get("artifacts") or {})
        if preserved.get("source_imports"):
            ledger["source_imports"] = preserved["source_imports"]
    save_ledger(ledger_path, ledger)
    return run_pipeline(ledger_path, resume=False)


def dry_run_plan(args: argparse.Namespace, ledger_path: Path, artifact_dir: Path) -> dict[str, Any]:
    args = apply_account_sources(args)
    if ledger_path.exists():
        ledger = load_ledger(ledger_path)
        steps = ledger.get("steps", {}) or {}
        if ledger.get("status") == "completed":
            would_run = []
        else:
            would_run = [
                step for step in ["linkedin", "gmail_msgvault", "gmail_directory", "gmail_linkedin_resolution", "gmail_apply_enrich", "messages_enrich_people", "merge", "duckdb"]
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
            "artifact_dir": ledger.get("artifact_dir") or ledger.get("run_dir") or str(artifact_dir),
            "existing_status": ledger.get("status", "unknown"),
            "would_run_steps": would_run,
            "estimated_paid_calls": 0 if not would_run else "unknown_without_running_child_stage_plans",
            "child_paid_call_counts": child_paid,
            "gmail_api_estimates": (ledger.get("artifacts") or {}).get("gmail_api_estimates") or [],
            "artifact_check": check_artifact_paths(ledger),
        }
    would_run = []
    if args.linkedin_csv:
        would_run.append("linkedin")
    messages_review_csv = getattr(args, "messages_review_csv", "")
    if messages_review_csv and not Path(str(messages_review_csv)).exists():
        messages_review_csv = ""
    input_cfg = {
        "linkedin_csv": args.linkedin_csv,
        "msgvault_db": resolve_msgvault_db(args),
        "gmail_account_email": args.gmail_account_email,
        "gmail_account_emails": unique_strings(getattr(args, "gmail_account_emails", [])),
        "gmail_exclude_labels": normalize_label_names(getattr(args, "gmail_exclude_label", [])),
        "include_category_mail": bool(getattr(args, "include_category_mail", False)),
        "gmail_sync_query": str(getattr(args, "gmail_sync_query", "") or "").strip(),
        "gmail_sync_after": gmail_sync_after({"gmail_sync_after": getattr(args, "gmail_sync_after", "")}),
        "skip_gmail_estimate": bool(getattr(args, "skip_gmail_estimate", False)),
        "gmail_estimate_max_pages": int(getattr(args, "gmail_estimate_max_pages", DEFAULT_GMAIL_ESTIMATE_MAX_PAGES) or DEFAULT_GMAIL_ESTIMATE_MAX_PAGES),
        "twitter_handle": getattr(args, "twitter_handle", ""),
        "messages_review_csv": messages_review_csv,
        "messages_contacts_csv": getattr(args, "messages_contacts_csv", ""),
        "include_existing_artifacts": getattr(args, "include_existing_artifacts", False),
        "linkedin_directory_csv": getattr(args, "linkedin_directory_csv", str(DEFAULT_DIRECTORY_CSV)),
        "linkedin_directory_source_csvs": unique_strings(getattr(args, "linkedin_directory_source_csv", [])),
        "linkedin_directory_use_defaults": not bool(getattr(args, "no_default_linkedin_directory_sources", False)),
    }
    gmail_emails = unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email"))
    gmail_estimates = estimate_gmail_accounts_via_api(input_cfg, gmail_emails) if gmail_emails else []
    fan_in_only = bool(getattr(args, "fan_in_only", False))
    source_import_only = bool(getattr(args, "source_import_only", False))
    if not fan_in_only and (args.gmail_account_email or unique_strings(getattr(args, "gmail_account_emails", [])) or resolve_msgvault_db(args)):
        would_run.append("gmail_msgvault")
    if not source_import_only and (args.gmail_account_email or unique_strings(getattr(args, "gmail_account_emails", [])) or resolve_msgvault_db(args)):
        would_run.append("gmail_directory")
    if not source_import_only and (getattr(args, "resolve_gmail_linkedin", False) or getattr(args, "gmail_linkedin_provider", "off") != "off"):
        would_run.append("gmail_linkedin_resolution")
    if not source_import_only and getattr(args, "gmail_resolutions_csv", ""):
        would_run.append("gmail_apply_enrich")
    if not source_import_only and messages_review_csv:
        would_run.append("messages_enrich_people")
    if not source_import_only:
        would_run.extend(["merge", "duckdb"])
    return {
        "status": "dry_run",
        "ledger": str(ledger_path),
        "artifact_dir": str(artifact_dir),
        "existing_status": "missing",
        "would_run_steps": would_run,
        "worker_groups": {} if fan_in_only else {"import": source_worker_group(input_cfg)},
        "gmail_api_estimates": gmail_estimates,
        "gmail_estimate_summary": summarize_gmail_estimates(gmail_estimates) if gmail_estimates else "",
        "estimated_paid_calls": "unknown_without_existing_stage_outputs",
        "message": "No existing discover-contacts ledger was found; running would execute the listed stages until any child approval confirmation.",
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
    if blocked.get("step_id") == "messages_enrich_people" and blocked.get("child_ledger"):
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
        "artifact_dir": ledger.get("artifact_dir") or ledger.get("run_dir"),
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
    run.add_argument("--operator-id", default="local")
    run.add_argument("--linkedin-csv", default="")
    run.add_argument("--linkedin-source-user", default="")
    run.add_argument("--linkedin-limit", type=int)
    run.add_argument("--msgvault-db", default="", help=f"msgvault SQLite DB; defaults to {DEFAULT_MSGVAULT_DB} when --gmail-account-email is set")
    run.add_argument("--gmail-account-email", default="")
    run.add_argument("--gmail-account-emails", action="append", default=[], help="Gmail/msgvault account email to import; may be repeated")
    run.add_argument("--gmail-limit", type=int)
    run.add_argument("--include-automated-gmail", action="store_true")
    run.add_argument("--gmail-exclude-label", action="append", default=[], help="Exclude this Gmail/msgvault label during sync/import; may be repeated. Defaults to Social, Promotions, Forums, Updates.")
    run.add_argument("--include-category-mail", action="store_true", help="Do not exclude Gmail Social, Promotions, Forums, and Updates categories during sync/import")
    run.add_argument("--gmail-sync-query", default="", help="Override the Gmail search query passed to msgvault sync-full and the Gmail API estimate")
    run.add_argument("--gmail-sync-after", default="", help="Pass --after YYYY-MM-DD to msgvault sync-full for bounded Gmail refreshes")
    run.add_argument("--skip-gmail-estimate", action="store_true", help="Skip the pre-sync Gmail API label/count estimate")
    run.add_argument("--gmail-estimate-max-pages", type=int, default=DEFAULT_GMAIL_ESTIMATE_MAX_PAGES, help=argparse.SUPPRESS)
    run.add_argument("--resolve-gmail-linkedin", action="store_true", help="Resolve Gmail contacts to LinkedIn with Parallel before applying Gmail enrichment.")
    run.add_argument("--approve-parallel-spend", action="store_true", help="Auto-approve Parallel.ai spend without blocking for confirmation.")
    run.add_argument("--gmail-linkedin-provider", choices=["off", "harness", "parallel"], default="off", help=argparse.SUPPRESS)
    run.add_argument("--gmail-linkedin-limit", type=int, help=argparse.SUPPRESS)
    run.add_argument("--gmail-resolutions-csv", default="", help="Existing linkedin_resolutions.csv to apply to Gmail people before shared enrich_people")
    run.add_argument("--linkedin-directory-csv", default=str(DEFAULT_DIRECTORY_CSV), help=argparse.SUPPRESS)
    run.add_argument("--linkedin-directory-source-csv", action="append", default=[], help=argparse.SUPPRESS)
    run.add_argument("--no-default-linkedin-directory-sources", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--include-existing-artifacts", action="store_true", help="Merge all discovered existing LinkedIn/Gmail/Twitter/message artifacts instead of only artifacts produced by this run")
    run.add_argument("--skip-msgvault-sync", action="store_true", help="Skip import-time msgvault sync-full and read the existing DB as-is")
    run.add_argument("--twitter-handle", default="", help=argparse.SUPPRESS)
    run.add_argument("--messages-review-csv", default="", help=argparse.SUPPRESS)
    run.add_argument("--messages-contacts-csv", default="", help=argparse.SUPPRESS)
    run.add_argument("--allow-unreviewed-messages", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--only-source", action="append", default=[], choices=SOURCE_NAMES, help="Run only a source import worker; skips fan-in merge unless --fan-in-only is set separately")
    run.add_argument("--fan-in-only", action="store_true", help="Skip source import workers and run merge/DuckDB fan-in from existing artifacts")
    run.add_argument("--source-import-only", action="store_true", help="Run raw source imports only; skip resolution, enrichment, merge, and DuckDB fan-in")
    run.add_argument("--enrichment-only", action="store_true", help="Run source-specific enrichment and stop before merge/DuckDB")
    run.add_argument("--merge-only", action="store_true", help="Run only merge/DuckDB materialization; skip source-specific enrichment")
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
