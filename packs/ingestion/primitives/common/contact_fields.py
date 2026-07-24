#!/usr/bin/env python3
"""Contact field parsing/normalization shared across ingestion primitives.

The ONE home for the email/phone/name/message-channel field helpers that the
directory materializer, the gmail import steps, and the message primitives each
carried an identical copy of. Two deliberately-distinct phone normalizers live
here under separate names:

- `normalize_phone` — the directory/import contract: keep digits, drop anything
  with fewer than 7, preserve a leading `+`. Used for directory identity keys.
- `canonicalize_phone` — the message contract: coerce to E.164-ish, defaulting a
  bare 10-digit US number to `+1`. Used by the iMessage/WhatsApp contact rows.

Vertical-specific variants stay in their own modules (WhatsApp's jid-aware phone
canonicalizer, merge_network_sources' plus-preserving normalizer, the
zero-default/tri-state parse_* in imports/messages/util.py).

Changelog:
  2026-07-23 (audit consolidation): created; absorbs normalize_phone (directory
    / import_steps), canonicalize_phone (the 3 identical message copies),
    parse_bool/parse_int/parse_float (merge_contacts / normalize_contacts),
    EMAIL_EXTRACT_RE (was EMAIL_RE), emails/phones_from_value/_row,
    normalize_name_key, and parse_groups / channel_counts_from_row /
    channel_last_messages_from_row / total_message_count / latest_message.
  2026-07-23 (contract consolidation): GROUP_SEPARATOR and MESSAGE_CHANNELS now
    import from packs.ingestion.schemas.message_contacts (the message-contact CSV
    contract home) instead of being redefined here.
  2026-07-23 (audit dedup): absorbs the plain normalize_email (strip + lowercase)
    that deep_context.common and imports/merge_network_sources each duplicated;
    the strict, validating normalize_email in discover/gmail/msgvault/util is a
    different contract and stays separate.
  2026-07-23 (audit): absorbs the person-vs-role classifiers is_likely_person_name
    / is_generic_or_non_person (+ GENERIC_PREFIXES / GENERIC_KEYWORDS /
    BUSINESS_NAME_KEYWORDS) from discover/gmail/msgvault_store — they are generic
    name/email testers, not msgvault-specific. Behavior unchanged.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.schemas.message_contacts import GROUP_SEPARATOR, MESSAGE_CHANNELS  # noqa: E402
from packs.ingestion.schemas.people_schema import parse_jsonish  # noqa: E402

EMAIL_EXTRACT_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def normalize_email(value: str) -> str:
    """Plain email key: strip + lowercase, no validation (blank stays blank).

    Distinct from discover/gmail/msgvault/util.normalize_email, which validates
    and raises on a malformed address."""
    return (value or "").strip().lower()


def normalize_phone(value: Any) -> str:
    """Directory-contract phone: digits only (min 7), preserving a leading `+`."""
    text = str(value or "").strip()
    if not text:
        return ""
    plus = text.startswith("+")
    digits = re.sub(r"\D+", "", text)
    if len(digits) < 7:
        return ""
    return f"+{digits}" if plus else digits


def canonicalize_phone(raw: str) -> str:
    """Message-contract phone: E.164-ish, defaulting a bare 10-digit number to `+1`."""
    value = (raw or "").strip()
    digits = re.sub(r"[^\d]", "", value)
    if len(digits) < 7:
        return ""
    if value.startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) <= 15:
        return f"+{digits}"
    return digits


def parse_bool(value: Any) -> bool:
    """True for the affirmative string tokens (1/true/yes/y), else False."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def parse_int(value: Any) -> int | None:
    """Parse a non-negative int (via float), or None for blank/invalid/negative."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = int(float(text))
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def parse_float(value: Any) -> float | None:
    """Parse a float, or None for blank/invalid input."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def emails_from_value(value: Any) -> list[str]:
    """Sorted-unique lowercase emails extracted from a scalar/list/dict/JSON blob."""
    parsed = parse_jsonish(value, None)
    found: list[str] = []
    if isinstance(parsed, list):
        for item in parsed:
            found.extend(emails_from_value(item))
    elif isinstance(parsed, dict):
        for item in parsed.values():
            found.extend(emails_from_value(item))
    else:
        found.extend(match.group(0).lower() for match in EMAIL_EXTRACT_RE.finditer(str(value or "")))
    return sorted(set(found))


def emails_from_row(row: dict[str, str]) -> list[str]:
    """Sorted-unique emails from a row's email-bearing columns."""
    emails: list[str] = []
    for key in ("primary_email", "email", "handle", "all_emails", "emails"):
        emails.extend(emails_from_value(row.get(key, "")))
    return sorted(set(emails))


def phones_from_value(value: Any) -> list[str]:
    """Sorted-unique normalized phones extracted from a scalar/list/dict/JSON blob."""
    parsed = parse_jsonish(value, None)
    found: list[str] = []
    if isinstance(parsed, list):
        for item in parsed:
            found.extend(phones_from_value(item))
    elif isinstance(parsed, dict):
        for item in parsed.values():
            found.extend(phones_from_value(item))
    else:
        phone = normalize_phone(value)
        if phone:
            found.append(phone)
    return sorted(set(found))


def phones_from_row(row: dict[str, str]) -> list[str]:
    """Sorted-unique normalized phones from a row's phone-bearing columns."""
    phones: list[str] = []
    for key in ("primary_phone", "phone", "phone_e164", "all_phones", "phones"):
        phones.extend(phones_from_value(row.get(key, "")))
    return sorted(set(phones))


def normalize_name_key(value: str) -> str:
    """Lowercased, single-spaced name key for directory name matching."""
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def parse_groups(value: str | None) -> list[str]:
    """Order-preserving unique group names from a ` | `-separated cell."""
    groups: list[str] = []
    for part in (value or "").split(GROUP_SEPARATOR):
        cleaned = re.sub(r"\s+", " ", part.strip())
        if cleaned and cleaned not in groups:
            groups.append(cleaned)
    return groups


def channel_counts_from_row(row: dict[str, str], sources: list[str], legacy_count: int | None) -> dict[str, int | None]:
    """Per-channel message counts, folding a single-source row's legacy count in."""
    counts = {channel: parse_int(row.get(f"{channel}_message_count")) for channel in MESSAGE_CHANNELS}
    # Transitional support for old per-channel CSVs: a single-source row's
    # legacy message_count belongs to that source.
    if legacy_count is not None and len(sources) == 1 and sources[0] in MESSAGE_CHANNELS and counts.get(sources[0]) is None:
        counts[sources[0]] = legacy_count
    return counts


def channel_last_messages_from_row(row: dict[str, str], sources: list[str], legacy_last: str | None) -> dict[str, str | None]:
    """Per-channel last-message timestamps, folding a single-source legacy value in."""
    values = {channel: (row.get(f"{channel}_last_message") or "").strip() or None for channel in MESSAGE_CHANNELS}
    if legacy_last and len(sources) == 1 and sources[0] in MESSAGE_CHANNELS and values.get(sources[0]) is None:
        values[sources[0]] = legacy_last
    return values


def total_message_count(record: dict[str, Any]) -> int | None:
    """Sum of per-channel counts, falling back to the legacy total when unset."""
    counts = [value for value in (record.get("channel_counts") or {}).values() if value is not None]
    if counts:
        return sum(int(value) for value in counts)
    return record.get("legacy_message_count")


def latest_message(record: dict[str, Any]) -> str | None:
    """Most recent per-channel/legacy last-message timestamp, or None."""
    values = [value for value in (record.get("channel_last_messages") or {}).values() if value]
    if record.get("legacy_last_message"):
        values.append(record["legacy_last_message"])
    return max(values, default=None)


# --- Person-vs-role classification (moved from the retired resolve_queue.py) ---

GENERIC_PREFIXES = {
    "noreply", "no-reply", "no_reply",
    "donotreply", "do-not-reply", "do_not_reply",
    "info", "contact", "support", "help", "hello",
    "sales", "marketing", "hr", "careers", "jobs",
    "admin", "administrator", "webmaster", "postmaster",
    "office", "team", "staff", "general",
    "billing", "invoices", "payments", "accounts", "accounting",
    "newsletter", "news", "updates", "notifications",
    "feedback", "enquiries", "inquiries",
    "service", "customerservice", "care", "dispatch",
    "concierge", "reservation", "reservations", "booking", "bookings",
    "rsvp", "registration", "club", "member", "members", "membership", "memberservices",
    "optical", "photos", "equity", "futures",
    "launch", "eat", "pbx", "alumni", "masters", "csi",
    "studentinfo", "fintechsupport", "casasupport",
}

GENERIC_KEYWORDS = {
    "support", "service", "noreply", "reply", "taskforce",
    "insurance", "verification", "recognition",
}

BUSINESS_NAME_KEYWORDS = {
    "llc", "inc", "corp", "ltd", "team", "services", "service",
    "spa", "optometry", "electronics", "insurance", "association",
    "department", "office", "institute", "run", "discount", "massages",
    "management", "concierge", "dispatch", "accounting", "task force",
    "hawaii", "waikiki", "aruba", "support", "delivery",
    "wines", "coffee", "mason", "security", "motors",
}


def is_likely_person_name(name: str) -> bool:
    """Return True if the name looks like a real person (first + last)."""
    if not name:
        return False
    clean = re.sub(r'\s*\([^)]*\)', '', name).strip()  # strip parentheticals like (LinkedIn Supplier)
    clean = re.sub(r'^["\']|["\']$', '', clean).strip()
    words = clean.split()
    if len(words) < 2:
        return False
    if any(kw in clean.lower() for kw in BUSINESS_NAME_KEYWORDS):
        return False
    if '&' in clean:
        return False
    # All-caps or all-lower single tokens that aren't name-like
    if clean == clean.upper() and len(words) <= 2:
        return False
    return True


def is_generic_or_non_person(email: str) -> bool:
    """Return True if the email looks like a role/service address, not a person."""
    if not email or "@" not in email:
        return True
    local = email.split("@")[0].lower().strip()
    # Strip plus-addressing
    local = local.split("+")[0]
    # Exact prefix match
    if local in GENERIC_PREFIXES:
        return True
    # First segment match (e.g. customer.service@, no-reply@, info-mhi@)
    base = re.split(r'[.\-_]', local)[0]
    if base in GENERIC_PREFIXES:
        return True
    # Contains generic keyword anywhere
    for kw in GENERIC_KEYWORDS:
        if kw in local:
            return True
    # Phone-number-like local parts
    if re.match(r'^\d{7,}$', local):
        return True
    # Single character local parts
    if len(local) <= 1:
        return True
    # Local part is just digits (e.g. 2relaxinparadise is fine but pure digits aren't)
    if re.match(r'^\d+$', local):
        return True
    return False
