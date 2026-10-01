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
canonicalizer, the zero-default/tri-state parse_* in imports/messages/util.py).

Changelog:
  2026-07-23 (audit consolidation): created; absorbs normalize_phone (directory
    / import_steps), canonicalize_phone (the 3 identical message copies),
    parse_bool/parse_int/parse_float (merge_contacts),
    EMAIL_EXTRACT_RE (was EMAIL_RE), emails/phones_from_value/_row,
    normalize_name_key, and parse_groups / channel_counts_from_row /
    channel_last_messages_from_row / total_message_count / latest_message.
  2026-07-23 (contract consolidation): GROUP_SEPARATOR and MESSAGE_CHANNELS now
    import from packs.ingestion.schemas.message_contacts (the message-contact CSV
    contract home) instead of being redefined here.
  2026-07-23 (audit dedup): absorbs the plain normalize_email (strip + lowercase)
    that deep_context.shared.common and imports/merge_network_sources each duplicated;
    the strict, validating normalize_email in discover/gmail/msgvault/util is a
    different contract and stays separate.
  2026-07-23 (audit): absorbs the person-vs-role classifiers is_likely_person_name
    / is_generic_or_non_person (+ GENERIC_PREFIXES / GENERIC_KEYWORDS /
    BUSINESS_NAME_KEYWORDS) from discover/gmail/msgvault_store — they are generic
    name/email testers, not msgvault-specific. Behavior unchanged.
  2026-10-01: adds `is_role_address` / `ROLE_ADDRESS_WORDS`, a whole-local-part
    shared-mailbox test (`ir@` yes, `irene@` no) that deep-context applies where
    imported contacts enter it. Deletes the uncalled is_likely_person_name /
    is_generic_or_non_person and their word lists, which matched pieces of an
    address.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
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


def identifier_emails(identifiers: Iterable[str]) -> set[str]:
    """Extract merge-candidate email keys from owned identifier evidence.

    PINNED: this deliberately accepts the merge judge's looser dotted-domain
    evidence shape instead of applying the stricter import email validator.
    Tightening it would change merge blocking and paid-judge queue membership.
    """
    values = (str(identifier).strip() for identifier in identifiers)
    return {
        value.lower()
        for value in values
        if "@" in value and "." in value.rsplit("@", 1)[-1]
    }


def identifier_phones(identifiers: Iterable[str]) -> set[str]:
    """Extract merge-candidate phone keys from owned identifier evidence.

    PINNED: merge evidence accepts 7-15 digits, rejects URL/domain lookalikes,
    and drops a leading US country code. Import normalization is intentionally
    stricter; changing this variant alters merge blocking and paid-judge work.
    """
    phones: set[str] = set()
    for raw in identifiers:
        value = str(raw).strip()
        if not value or "@" in value or re.search(r"[a-z]{2,}\.[a-z]{2,}", value.lower()):
            continue
        digits = re.sub(r"[^\d]", "", value)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if 7 <= len(digits) <= 15:
            phones.add(digits)
    return phones


def format_phone_digits(digits: str) -> str:
    """Render a normalized digit key for human-readable evidence."""
    if len(digits) == 10:
        return f"+1 ({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return f"+{digits}"


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


# --- Shared mailboxes ---

ROLE_ADDRESS_WORDS = frozenset({
    "accounting", "accounts", "accountspayable", "accountsreceivable", "admin",
    "ap", "ar", "billing", "care", "careers", "community", "compliance",
    "concierge", "contact", "customerservice", "customersupport", "events",
    "feedback", "filings", "finance", "frontdesk", "help", "helpdesk", "hr",
    "info", "investorrelations", "investors", "invoice", "invoices", "ir",
    "legal", "mail", "members", "membership", "office", "onboarding", "operations",
    "ops", "orders", "partnerships", "payments", "portfolio", "reception",
    "registration", "rsvp", "sales", "scheduling", "service", "services",
    "support", "tax", "taxes",
})
"""Local parts of shared mailboxes. Deliberately absent: hello, hi, team,
assistant — real people and solo founders write from those."""

_SHORT_ROLE_LENGTH = 3
"""A separator-joined local part this short is never a role word (`i.r`)."""


def is_role_address(email: str) -> bool:
    """True when the whole local part is a role word (`ir@`, `customer.service@`).

    Never matches a piece of the local part: `irene@` and `kirk.ir@` are people.
    Separators are dropped only when the joined result is longer than 3
    characters, so short words must equal the raw local part.
    """
    local = email.strip().lower().rsplit("@", 1)[0]
    if local in ROLE_ADDRESS_WORDS:
        return True
    joined = re.sub(r"[.\-_]", "", local)
    return len(joined) > _SHORT_ROLE_LENGTH and joined in ROLE_ADDRESS_WORDS
