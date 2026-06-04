#!/usr/bin/env python3
"""Messages/iMessage/WhatsApp review and enrichment flow."""

from __future__ import annotations

import json
import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        extract_public_identifier,
        normalize_linkedin_url,
        normalize_people_row,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        extract_public_identifier,
        normalize_linkedin_url,
        normalize_people_row,
    )

try:
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        account_config,
        account_channel,
        artifact_dir_from_ledger,
        begin_step,
        channel_is_linked,
        csv_row_count,
        emit,
        emit_progress,
        mark_step,
        now_iso,
        parse_jsonish,
        py_cmd,
        read_accounts,
        read_csv_rows,
        read_json,
        run_cmd,
        save_ledger,
        sha,
        source_slug,
        unique_strings,
        write_csv_rows,
        write_json,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.directory import (
        DIRECTORY_COLUMNS,
        build_directory_checkpoint,
        commit_people_csv_to_directory,
        commit_directory_rows,
        directory_row_is_found,
        directory_rows_from_people_csv,
        materialize_messages_merged_people_csv,
        normalized_directory_row,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        account_config,
        account_channel,
        artifact_dir_from_ledger,
        begin_step,
        channel_is_linked,
        csv_row_count,
        emit,
        emit_progress,
        mark_step,
        now_iso,
        parse_jsonish,
        py_cmd,
        read_accounts,
        read_csv_rows,
        read_json,
        run_cmd,
        save_ledger,
        sha,
        source_slug,
        unique_strings,
        write_csv_rows,
        write_json,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.directory import (
        DIRECTORY_COLUMNS,
        build_directory_checkpoint,
        commit_people_csv_to_directory,
        commit_directory_rows,
        directory_row_is_found,
        directory_rows_from_people_csv,
        materialize_messages_merged_people_csv,
        normalized_directory_row,
    )

MESSAGES_REVIEW_GATE_REASON = (
    "messages contacts require the reviewed $import-contacts flow before they can be merged into local network search"
)
TRUTHY = {"1", "true", "yes", "y", "on"}
FALSY = {"0", "false", "no", "n", "off"}
INCLUDE_DECISIONS = {"include", "approved", "approve", "yes", "true", "1"}
EXCLUDE_DECISIONS = {"exclude", "excluded", "skip", "skipped", "no", "false", "0"}
RESEARCH_REVIEW_SOURCES = {"llm_network_review", "retarget_research", "retarget_refresh", "deep_research"}
DEFAULT_ACCOUNTS = Path(".powerpacks/ingestion/accounts.json")
DEFAULT_MESSAGES_OUTPUT_DIR = DEFAULT_BASE_DIR / "discover" / "messages"
DEFAULT_MESSAGES_DISCOVERY_LEDGER = DEFAULT_MESSAGES_OUTPUT_DIR / "ledger.json"
DEFAULT_IMPORT_CONTACTS_LEDGER = Path(".powerpacks/messages/import-run.setup-messages.json")
DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES = 500

def normalize_bool(value: Any) -> bool | None:
    raw = str(value or "").strip().lower()
    if raw in TRUTHY:
        return True
    if raw in FALSY:
        return False
    return None


def normalize_include_decision(value: Any) -> bool | None:
    raw = str(value or "").strip().lower()
    if raw in INCLUDE_DECISIONS:
        return True
    if raw in EXCLUDE_DECISIONS:
        return False
    return None


def normalize_exclude_decision(value: Any) -> bool | None:
    raw = str(value or "").strip().lower()
    if raw in TRUTHY:
        return False
    if raw in FALSY:
        return True
    return None


def explicitly_approved_message_review_row(row: dict[str, str]) -> bool:
    """Return True only for explicit human/upload approval, never bucket defaults."""
    approved = normalize_bool(row.get("approved", ""))
    upload_decision = normalize_include_decision(row.get("upload_decision", ""))
    exclude_decision = normalize_exclude_decision(row.get("exclude", ""))
    if approved is False or upload_decision is False or exclude_decision is False:
        return False
    return approved is True or upload_decision is True or exclude_decision is True


def review_row_to_messages_contact(row: dict[str, str]) -> dict[str, str]:
    name = row.get("network_name") or row.get("full_name") or ""
    return {
        "name": name,
        "phone": row.get("phone_e164", ""),
        "source": row.get("message_source") or "messages_review",
        "message_count": row.get("total_messages", ""),
        "last_message": row.get("last_message", ""),
        "matched_linkedin_url": row.get("network_linkedin_url", ""),
        "matched_name": name,
        "matched_person_id": row.get("network_person_id", ""),
        "match_method": row.get("network_match_method") or row.get("review_source") or "messages_review_approved",
    }


def materialize_approved_messages_review(review_csv: Path, scratch: Path) -> dict[str, Any] | None:
    if not review_csv.exists():
        return None
    _fields, rows = read_csv_rows(review_csv)
    approved_rows = [review_row_to_messages_contact(row) for row in rows if explicitly_approved_message_review_row(row)]
    if not approved_rows:
        return {
            "review_csv": str(review_csv),
            "approved_rows": 0,
            "total_rows": len(rows),
            "contacts_csv": "",
        }
    fields = ["name", "phone", "source", "message_count", "last_message", "matched_linkedin_url", "matched_name", "matched_person_id", "match_method"]
    write_csv_rows(scratch, fields, approved_rows)
    return {
        "review_csv": str(review_csv),
        "approved_rows": len(approved_rows),
        "total_rows": len(rows),
        "contacts_csv": str(scratch),
    }


def messages_review_linkedin_url(row: dict[str, str]) -> str:
    for key in ("retarget_linkedin_url", "linkedin_url", "network_linkedin_url"):
        normalized = normalize_linkedin_url(row.get(key, ""))
        if extract_public_identifier(normalized):
            return normalized
    return ""


def messages_review_row_rejected(row: dict[str, str]) -> bool:
    approved = normalize_bool(row.get("approved", ""))
    upload_decision = normalize_include_decision(row.get("upload_decision", ""))
    exclude_decision = normalize_exclude_decision(row.get("exclude", ""))
    enrich_decision = normalize_include_decision(row.get("enrich_decision", ""))
    return approved is False or upload_decision is False or exclude_decision is False or enrich_decision is False


def messages_review_row_in_network(row: dict[str, str]) -> bool:
    raw = normalize_bool(row.get("in_network", ""))
    if raw is not None:
        return raw
    return bool((row.get("network_person_id") or "").strip())


def messages_review_row_approved_for_local(row: dict[str, str]) -> bool:
    if messages_review_row_rejected(row):
        return False
    if explicitly_approved_message_review_row(row):
        return True
    return (row.get("bucket") or "").strip().lower() in {"yes", "confident"}


def messages_review_row_researched(row: dict[str, str]) -> bool:
    review_source = (row.get("review_source") or "").strip().lower()
    if review_source in RESEARCH_REVIEW_SOURCES:
        return True
    if review_source and review_source != "in_network_match":
        return True
    return any((row.get(key) or "").strip() for key in ("top_title_company_pairs", "top_titles", "top_companies", "schools", "short_reason"))


def messages_review_people_selection_reason(row: dict[str, str]) -> str:
    if messages_review_row_rejected(row):
        return ""
    if not messages_review_linkedin_url(row):
        return ""
    if messages_review_row_in_network(row):
        return "in_network"
    if messages_review_row_approved_for_local(row):
        return "approved"
    if normalize_include_decision(row.get("enrich_decision", "")) is True:
        return "enrich_decision"
    if messages_review_row_researched(row):
        return "researched"
    return ""


def split_full_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


def split_title_company(pair_text: str) -> tuple[str, str]:
    first = next((part.strip() for part in re.split(r"\s*\|\s*", pair_text or "") if part.strip()), "")
    for marker in (" at ", " @ ", " - "):
        if marker in first:
            title, company = first.split(marker, 1)
            return title.strip(), company.strip()
    return first.strip(), ""


def messages_source_channels(row: dict[str, str]) -> list[str]:
    channels: list[str] = []
    raw = (row.get("message_source") or "").strip().lower()
    for token in re.split(r"[,|+/;\s]+", raw):
        if token in {"imessage", "whatsapp"} and token not in channels:
            channels.append(token)
    for key, channel in (("imessage_message_count", "imessage"), ("whatsapp_message_count", "whatsapp")):
        try:
            count = int(float(row.get(key) or 0))
        except ValueError:
            count = 0
        if count > 0 and channel not in channels:
            channels.append(channel)
    return channels or ["messages"]


def review_row_to_messages_people(row: dict[str, str], review_csv: Path, reason: str) -> dict[str, str]:
    linkedin_url = messages_review_linkedin_url(row)
    public_identifier = extract_public_identifier(linkedin_url)
    full_name = (row.get("network_name") if reason == "in_network" else "") or row.get("full_name") or row.get("network_name") or ""
    first_name, last_name = split_full_name(full_name)
    current_title, current_company = split_title_company(row.get("top_title_company_pairs") or row.get("top_titles", ""))
    phone = (row.get("phone_e164") or "").strip()
    channels = messages_source_channels(row)
    summary_parts = [
        f"messages_total={row.get('total_messages') or '0'}",
        f"selection={reason}",
    ]
    if row.get("last_message"):
        summary_parts.append(f"last_message={row.get('last_message')}")
    if row.get("short_reason"):
        summary_parts.append(f"review_reason={row.get('short_reason')}")
    people = {
        "id": row.get("network_person_id") or f"message-linkedin:{sha(public_identifier or linkedin_url, 16)}",
        "public_identifier": public_identifier,
        "linkedin_url": linkedin_url,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "headline": row.get("top_title_company_pairs") or row.get("top_titles", ""),
        "summary": "; ".join(part for part in summary_parts if part),
        "city": row.get("location_city", ""),
        "country": row.get("location_country", ""),
        "current_title": current_title,
        "current_company": current_company,
        "primary_phone": phone,
        "all_phones": json.dumps([phone], ensure_ascii=False) if phone else "",
        "source_channels": ",".join(channels),
        "source_artifacts": str(review_csv),
        "enrichment_provider": f"messages_review:{reason}",
    }
    return normalize_people_row(people)


def merge_messages_people_candidate(existing: dict[str, str], incoming: dict[str, str]) -> dict[str, str]:
    merged = dict(existing)
    for key in ("primary_phone", "full_name", "first_name", "last_name", "headline", "summary", "city", "country", "current_title", "current_company"):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming[key]
    phones = unique_strings([merged.get("primary_phone", ""), incoming.get("primary_phone", "")])
    if phones:
        merged["primary_phone"] = merged.get("primary_phone") or phones[0]
        merged["all_phones"] = json.dumps(phones, ensure_ascii=False)
    channels = unique_strings((merged.get("source_channels", "").split(",") if merged.get("source_channels") else []) + (incoming.get("source_channels", "").split(",") if incoming.get("source_channels") else []))
    if channels:
        merged["source_channels"] = ",".join(channels)
    providers = unique_strings([merged.get("enrichment_provider", ""), incoming.get("enrichment_provider", "")])
    if providers:
        merged["enrichment_provider"] = ",".join(providers)
    return normalize_people_row(merged)


def materialize_messages_review_people(review_csv: Path, output_csv: Path, manifest_path: Path | None = None) -> dict[str, Any]:
    if not review_csv.exists():
        return {
            "review_csv": str(review_csv),
            "people_csv": "",
            "total_rows": 0,
            "eligible_rows": 0,
            "rows_written": 0,
            "selection_counts": {},
            "skipped": {"missing_review_csv": 1},
        }
    _fields, rows = read_csv_rows(review_csv)
    by_public_identifier: dict[str, dict[str, str]] = {}
    selection_counts: dict[str, int] = {}
    skipped = {
        "rejected": 0,
        "missing_linkedin_url": 0,
        "not_selected": 0,
        "duplicate_public_identifier": 0,
    }
    eligible_rows = 0
    for row in rows:
        linkedin_url = messages_review_linkedin_url(row)
        if not linkedin_url:
            skipped["missing_linkedin_url"] += 1
            continue
        if messages_review_row_rejected(row):
            skipped["rejected"] += 1
            continue
        reason = messages_review_people_selection_reason(row)
        if not reason:
            skipped["not_selected"] += 1
            continue
        eligible_rows += 1
        selection_counts[reason] = selection_counts.get(reason, 0) + 1
        candidate = review_row_to_messages_people(row, review_csv, reason)
        public_identifier = candidate.get("public_identifier", "")
        if public_identifier in by_public_identifier:
            skipped["duplicate_public_identifier"] += 1
            by_public_identifier[public_identifier] = merge_messages_people_candidate(by_public_identifier[public_identifier], candidate)
        else:
            by_public_identifier[public_identifier] = candidate
    output_rows = [by_public_identifier[key] for key in sorted(by_public_identifier)]
    if output_rows:
        write_csv_rows(output_csv, PEOPLE_SCHEMA_COLUMNS, output_rows)
    summary = {
        "review_csv": str(review_csv),
        "people_csv": str(output_csv) if output_rows else "",
        "total_rows": len(rows),
        "eligible_rows": eligible_rows,
        "rows_written": len(output_rows),
        "selection_counts": selection_counts,
        "skipped": skipped,
    }
    if manifest_path:
        write_json(manifest_path, summary)
        summary["manifest"] = str(manifest_path)
    return summary
def resolve_messages_review_csv(ledger: dict[str, Any]) -> str:
    input_cfg = ledger.get("input", {}) or {}
    artifacts = ledger.get("artifacts", {}) or {}
    review_csv = artifacts.get("messages_review_csv") or input_cfg.get("messages_review_csv") or ""
    return str(review_csv or "")


def enrich_people_payload_from_ledger(child_ledger: Path) -> dict[str, Any]:
    child = read_json(child_ledger, {}) or {}
    if child.get("status") != "completed":
        return {}
    return {"status": "completed", "ledger": str(child_ledger), "artifact_dir": child.get("artifact_dir") or child.get("run_dir"), "artifacts": child.get("artifacts", {})}


def run_messages_enrichment(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    artifacts = ledger.setdefault("artifacts", {})
    review_csv_text = resolve_messages_review_csv(ledger)
    if not review_csv_text:
        mark_step(ledger, "messages_enrich_people", "skipped", reason="no messages research_review.csv")
        return True
    review_csv = Path(review_csv_text)
    artifacts["messages_review_csv"] = str(review_csv)
    run_dir = artifact_dir_from_ledger(ledger) / "messages"
    input_people = run_dir / "people.input.csv"
    manifest_path = run_dir / "people_manifest.json"
    begin_step(ledger_path, ledger, "messages_enrich_people", "Preparing reviewed Messages LinkedIn rows for local profile enrichment.")
    materialized = materialize_messages_review_people(review_csv, input_people, manifest_path)
    artifacts["messages_people_input_manifest"] = str(manifest_path)
    if materialized.get("people_csv"):
        artifacts["messages_people_input_csv"] = materialized["people_csv"]
    if not materialized.get("rows_written"):
        mark_step(ledger, "messages_enrich_people", "skipped", summary=materialized, reason="no eligible reviewed Messages LinkedIn rows")
        save_ledger(ledger_path, ledger)
        emit_progress("No reviewed Messages LinkedIn rows need local enrichment.")
        return True

    child_ledger = artifact_dir_from_ledger(ledger) / "messages-enrich-people.ledger.json"
    artifacts["messages_enrich_people_ledger"] = str(child_ledger)
    enrich_cmd = py_cmd(
        "packs/ingestion/primitives/enrich_people/enrich_people.py",
        "run",
        "--input", str(input_people),
        "--ledger", str(child_ledger),
        "--artifact-dir", str(run_dir / "enrichment"),
    )
    code, enrich_payload, stderr = run_cmd(enrich_cmd)
    if code == 20 or enrich_payload.get("status") == "blocked_approval":
        ledger["blocked"] = {"step_id": "messages_enrich_people", "child_ledger": str(child_ledger), "child": enrich_payload}
        mark_step(ledger, "messages_enrich_people", "blocked", summary=materialized, payload=enrich_payload)
        save_ledger(ledger_path, ledger)
        emit({"status": "blocked_approval", "step_id": "messages_enrich_people", "ledger": str(ledger_path), "child": enrich_payload})
        return False
    if code != 0:
        mark_step(ledger, "messages_enrich_people", "failed", summary=materialized, error=stderr or enrich_payload)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "messages_enrich_people", "error": stderr or enrich_payload})
        return False
    for key, value in (enrich_payload.get("artifacts") or {}).items():
        artifacts[f"messages_enriched_{key}"] = value
    enriched_people = enrich_payload.get("artifacts", {}).get("people_csv") or materialized.get("people_csv")
    final_people = str(enriched_people or "")
    messages_merge: dict[str, Any] = {"status": "skipped", "people_csv": str(DEFAULT_BASE_DIR / "messages" / "people.messages.csv"), "rows": 0, "input_csvs": []}
    if enriched_people:
        messages_merge = materialize_messages_merged_people_csv(
            [str(enriched_people)],
            DEFAULT_BASE_DIR / "messages" / "people.messages.csv",
        )
        artifacts["messages_merged_people"] = messages_merge
        if messages_merge.get("status") == "completed" and messages_merge.get("people_csv"):
            final_people = str(messages_merge["people_csv"])
            artifacts["messages_merged_people_csv"] = final_people
        artifacts["messages_people_csv"] = final_people
        artifacts["messages_final_people_csvs"] = [final_people] if final_people else []
        # Keep the legacy plural key as an alias, but never let it accumulate
        # old run dirs. Fan-in should see one canonical Messages artifact.
        artifacts["messages_people_csvs"] = [final_people] if final_people else []
    enrich_payload = {**enrich_payload, "messages_merged_people": messages_merge}
    if final_people:
        artifacts["messages_directory_checkpoint"] = commit_people_csv_to_directory(
            ledger.get("input", {}),
            artifacts,
            final_people,
            source="messages",
        )
    mark_step(ledger, "messages_enrich_people", "completed", summary=materialized, payload=enrich_payload)
    emit_progress(f"Messages profile enrichment completed: {materialized.get('rows_written', 0)} LinkedIn rows prepared.")
    return True


def messages_discovery_inputs(accounts_path: Path) -> dict[str, Any]:
    accounts = read_accounts(accounts_path)
    channel = account_channel(accounts, "messages")
    cfg = account_config(accounts, "messages")
    if not channel_is_linked(accounts, "messages"):
        return {"linked": False, "include_imessage": False, "include_whatsapp": False}
    imessage_cfg = cfg.get("imessage") if isinstance(cfg.get("imessage"), dict) else {}
    whatsapp_cfg = cfg.get("whatsapp") if isinstance(cfg.get("whatsapp"), dict) else {}
    include_imessage = str(imessage_cfg.get("status") or "").strip().lower() != "skipped"
    include_whatsapp = (
        str(whatsapp_cfg.get("status") or "").strip().lower() == "linked"
        or bool(whatsapp_cfg.get("authenticated") is True)
    )
    return {
        "linked": bool(include_imessage or include_whatsapp),
        "include_imessage": include_imessage,
        "include_whatsapp": include_whatsapp,
    }


def discover(
    *,
    accounts_path: Path = DEFAULT_ACCOUNTS,
    ledger_path: Path = DEFAULT_MESSAGES_DISCOVERY_LEDGER,
    output_dir: Path = DEFAULT_MESSAGES_OUTPUT_DIR,
    import_ledger: Path = DEFAULT_IMPORT_CONTACTS_LEDGER,
    wacli_max_messages: int = DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
) -> dict[str, Any]:
    inputs = messages_discovery_inputs(accounts_path)
    ledger = read_json(ledger_path, {}) or {}
    ledger.update({
        "primitive": "discover_contacts_messages",
        "source": "messages",
        "updated_at": now_iso(),
        "accounts_path": str(accounts_path),
        "output_dir": str(output_dir),
        "import_ledger": str(import_ledger),
    })
    contacts_csv = output_dir / "contacts.csv"
    manifest_json = output_dir / "manifest.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    if not inputs["linked"]:
        payload = {"status": "skipped", "source": "messages", "reason": "messages_not_linked", "contacts_csv": str(contacts_csv)}
        ledger["status"] = "skipped"
        ledger["payload"] = payload
        payload["updated_at"] = now_iso()
        write_json(manifest_json, payload)
        write_json(ledger_path, ledger)
        return payload

    cmd = py_cmd(
        "packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py",
        "run",
        "--ledger", str(import_ledger),
        "--reuse-existing-artifacts",
        "--include-contact-merge",
        "--wacli-max-messages", str(wacli_max_messages),
    )
    if inputs["include_imessage"]:
        cmd.append("--include-imessage")
    if inputs["include_whatsapp"]:
        cmd.append("--include-whatsapp")
    code, payload, stderr = run_cmd(cmd)
    if code != 0:
        blocked_statuses = {"blocked_user_action", "blocked_approval"}
        status = str(payload.get("status") or "").strip()
        result = {
            "status": status if status in blocked_statuses else "failed",
            "source": "messages",
            "error": stderr or payload,
            "child": payload,
            "contacts_csv": str(contacts_csv),
            "updated_at": now_iso(),
        }
        ledger["status"] = result["status"]
        ledger["payload"] = result
        write_json(manifest_json, result)
        write_json(ledger_path, ledger)
        return result

    child_contacts = Path(str((payload.get("artifacts") or {}).get("contacts_csv") or ".powerpacks/messages/contacts.csv"))
    if child_contacts.exists():
        shutil.copyfile(child_contacts, contacts_csv)
    else:
        write_csv_rows(contacts_csv, ["phone", "name", "source", "message_count", "last_message"], [])
    _, rows = read_csv_rows(contacts_csv)
    result = {
        "status": "completed",
        "source": "messages",
        "contacts_csv": str(contacts_csv),
        "contacts": len(rows),
        "include_imessage": inputs["include_imessage"],
        "include_whatsapp": inputs["include_whatsapp"],
        "privacy": {
            "message_bodies_read": False,
            "powerset_sync_ran": False,
            "llm_review_ran": False,
            "deep_research_ran": False,
            "upload_ran": False,
        },
        "child": payload,
        "updated_at": now_iso(),
    }
    write_json(manifest_json, result)
    ledger["status"] = "completed"
    ledger["payload"] = result
    write_json(ledger_path, ledger)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover iMessage/WhatsApp contacts")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("discover", help="Discover message contacts")
    run.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    run.add_argument("--ledger", type=Path, default=DEFAULT_MESSAGES_DISCOVERY_LEDGER)
    run.add_argument("--output-dir", type=Path, default=DEFAULT_MESSAGES_OUTPUT_DIR)
    run.add_argument("--import-ledger", type=Path, default=DEFAULT_IMPORT_CONTACTS_LEDGER)
    run.add_argument("--wacli-max-messages", type=int, default=DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "discover":
        emit(discover(
            accounts_path=args.accounts,
            ledger_path=args.ledger,
            output_dir=args.output_dir,
            import_ledger=args.import_ledger,
            wacli_max_messages=args.wacli_max_messages,
        ))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
