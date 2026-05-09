#!/usr/bin/env python3
"""Build a targeted re-research queue from review CSV feedback hints.

Stdlib-only. Reads `research_review.csv`, finds rows with `retarget_hint`, and
writes a deep_research_contacts-compatible queue for hints that have not already
been attempted for that person.

Tracking is by `(source_handle, normalized_hint_hash)` in a local ledger so the
same feedback is not submitted repeatedly. If feedback changes, it creates a new
retarget attempt.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REVIEW_CSV = Path(".powerpacks/messages/research_review.csv")
DEFAULT_BASE_QUEUE = Path(".powerpacks/messages/research_queue.csv")
DEFAULT_OUTPUT = Path(".powerpacks/messages/retarget_queue.csv")
DEFAULT_LEDGER = Path(".powerpacks/messages/retarget_attempts.json")
DEFAULT_OUTPUT_DIR = Path(".powerpacks/messages/research_retarget")

QUEUE_COLUMNS = [
    "handle",
    "display_name",
    "first_name",
    "last_name",
    "primary_email",
    "domain",
    "total_messages",
    "imessage_message_count",
    "whatsapp_message_count",
    "bio",
    "all_emails",
    "operator_id",
    "gmail_token_id",
    "source_channel",
    "follower_count",
    "following_count",
    "verified",
    "location",
    "website_url",
    "twitter_user_id",
    "first_seen_at",
    "profile_image_url",
    "person_id",
    "moe_verdict",
    "moe_composite",
    "moe_confidence",
    "moe_top_expert",
    "moe_top_signal",
    "moe_top_reasoning",
    "whale_names",
    "operator",
    "phone_e164",
    "phone_last4",
    "area_code",
    "message_source",
    "last_message",
    "imessage_last_message",
    "whatsapp_last_message",
    "is_in_group_chats",
    "match_status",
    "group_names",
    "match_confidence",
    "match_method",
    "match_reason",
    "retarget_hint",
    "retarget_source_handle",
    "retarget_hint_hash",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "attempts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "attempts": {}}
    if not isinstance(data, dict):
        return {"version": 1, "attempts": {}}
    data.setdefault("version", 1)
    data.setdefault("attempts", {})
    return data


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [{key: value or "" for key, value in row.items()} for row in csv.DictReader(handle)]


def normalize_hint(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def hint_hash(value: str) -> str:
    normalized = normalize_hint(value).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def retarget_handle(source_handle: str, h: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_handle or "unknown").strip("_") or "unknown"
    return f"{safe}__retarget_{h[:10]}"


def split_name(name: str) -> tuple[str, str]:
    parts = (name or "").strip().split(None, 1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def phone_last4(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-4:] if digits else ""


def base_rows_by_handle(path: Path) -> dict[str, dict[str, str]]:
    return {(row.get("handle") or "").strip(): row for row in load_csv(path) if (row.get("handle") or "").strip()}


def synthesize_base_row(review_row: dict[str, str]) -> dict[str, str]:
    name = (review_row.get("full_name") or "").strip()
    first, last = split_name(name)
    phone = (review_row.get("phone_e164") or "").strip()
    return {
        "handle": review_row.get("handle", ""),
        "display_name": name,
        "first_name": first,
        "last_name": last,
        "primary_email": "",
        "domain": "",
        "total_messages": review_row.get("total_messages", "") or "0",
        "imessage_message_count": review_row.get("imessage_message_count", "") or "",
        "whatsapp_message_count": review_row.get("whatsapp_message_count", "") or "",
        "bio": "",
        "all_emails": "",
        "operator_id": "",
        "gmail_token_id": "",
        "source_channel": "phone",
        "follower_count": "",
        "following_count": "",
        "verified": "",
        "location": "",
        "website_url": "",
        "twitter_user_id": "",
        "first_seen_at": "",
        "profile_image_url": "",
        "person_id": "",
        "moe_verdict": "",
        "moe_composite": "",
        "moe_confidence": "",
        "moe_top_expert": "",
        "moe_top_signal": "",
        "moe_top_reasoning": "",
        "whale_names": "",
        "operator": "",
        "phone_e164": phone,
        "phone_last4": phone_last4(phone),
        "area_code": review_row.get("area_code", "") or "",
        "message_source": review_row.get("message_source", "") or "",
        "last_message": review_row.get("last_message", "") or "",
        "imessage_last_message": review_row.get("imessage_last_message", "") or "",
        "whatsapp_last_message": review_row.get("whatsapp_last_message", "") or "",
        "is_in_group_chats": "",
        "match_status": "",
        "group_names": review_row.get("group_names", "") or "",
        "match_confidence": "",
        "match_method": "",
        "match_reason": "",
    }


def already_attempted(
    ledger: dict[str, Any],
    source_handle: str,
    h: str,
    retarget_output_dir: Path,
    queue_handle: str,
    include_failed: bool,
) -> bool:
    if (retarget_output_dir / queue_handle / "01_research_parallel.json").exists():
        return True
    attempts = ((ledger.get("attempts") or {}).get(source_handle) or [])
    for attempt in attempts:
        if attempt.get("hint_hash") != h:
            continue
        status = attempt.get("status") or "queued"
        if status == "failed" and include_failed:
            continue
        return True
    return False


def cmd_prepare(args: argparse.Namespace) -> int:
    review_csv = Path(args.review_csv)
    base_queue = Path(args.base_queue)
    output = Path(args.output)
    ledger_path = Path(args.ledger)
    retarget_output_dir = Path(args.retarget_output_dir)
    manifest_path = Path(args.manifest) if args.manifest else output.with_suffix(output.suffix + ".manifest.json")

    review_rows = load_csv(review_csv)
    base_by_handle = base_rows_by_handle(base_queue)
    ledger = read_json(ledger_path)

    rows: list[dict[str, str]] = []
    attempts_to_record: list[dict[str, str]] = []
    counts = {
        "review_rows": len(review_rows),
        "with_feedback": 0,
        "queued": 0,
        "skipped_already_attempted": 0,
        "skipped_missing_handle": 0,
        "synthesized_from_review": 0,
    }

    for review_row in review_rows:
        source_handle = (review_row.get("handle") or "").strip()
        hint = normalize_hint(review_row.get("retarget_hint", ""))
        if not hint:
            continue
        counts["with_feedback"] += 1
        if not source_handle:
            counts["skipped_missing_handle"] += 1
            continue
        h = hint_hash(hint)
        queue_handle = retarget_handle(source_handle, h)
        if already_attempted(ledger, source_handle, h, retarget_output_dir, queue_handle, args.include_failed):
            counts["skipped_already_attempted"] += 1
            continue
        base = dict(base_by_handle.get(source_handle) or {})
        if not base:
            base = synthesize_base_row(review_row)
            counts["synthesized_from_review"] += 1
        out_row = {key: base.get(key, "") for key in QUEUE_COLUMNS}
        out_row.update({
            "handle": queue_handle,
            "retarget_source_handle": source_handle,
            "retarget_hint": hint,
            "retarget_hint_hash": h,
        })
        if not out_row.get("display_name"):
            out_row["display_name"] = review_row.get("full_name", "") or source_handle
        rows.append(out_row)
        attempts_to_record.append({
            "source_handle": source_handle,
            "queue_handle": queue_handle,
            "hint_hash": h,
            "hint": hint,
        })
        counts["queued"] += 1

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=QUEUE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    if not args.no_mark_queued:
        attempts = ledger.setdefault("attempts", {})
        queued_at = now_iso()
        for attempt in attempts_to_record:
            source_handle = attempt["source_handle"]
            attempts.setdefault(source_handle, []).append({
                "hint_hash": attempt["hint_hash"],
                "hint": attempt["hint"],
                "queue_handle": attempt["queue_handle"],
                "status": "queued",
                "queued_at": queued_at,
                "queue_csv": str(output),
                "retarget_output_dir": str(retarget_output_dir),
            })
        write_json(ledger_path, ledger)

    manifest = {
        "primitive": "prepare_retarget_queue",
        "command": "prepare",
        "status": "ok",
        "review_csv": str(review_csv),
        "base_queue": str(base_queue),
        "output": str(output),
        "manifest_path": str(manifest_path),
        "ledger": str(ledger_path),
        "retarget_output_dir": str(retarget_output_dir),
        "counts": counts,
        "rows_written": len(rows),
        "parallel_cost_estimate_usd": {
            "core_usd": round(len(rows) * 0.025, 2),
            "core2x_usd": round(len(rows) * 0.05, 2),
            "pro_usd": round(len(rows) * 0.10, 2),
        },
    }
    write_json(manifest_path, manifest)
    emit(manifest)
    return 0


def profile_positions(profile: dict[str, Any]) -> tuple[str, str, str]:
    titles: list[str] = []
    companies: list[str] = []
    pairs: list[str] = []
    for pos in (profile.get("positions") or [])[:6]:
        title = (pos.get("title") or "").strip()
        company = (pos.get("company_name") or "").strip()
        if title:
            titles.append(title)
        if company:
            companies.append(company)
        if title and company:
            pairs.append(f"{title} @ {company}")
        elif title:
            pairs.append(title)
        elif company:
            pairs.append(f"@ {company}")
    return " | ".join(titles), " | ".join(companies), " | ".join(pairs)


def profile_schools(profile: dict[str, Any]) -> str:
    schools: list[str] = []
    for edu in (profile.get("education") or [])[:4]:
        school = (edu.get("school_name") or "").strip()
        if school:
            schools.append(school)
    return " | ".join(schools)


def merge_retarget_results(review_csv: Path, output_dir: Path, ledger: dict[str, Any]) -> int:
    rows = load_csv(review_csv)
    if not rows:
        return 0
    fieldnames = list(rows[0].keys())
    for column in [
        "location_city",
        "location_country",
        "top_titles",
        "top_companies",
        "top_title_company_pairs",
        "schools",
        "linkedin_url",
        "retarget_status",
        "retarget_handle",
        "retarget_researched_at",
        "retarget_linkedin_url",
        "retarget_name_confidence",
        "retarget_notes",
    ]:
        if column not in fieldnames:
            fieldnames.append(column)
            for row in rows:
                row[column] = ""

    row_by_handle = {(row.get("handle") or "").strip(): row for row in rows}
    merged = 0
    attempts = ledger.get("attempts") or {}
    for source_handle, source_attempts in attempts.items():
        if not isinstance(source_attempts, list):
            continue
        target_row = row_by_handle.get(source_handle)
        if not target_row:
            continue
        # Prefer the newest completed result for this source handle.
        completed_attempts = [a for a in source_attempts if a.get("status") == "completed" and a.get("queue_handle")]
        completed_attempts.sort(key=lambda a: a.get("completed_at") or a.get("queued_at") or "")
        if not completed_attempts:
            continue
        attempt = completed_attempts[-1]
        queue_handle = attempt.get("queue_handle") or ""
        profile_path = output_dir / queue_handle / "01_research_parallel.json"
        if not profile_path.exists():
            continue
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        person = profile.get("person") or {}
        location = profile.get("location") or {}
        social = profile.get("social") or {}
        summary = profile.get("summary") or {}
        metadata = profile.get("metadata") or {}
        titles, companies, pairs = profile_positions(profile)
        schools = profile_schools(profile)

        target_row["full_name"] = (person.get("full_name") or target_row.get("full_name") or "").strip()
        target_row["location_city"] = (location.get("city") or "").strip()
        target_row["location_country"] = (location.get("country") or "").strip()
        target_row["top_titles"] = titles
        target_row["top_companies"] = companies
        target_row["top_title_company_pairs"] = pairs
        target_row["schools"] = schools
        linkedin = ((social.get("linkedin_url") or "").strip())
        if linkedin:
            target_row["linkedin_url"] = linkedin
        target_row["retarget_status"] = "re_researched"
        target_row["retarget_handle"] = queue_handle
        target_row["retarget_researched_at"] = attempt.get("completed_at") or now_iso()
        target_row["retarget_linkedin_url"] = linkedin
        target_row["retarget_name_confidence"] = str(person.get("confidence") or "")
        target_row["retarget_notes"] = (metadata.get("research_notes") or summary.get("text") or "").strip()
        merged += 1

    if merged:
        tmp = review_csv.with_suffix(review_csv.suffix + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        tmp.replace(review_csv)
    return merged


def cmd_mark_completed(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    output_dir = Path(args.retarget_output_dir)
    review_csv = Path(args.review_csv)
    ledger = read_json(ledger_path)
    completed = 0
    attempts = ledger.get("attempts") or {}
    for source_attempts in attempts.values():
        if not isinstance(source_attempts, list):
            continue
        for attempt in source_attempts:
            queue_handle = attempt.get("queue_handle") or ""
            if not queue_handle:
                continue
            if (output_dir / queue_handle / "01_research_parallel.json").exists() and attempt.get("status") != "completed":
                attempt["status"] = "completed"
                attempt["completed_at"] = now_iso()
                completed += 1
    merged = merge_retarget_results(review_csv, output_dir, ledger) if review_csv.exists() else 0
    write_json(ledger_path, ledger)
    emit({
        "primitive": "prepare_retarget_queue",
        "command": "mark-completed",
        "status": "ok",
        "ledger": str(ledger_path),
        "retarget_output_dir": str(output_dir),
        "review_csv": str(review_csv),
        "completed_marked": completed,
        "review_rows_merged": merged,
    })
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a targeted re-research queue from review feedback")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--review-csv", default=str(DEFAULT_REVIEW_CSV))
    prepare.add_argument("--base-queue", default=str(DEFAULT_BASE_QUEUE))
    prepare.add_argument("--output", default=str(DEFAULT_OUTPUT))
    prepare.add_argument("--manifest")
    prepare.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    prepare.add_argument("--retarget-output-dir", default=str(DEFAULT_OUTPUT_DIR))
    prepare.add_argument("--include-failed", action="store_true", help="Retry attempts previously marked failed")
    prepare.add_argument("--no-mark-queued", action="store_true", help="Write queue without recording queued attempts")
    prepare.set_defaults(func=cmd_prepare)

    mark = sub.add_parser("mark-completed")
    mark.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    mark.add_argument("--retarget-output-dir", default=str(DEFAULT_OUTPUT_DIR))
    mark.add_argument("--review-csv", default=str(DEFAULT_REVIEW_CSV),
                      help="Review CSV to update with completed retarget results")
    mark.set_defaults(func=cmd_mark_completed)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
