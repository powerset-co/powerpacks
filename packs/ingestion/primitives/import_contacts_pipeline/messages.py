#!/usr/bin/env python3
"""Import/enrich reviewed Messages contacts."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        extract_public_identifier,
        latest_interaction,
        merge_interaction_counts,
        normalize_linkedin_url,
        normalize_people_row,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        DEFAULT_DIRECTORY_CSV,
        emit,
        read_csv_rows,
        read_accounts,
        sha,
        unique_strings,
        write_csv_rows,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.directory import (
        DIRECTORY_COLUMNS,
        directory_rows_from_people_csv,
        merge_directory_rows,
        normalized_directory_row,
        people_directory_source_key,
        phones_from_value,
    )
    from packs.ingestion.primitives.enrich_people import enrich_people
    from packs.ingestion.primitives.import_contacts_pipeline.common import (
        DEFAULT_ACCOUNTS,
        DEFAULT_IMPORT_DIR,
        DEFAULT_PROFILE_CACHE_DIR,
        copy_people_csv,
        csv_count,
        directory_row_matches_source,
        directory_source_account_quality,
        import_manifest_current,
        normalize_directory_source_accounts,
        write_manifest,
    )
    from packs.ingestion.primitives.prepare_research_queue.prepare_research_queue import (
        RESEARCH_COLUMNS,
    )
    from packs.shared.csv_io import CsvIO
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        extract_public_identifier,
        latest_interaction,
        merge_interaction_counts,
        normalize_linkedin_url,
        normalize_people_row,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        DEFAULT_DIRECTORY_CSV,
        emit,
        read_csv_rows,
        read_accounts,
        sha,
        unique_strings,
        write_csv_rows,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.directory import (
        DIRECTORY_COLUMNS,
        directory_rows_from_people_csv,
        merge_directory_rows,
        normalized_directory_row,
        people_directory_source_key,
        phones_from_value,
    )
    from packs.ingestion.primitives.enrich_people import enrich_people
    from packs.ingestion.primitives.import_contacts_pipeline.common import (
        DEFAULT_ACCOUNTS,
        DEFAULT_IMPORT_DIR,
        DEFAULT_PROFILE_CACHE_DIR,
        copy_people_csv,
        csv_count,
        directory_row_matches_source,
        directory_source_account_quality,
        import_manifest_current,
        normalize_directory_source_accounts,
        write_manifest,
    )
    from packs.ingestion.primitives.prepare_research_queue.prepare_research_queue import (
        RESEARCH_COLUMNS,
    )
    from packs.shared.csv_io import CsvIO


TRUTHY = {"1", "true", "yes", "y", "on"}
FALSY = {"0", "false", "no", "n", "off"}
INCLUDE_DECISIONS = {"include", "approved", "approve", "yes", "true", "1"}
EXCLUDE_DECISIONS = {"exclude", "excluded", "skip", "skipped", "no", "false", "0"}
RESEARCH_REVIEW_SOURCES = {
    "llm_network_review",
    "retarget_research",
    "retarget_refresh",
    "deep_research",
}
MESSAGES_IMPORT_CONTRACT = "messages-stateless-v2"
DEFAULT_RESEARCH_QUEUE = Path(".powerpacks/messages/research_queue.csv")


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
    return (
        approved is False
        or upload_decision is False
        or exclude_decision is False
        or enrich_decision is False
    )


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
    return any(
        (row.get(key) or "").strip()
        for key in (
            "top_title_company_pairs",
            "top_titles",
            "top_companies",
            "schools",
            "short_reason",
        )
    )


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
    first = next(
        (part.strip() for part in re.split(r"\s*\|\s*", pair_text or "") if part.strip()),
        "",
    )
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
    for key, channel in (
        ("imessage_message_count", "imessage"),
        ("whatsapp_message_count", "whatsapp"),
    ):
        try:
            count = int(float(row.get(key) or 0))
        except ValueError:
            count = 0
        if count > 0 and channel not in channels:
            channels.append(channel)
    return channels or ["messages"]


def review_row_to_messages_people(
    row: dict[str, str],
    review_csv: Path,
    reason: str,
) -> dict[str, str]:
    linkedin_url = messages_review_linkedin_url(row)
    public_identifier = extract_public_identifier(linkedin_url)
    full_name = (
        (row.get("network_name") if reason == "in_network" else "")
        or row.get("full_name")
        or row.get("network_name")
        or ""
    )
    first_name, last_name = split_full_name(full_name)
    current_title, current_company = split_title_company(
        row.get("top_title_company_pairs") or row.get("top_titles", "")
    )
    phone = (row.get("phone_e164") or "").strip()
    channels = messages_source_channels(row)
    summary_parts = [f"selection={reason}"]
    if row.get("short_reason"):
        summary_parts.append(f"review_reason={row.get('short_reason')}")
    interaction_counts: dict[str, int] = {}
    for count_key, channel in (
        ("imessage_message_count", "imessage"),
        ("whatsapp_message_count", "whatsapp"),
    ):
        try:
            count = int(float(row.get(count_key) or 0))
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            interaction_counts[channel] = count
    last_interaction = latest_interaction(
        row.get("imessage_last_message"),
        row.get("whatsapp_last_message"),
        row.get("last_message"),
    )
    people = {
        "id": row.get("network_person_id")
        or f"message-linkedin:{sha(public_identifier or linkedin_url, 16)}",
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
        "interaction_counts": (
            json.dumps(interaction_counts, ensure_ascii=False) if interaction_counts else ""
        ),
        "last_interaction": last_interaction,
    }
    return normalize_people_row(people)


def merge_messages_people_candidate(
    existing: dict[str, str],
    incoming: dict[str, str],
) -> dict[str, str]:
    merged = dict(existing)
    for key in (
        "primary_phone",
        "full_name",
        "first_name",
        "last_name",
        "headline",
        "summary",
        "city",
        "country",
        "current_title",
        "current_company",
    ):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming[key]
    phones = unique_strings([
        *phones_from_value(merged.get("all_phones", "")),
        *phones_from_value(merged.get("primary_phone", "")),
        *phones_from_value(incoming.get("all_phones", "")),
        *phones_from_value(incoming.get("primary_phone", "")),
    ])
    if phones:
        merged["primary_phone"] = merged.get("primary_phone") or phones[0]
        merged["all_phones"] = json.dumps(phones, ensure_ascii=False)
    channels = unique_strings(
        (merged.get("source_channels", "").split(",") if merged.get("source_channels") else [])
        + (incoming.get("source_channels", "").split(",") if incoming.get("source_channels") else [])
    )
    if channels:
        merged["source_channels"] = ",".join(channels)
    providers = unique_strings(
        [merged.get("enrichment_provider", ""), incoming.get("enrichment_provider", "")]
    )
    if providers:
        merged["enrichment_provider"] = ",".join(providers)
    counts = merge_interaction_counts(
        merged.get("interaction_counts"),
        incoming.get("interaction_counts"),
    )
    merged["interaction_counts"] = json.dumps(counts, ensure_ascii=False) if counts else ""
    merged["last_interaction"] = latest_interaction(
        merged.get("last_interaction"), incoming.get("last_interaction")
    )
    return normalize_people_row(merged)


def selected_messages_review_people(
    review_csv: Path,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not review_csv.exists():
        return ({
            "review_csv": str(review_csv),
            "people_csv": "",
            "total_rows": 0,
            "eligible_rows": 0,
            "rows_written": 0,
            "selection_counts": {},
            "skipped": {"missing_review_csv": 1},
        }, [])
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
            by_public_identifier[public_identifier] = merge_messages_people_candidate(
                by_public_identifier[public_identifier], candidate
            )
        else:
            by_public_identifier[public_identifier] = candidate
    output_rows = [by_public_identifier[key] for key in sorted(by_public_identifier)]
    summary = {
        "review_csv": str(review_csv),
        "people_csv": "",
        "total_rows": len(rows),
        "eligible_rows": eligible_rows,
        "rows_written": len(output_rows),
        "selection_counts": selection_counts,
        "skipped": skipped,
    }
    return summary, output_rows


def materialize_messages_review_people(
    review_csv: Path,
    output_csv: Path,
) -> dict[str, Any]:
    summary, output_rows = selected_messages_review_people(review_csv)
    if review_csv.exists():
        write_csv_rows(output_csv, PEOPLE_SCHEMA_COLUMNS, output_rows)
        summary["people_csv"] = str(output_csv)
    return summary


def directory_source_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    for row in read_csv_rows(path)[1]:
        normalized = normalized_directory_row(row, source="directory")
        if normalized.get("source_key"):
            keys.add(normalized["source_key"])
    return keys


def messages_people_directory_keys(rows: list[dict[str, str]]) -> set[str]:
    keys: set[str] = set()
    for row in rows:
        public_identifier = row.get("public_identifier") or extract_public_identifier(
            row.get("linkedin_url") or ""
        )
        if not public_identifier:
            continue
        key = people_directory_source_key(
            row,
            "messages",
            row.get("source_channels") or "messages",
            public_identifier,
        )
        if key:
            keys.add(key)
    return keys


def messages_import_diff(review_csv: Path) -> dict[str, Any]:
    materialized, people_rows = selected_messages_review_people(review_csv)
    candidate_keys = messages_people_directory_keys(people_rows)
    existing_keys = directory_source_keys(DEFAULT_DIRECTORY_CSV)
    return {
        "materialized": materialized,
        "candidate_rows": len(candidate_keys),
        "new_rows": len(candidate_keys - existing_keys),
        "existing_directory_rows": len(existing_keys),
    }


def people_csv_schema_stale(path: Path) -> bool:
    """True when an existing people.csv predates the interaction-count
    columns. Input fingerprints can't catch this (the code changed, not the
    data), so the import self-invalidates instead of trusting its manifest."""
    if not path.exists():
        return False
    with path.open(newline="", encoding="utf-8") as handle:
        header = next(CsvIO.reader(handle), [])
    return bool(header) and "interaction_counts" not in header


def enrich_messages_people(input_csv: Path, enrichment_dir: Path) -> dict[str, Any]:
    if enrichment_dir.exists():
        shutil.rmtree(enrichment_dir)
    context: dict[str, Any] = {
        "artifact_dir": str(enrichment_dir),
        "input": {
            "input_csv": str(input_csv),
            "limit": None,
            "force": False,
            "profile_cache_dir": str(DEFAULT_PROFILE_CACHE_DIR),
            "refresh_cache": False,
            "company_corpus_jsonl": [],
            "sleep_seconds": 0.0,
            "max_workers": enrich_people.DEFAULT_RAPIDAPI_MAX_WORKERS,
            "max_rpm": enrich_people.DEFAULT_RAPIDAPI_MAX_RPM,
            "failure_retry_hours": enrich_people.DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS,
        },
        "artifacts": {},
    }
    prepare = enrich_people.step_prepare_queue(context)
    provider = enrich_people.step_enrich_linkedin(context)
    merge = enrich_people.step_merge_people(context)
    return {
        "status": "completed",
        "people_csv": str(context["artifacts"]["people_csv"]),
        "prepare": prepare,
        "provider": provider,
        "merge": merge,
        "artifacts": context["artifacts"],
    }


def replace_messages_directory_rows(
    people_csv: Path,
    directory_csv: Path | None = None,
) -> dict[str, Any]:
    directory_csv = directory_csv or DEFAULT_DIRECTORY_CSV
    retained: dict[str, dict[str, str]] = {}
    existing_rows = read_csv_rows(directory_csv)[1] if directory_csv.exists() else []
    removed_rows = 0
    for row in existing_rows:
        normalized = normalized_directory_row(row, source="directory")
        if not normalized:
            continue
        if directory_row_matches_source(normalized, "messages") or normalized.get(
            "source_key", ""
        ).startswith("messages:"):
            removed_rows += 1
            continue
        retained[normalized["source_key"]] = normalized
    incoming = directory_rows_from_people_csv(people_csv, source="messages")
    merged = merge_directory_rows(incoming, retained)
    write_csv_rows(directory_csv, DIRECTORY_COLUMNS, merged)
    return {
        "path": str(directory_csv),
        "existing_rows": len(existing_rows),
        "removed_messages_rows": removed_rows,
        "imported_messages_rows": len(incoming),
        "rows": len(merged),
    }


def empty_research_queue_error(queue_csv: Path) -> dict[str, Any] | None:
    if not queue_csv.is_file():
        return {
            "status": "failed",
            "reason": "messages_research_queue_missing",
            "message": f"Research queue not found: {queue_csv}",
            "queue_csv": str(queue_csv),
        }
    try:
        fields, rows = read_csv_rows(queue_csv)
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        return {
            "status": "failed",
            "reason": "messages_research_queue_unreadable",
            "message": f"Could not read research queue {queue_csv}: {exc}",
            "queue_csv": str(queue_csv),
        }
    missing_fields = sorted(set(RESEARCH_COLUMNS) - set(fields))
    if missing_fields:
        return {
            "status": "failed",
            "reason": "messages_research_queue_schema_invalid",
            "message": "Research queue does not match the canonical schema.",
            "queue_csv": str(queue_csv),
            "missing_fields": missing_fields,
        }
    if rows:
        return {
            "status": "failed",
            "reason": "messages_research_queue_not_empty",
            "message": (
                f"Refusing to clear Messages artifacts: research queue contains {len(rows)} row(s)."
            ),
            "queue_csv": str(queue_csv),
            "rows": len(rows),
        }
    return None


def reconcile_empty(args: argparse.Namespace) -> dict[str, Any]:
    """Clear the Messages source slice only after proving the current queue is empty."""
    queue_csv = Path(getattr(args, "queue", DEFAULT_RESEARCH_QUEUE))
    queue_error = empty_research_queue_error(queue_csv)
    if queue_error:
        return queue_error

    read_accounts(args.accounts)
    import_dir = DEFAULT_IMPORT_DIR / "messages"
    input_people = import_dir / "people.input.csv"
    people_csv = import_dir / "people.csv"
    enrichment_dir = import_dir / "enrichment"
    if enrichment_dir.exists():
        shutil.rmtree(enrichment_dir)
    write_csv_rows(input_people, PEOPLE_SCHEMA_COLUMNS, [])
    write_csv_rows(people_csv, PEOPLE_SCHEMA_COLUMNS, [])

    review_csv = Path(".powerpacks/messages/research_review.csv")
    if review_csv.exists():
        review_csv.unlink()

    directory_replacement = replace_messages_directory_rows(people_csv)
    directory_normalization = normalize_directory_source_accounts("messages")
    directory_quality = directory_source_account_quality("messages")
    status = "completed" if directory_quality["status"] == "ok" else "failed"
    return write_manifest("messages", {
        "status": status,
        "reason": (
            "empty_current_research_queue"
            if status == "completed"
            else "directory_source_account_quality_failed"
        ),
        "input": {
            "pipeline_contract": MESSAGES_IMPORT_CONTRACT,
            "mode": "empty",
            "discovery_manifest": str(
                DEFAULT_BASE_DIR / "discover" / "messages" / "manifest.json"
            ),
            "contacts_csv": str(
                DEFAULT_BASE_DIR / "discover" / "messages" / "contacts.csv"
            ),
            "research_queue_csv": str(queue_csv),
        },
        "outputs": {
            "people_input_csv": str(input_people),
            "people_csv": str(people_csv),
        },
        "stats": {"people": 0, "candidates": 0},
        "materialized": {
            "review_csv": "",
            "people_csv": str(input_people),
            "total_rows": 0,
            "eligible_rows": 0,
            "rows_written": 0,
            "selection_counts": {},
            "skipped": {"empty_current_research_queue": 1},
        },
        "directory": {
            "path": str(DEFAULT_DIRECTORY_CSV),
            "replacement": directory_replacement,
            "normalization": directory_normalization,
            "quality": directory_quality,
        },
    }, import_dir=DEFAULT_IMPORT_DIR)


def run(args: argparse.Namespace) -> dict:
    import_dir = DEFAULT_IMPORT_DIR / "messages"
    people_csv_path = import_dir / "people.csv"
    schema_stale = people_csv_schema_stale(people_csv_path)
    expected_input = {
        "pipeline_contract": MESSAGES_IMPORT_CONTRACT,
        "mode": "review",
    }
    current = None if schema_stale else import_manifest_current(
        "messages",
        expected_input,
        import_dir=DEFAULT_IMPORT_DIR,
    )
    if current:
        return current
    read_accounts(args.accounts)
    review_csv = Path(".powerpacks/messages/research_review.csv")
    manifest_input = {
        **expected_input,
        "review_csv": str(review_csv),
        "discovery_manifest": str(DEFAULT_BASE_DIR / "discover" / "messages" / "manifest.json"),
        "contacts_csv": str(DEFAULT_BASE_DIR / "discover" / "messages" / "contacts.csv"),
    }
    if not review_csv.exists():
        return write_manifest("messages", {
            "status": "failed",
            "reason": "messages_review_csv_missing",
            "message": f"Review Messages contacts before import: {review_csv}",
            "input": manifest_input,
            "outputs": {},
            "stats": {"people": 0, "candidates": 0},
        }, import_dir=DEFAULT_IMPORT_DIR)
    diff = messages_import_diff(review_csv)
    if diff["new_rows"] > 0 and not args.confirm_import:
        return write_manifest("messages", {
            "status": "blocked_approval",
            "approval_type": "import_confirmation",
            "message": f"Import {diff['new_rows']} reviewed Messages LinkedIn profiles into your local network?",
            "blocked": {
                "status": "blocked_approval",
                "approval_type": "import_confirmation",
                "source": "messages",
                "message": f"Import {diff['new_rows']} reviewed Messages LinkedIn profiles into your local network?",
                "payload": diff,
            },
            "input": manifest_input,
            "outputs": {},
            "stats": {
                "people": 0,
                "candidates": csv_count(str(review_csv)),
                "candidate_directory_rows": diff["candidate_rows"],
                "new_directory_rows": diff["new_rows"],
            },
            "diff": diff,
        }, import_dir=DEFAULT_IMPORT_DIR)
    input_people = import_dir / "people.input.csv"
    materialized = materialize_messages_review_people(review_csv, input_people)
    try:
        enrichment = enrich_messages_people(input_people, import_dir / "enrichment")
    except Exception as exc:
        return write_manifest("messages", {
            "status": "failed",
            "reason": "messages_profile_enrichment_failed",
            "error": str(exc),
            "input": manifest_input,
            "outputs": {"people_input_csv": str(input_people)},
            "stats": {
                "people": 0,
                "candidates": csv_count(str(review_csv)),
            },
            "diff": diff,
            "materialized": materialized,
        }, import_dir=DEFAULT_IMPORT_DIR)
    people_csv = copy_people_csv(
        "messages",
        enrichment["people_csv"],
        import_dir=DEFAULT_IMPORT_DIR,
    )
    directory_replacement = replace_messages_directory_rows(Path(people_csv))
    directory_normalization = normalize_directory_source_accounts("messages")
    directory_quality = directory_source_account_quality("messages")
    status = "completed" if directory_quality["status"] == "ok" else "failed"
    return write_manifest("messages", {
        "status": status,
        "reason": "directory_source_account_quality_failed" if status == "failed" and directory_quality.get("status") == "failed" else "",
        "input": manifest_input,
        "outputs": {
            "people_input_csv": str(input_people),
            "people_csv": people_csv,
        },
        "stats": {
            "people": csv_count(people_csv),
            "candidates": csv_count(str(review_csv)),
        },
        "diff": diff,
        "materialized": materialized,
        "artifacts": {"enrichment": enrichment},
        "directory": {
            "path": str(DEFAULT_DIRECTORY_CSV),
            "replacement": directory_replacement,
            "normalization": directory_normalization,
            "quality": directory_quality,
        },
    }, import_dir=DEFAULT_IMPORT_DIR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import/enrich reviewed Messages contacts")
    parser.add_argument("command", choices=["run", "reconcile-empty"])
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument("--operator-id", default="local")
    parser.add_argument("--confirm-import", action="store_true")
    parser.add_argument("--queue", type=Path, default=DEFAULT_RESEARCH_QUEUE)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = reconcile_empty(args) if args.command == "reconcile-empty" else run(args)
    emit(payload)
    return 20 if payload.get("status") == "blocked_approval" else 1 if payload.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
