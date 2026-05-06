#!/usr/bin/env python3
"""Convert the unified contacts.csv into a deep-research input CSV.

Stdlib-only. Filters the message-contacts CSV to the rows that are worth
deep-researching (named, LLM-ENRICH, not already matched in Powerset) and
reshapes them into the column set consumed by aleph-mvp's
`data_pipeline_v2.pipelines.synthetic.research_parallel`.

This primitive does not call the deep-research pipeline itself. It only
produces the CSV that pipeline reads.

Filter (default):
  - name is non-empty
  - name has at least 2 tokens, each at least 2 characters (i.e. plausibly
    searchable on LinkedIn) — override with `--allow-single-token`
  - last-name tokens do not contain dating-app labels from the legacy
    phone_prune_config (`hinge`, `raya`, `tinder`, `bumble`)
  - skip != "yes" (i.e. LLM said ENRICH, or no LLM review yet)
  - matched_person_id is empty (no Powerset linkage)

Priority tiers (`priority_reason` column):
  - P1: cross-channel (imessage,whatsapp) AND (>=100 msgs OR last <=365d)
  - P2a: cross-channel (any volume/recency)
  - P2b: single channel, >=100 msgs lifetime
  - P3:  single channel, recent (<=365d) and >=10 msgs
  - P4:  everything else
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Column order matches what research_parallel.py / prepare_phone_contacts.py expect.
RESEARCH_COLUMNS = [
    "handle",
    "display_name",
    "first_name",
    "last_name",
    "primary_email",
    "domain",
    "total_messages",
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
    "is_in_group_chats",
    "match_status",
    "group_names",
    "match_confidence",
    "match_method",
    "match_reason",
    "priority_reason",
]

PRIORITY_RANK = {"P1": 0, "P2a": 1, "P2b": 2, "P3": 3, "P4": 4}

# Single-token names (no last name) and short tokens are essentially
# un-LinkedIn-searchable from a phone+area-code prior alone, so we filter
# them out before paying for deep research. The thresholds match
# aleph-mvp's `looks_like_real_name`:
#   - >= 2 alpha-only tokens of >= 2 chars each
#   - >= 5 total alpha characters across the name
MIN_NAME_TOKENS = 2
MIN_TOKEN_LEN = 2
MIN_TOTAL_ALPHA = 5
MIN_MESSAGE_COUNT = 3
NAME_CLEAN_RE = re.compile(r"[^A-Za-zÀ-ÿ'’\-\s]")
MULTISPACE_RE = re.compile(r"\s+")
BLOCKED_LAST_NAME_TOKENS = {"hinge", "raya", "tinder", "bumble"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    cleaned = NAME_CLEAN_RE.sub(" ", name or "")
    return MULTISPACE_RE.sub(" ", cleaned).strip()


def split_name(full: str) -> tuple[str, str]:
    cleaned = normalize_name(full)
    if not cleaned:
        return "", ""
    parts = cleaned.split(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def normalize_last_name_tokens(name: str) -> set[str]:
    cleaned = normalize_name(name).lower()
    parts = cleaned.split()
    if len(parts) < 2:
        return set()
    return {token for token in parts[1:] if token}


def normalize_phoneish(value: str) -> str:
    return "".join(ch for ch in value or "" if ch.isdigit())


def phone_last4(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-4:] if digits else ""


def area_code(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:4]
    if len(digits) == 10:
        return digits[0:3]
    if len(digits) >= 11:
        # Assume country code 1-3 digits, take next 3 as area code best-effort.
        # Keep it conservative: only emit area code for US-shaped numbers.
        return ""
    return ""


def parse_int(value: str) -> int:
    text = (value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def parse_bool(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def days_since(iso: str) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now() - dt).days


def stable_handle(phone: str, name: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if digits:
        return f"phone-{digits[-10:]}"
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return slug or "unknown"


def priority_tier(row: dict[str, str], today: datetime) -> str:
    cross = row.get("source", "") == "imessage,whatsapp"
    msgs = parse_int(row.get("message_count"))
    days = days_since(row.get("last_message", ""))
    recent = days is not None and days <= 365

    if cross and (msgs >= 100 or recent):
        return "P1"
    if cross:
        return "P2a"
    if msgs >= 100:
        return "P2b"
    if recent and msgs >= 10:
        return "P3"
    return "P4"


# ---------------------------------------------------------------------------
# Filtering + transform
# ---------------------------------------------------------------------------

def is_eligible(row: dict[str, str], include_skipped: bool, include_matched: bool) -> bool:
    if not (row.get("name") or "").strip():
        return False
    if not include_skipped and parse_bool(row.get("skip", "")):
        return False
    if not include_matched and (row.get("matched_person_id") or "").strip():
        return False
    return True


def has_searchable_name(name: str) -> bool:
    """True when a name looks plausibly LinkedIn-searchable.

    Mirrors aleph-mvp's `looks_like_real_name` so the same filter applies
    whether the deep-research stage runs natively here or via aleph-mvp.
    """
    cleaned = normalize_name(name)
    if not cleaned:
        return False
    tokens = [t for t in cleaned.split(" ") if len(t) >= MIN_TOKEN_LEN]
    if len(tokens) < MIN_NAME_TOKENS:
        return False
    alpha = sum(1 for ch in cleaned if ch.isalpha())
    return alpha >= MIN_TOTAL_ALPHA


def bad_name_reason(name: str, phone: str = "") -> str:
    phone_digits = normalize_phoneish(phone)
    raw_name_digits = normalize_phoneish(name)
    if phone_digits and raw_name_digits and phone_digits.endswith(raw_name_digits):
        return "name_is_phone"
    cleaned = normalize_name(name)
    if not cleaned:
        return "no_name"
    if normalize_last_name_tokens(cleaned) & BLOCKED_LAST_NAME_TOKENS:
        return "blocked_name_token"
    if not has_searchable_name(cleaned):
        return "bad_name"
    return ""


def transform_row(row: dict[str, str], today: datetime) -> dict[str, str]:
    name = normalize_name(row.get("name") or "")
    first, last = split_name(name)
    phone = (row.get("phone") or "").strip()
    tier = priority_tier(row, today)
    return {
        "handle": stable_handle(phone, name),
        "display_name": name,
        "first_name": first,
        "last_name": last,
        "primary_email": "",
        "domain": "",
        "total_messages": str(parse_int(row.get("message_count"))) if row.get("message_count") else "",
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
        "area_code": area_code(phone),
        "message_source": row.get("source", "") or "",
        "last_message": row.get("last_message", "") or "",
        "is_in_group_chats": row.get("is_in_group_chats", "") or "false",
        "match_status": row.get("match_status", "") or "",
        "group_names": row.get("group_names", "") or "",
        "match_confidence": row.get("match_confidence", "") or "",
        "match_method": row.get("match_method", "") or "",
        "match_reason": row.get("match_reason", "") or "",
        "priority_reason": tier,
    }


# ---------------------------------------------------------------------------
# Subcommand
# ---------------------------------------------------------------------------

def cmd_prepare(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    output_path = Path(args.output)
    manifest_path = Path(args.manifest) if args.manifest else output_path.with_suffix(output_path.suffix + ".manifest.json")

    if not input_path.exists():
        emit({
            "primitive": "prepare_research_queue",
            "command": "prepare",
            "status": "failed",
            "error": f"input CSV not found: {input_path}",
        })
        return 1

    today = now()
    eligible: list[dict[str, str]] = []
    counts = {
        "input_rows": 0,
        "eligible_rows": 0,
        "filtered_no_name": 0,
        "filtered_unsearchable_name": 0,
        "filtered_blocked_name_token": 0,
        "filtered_name_is_phone": 0,
        "filtered_skipped": 0,
        "filtered_already_matched": 0,
        "filtered_suggested": 0,
        "filtered_unsupported_status": 0,
        "filtered_low_messages": 0,
        "filtered_low_signal_group_chat": 0,
    }
    by_tier: dict[str, int] = {tier: 0 for tier in PRIORITY_RANK}

    with input_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            counts["input_rows"] += 1
            raw_name = row.get("name") or ""
            name = normalize_name(raw_name)
            reason = bad_name_reason(raw_name, row.get("phone", ""))
            if reason == "no_name":
                counts["filtered_no_name"] += 1
                continue
            if reason == "blocked_name_token":
                counts["filtered_blocked_name_token"] += 1
                continue
            if reason == "name_is_phone":
                counts["filtered_name_is_phone"] += 1
                continue
            if not args.allow_single_token and reason == "bad_name":
                counts["filtered_unsearchable_name"] += 1
                continue
            skip_flag = parse_bool(row.get("skip", ""))
            status = (row.get("match_status") or "").strip().lower()
            matched_flag = (
                bool((row.get("matched_person_id") or "").strip())
                or bool((row.get("matched_linkedin_url") or "").strip())
                or status == "matched"
            )
            if not args.include_skipped and skip_flag:
                counts["filtered_skipped"] += 1
                continue
            if not args.include_matched and matched_flag:
                counts["filtered_already_matched"] += 1
                continue
            if status == "suggested" and not args.include_suggested:
                counts["filtered_suggested"] += 1
                continue
            if status not in {"", "unmatched", "suggested", "matched"}:
                counts["filtered_unsupported_status"] += 1
                continue
            message_count = parse_int(row.get("message_count", ""))
            if message_count < args.min_message_count:
                counts["filtered_low_messages"] += 1
                continue
            if args.exclude_group_only_unknowns and parse_bool(row.get("is_in_group_chats", "")) and message_count < 10:
                counts["filtered_low_signal_group_chat"] += 1
                continue
            transformed = transform_row(row, today)
            tier = transformed["priority_reason"]
            by_tier[tier] = by_tier.get(tier, 0) + 1
            if args.tiers and tier not in args.tiers:
                continue
            eligible.append(transformed)
            counts["eligible_rows"] += 1

    # Sort by priority tier then descending message volume so the pipeline runs
    # the highest-signal contacts first and `--limit` slices off P1/P2 work first.
    def sort_key(r: dict[str, str]) -> tuple[int, int]:
        msgs = -parse_int(r.get("total_messages"))
        rank = PRIORITY_RANK.get(r["priority_reason"], 99)
        return (rank, msgs)

    eligible.sort(key=sort_key)

    if args.limit is not None:
        eligible = eligible[: args.limit]
        counts["eligible_rows"] = len(eligible)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESEARCH_COLUMNS)
        writer.writeheader()
        writer.writerows(eligible)

    estimate = {
        "core2x_usd": round(len(eligible) * 0.05, 2),
        "pro_usd": round(len(eligible) * 0.10, 2),
        "ultra8x_usd": round(len(eligible) * 2.40, 2),
    }
    manifest = {
        "primitive": "prepare_research_queue",
        "command": "prepare",
        "status": "ok",
        "input": str(input_path),
        "output": str(output_path),
        "manifest_path": str(manifest_path),
        "tiers_filter": args.tiers or [],
        "include_matched": bool(args.include_matched),
        "include_skipped": bool(args.include_skipped),
        "include_suggested": bool(args.include_suggested),
        "min_message_count": int(args.min_message_count),
        "exclude_group_only_unknowns": bool(args.exclude_group_only_unknowns),
        "counts": counts,
        "by_tier_total": by_tier,
        "rows_written": len(eligible),
        "parallel_cost_estimate_usd": estimate,
    }
    write_json(manifest_path, manifest)
    emit(manifest)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the deep-research input CSV from a unified contacts.csv"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="Filter + reshape contacts.csv into research-pipeline input")
    prepare.add_argument("--input", "-i", required=True, help="Path to the unified contacts.csv")
    prepare.add_argument("--output", "-o", required=True, help="Path to write the research-input CSV")
    prepare.add_argument("--manifest", help="Path to write the run manifest JSON")
    prepare.add_argument("--tiers", nargs="+", choices=list(PRIORITY_RANK),
                        help="Only include rows in these priority tiers")
    prepare.add_argument("--limit", type=int, help="Cap the number of rows (after sort)")
    prepare.add_argument("--include-matched", action="store_true",
                        help="Also include rows already matched to a Powerset person")
    prepare.add_argument("--include-skipped", action="store_true",
                        help="Also include rows the LLM review marked skip=yes")
    prepare.add_argument("--include-suggested", action=argparse.BooleanOptionalAction, default=True,
                        help="Include suggested matches in the research queue. Defaults to true.")
    prepare.add_argument("--min-message-count", type=int, default=MIN_MESSAGE_COUNT,
                        help="Minimum message count for unresolved contacts. Defaults to 3, matching the aleph-mvp prune rules.")
    prepare.add_argument("--exclude-group-only-unknowns", action="store_true",
                        help="Drop group-chat-only low-signal contacts with fewer than 10 messages.")
    prepare.add_argument("--allow-single-token", action="store_true",
                        help="Skip the searchable-name filter (allow single-token / very short names). "
                             "Off by default because such names rarely yield a deep-research hit and just burn API spend.")
    prepare.set_defaults(func=cmd_prepare)
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
