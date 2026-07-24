"""Pure msgvault/email helpers: no SQLite connection, no I/O.

The connection-free half of the old `gmail/msgvault_store.py`: address parsing,
name/display normalization, domain classification, label normalization, the
canonical-message identity, and the round-trip test. `MsgvaultStore`
(``msgvault/store.py``) imports what it needs from here; every function takes
plain values, never a connection, so they are safe to reuse and unit-test in
isolation.

Two `normalize_email` contracts exist in the repo: THIS one validates and raises
on a malformed address (used for the discovery aggregation's participant rows);
`common/contact_fields.py:normalize_email` is the plain strip+lowercase key.

Changelog:
  2026-07-23 (audit): split out of `gmail/msgvault_store.py` — the pure
    module-level helpers + their constants moved here so `store.py` is the
    `MsgvaultStore` class (+ its SQL) alone. The person-vs-role classifiers
    (`is_likely_person_name` / `is_generic_or_non_person`) moved further out to
    `common/contact_fields.py` (generic name/email testers, not msgvault-only).
    DEFAULT_MSGVAULT_DB stays here (honors $MSGVAULT_HOME) as the msgvault
    package's own default; behavior/values unchanged.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

DEFAULT_MSGVAULT_DB = Path(os.environ.get("MSGVAULT_HOME", str(Path.home() / ".msgvault"))) / "msgvault.db"
DEFAULT_EXCLUDED_MSGVAULT_LABELS = ("CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_FORUMS", "CATEGORY_UPDATES")

PERSONAL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "icloud.com",
    "aol.com",
    "msn.com",
    "live.com",
    "me.com",
    "mac.com",
    "protonmail.com",
    "mail.com",
    "ymail.com",
    "googlemail.com",
    "comcast.net",
    "att.net",
    "verizon.net",
    "sbcglobal.net",
    "cox.net",
    "earthlink.net",
    "126.com",
    "163.com",
    "qq.com",
}
NON_WORK_DOMAINS = PERSONAL_DOMAINS | {"noreply.github.com", "users.noreply.github.com"}
AUTOMATED_EMAIL_KEYWORDS = {
    "unsub",
    "unsubscribe",
    "bounce",
    "spam",
    "spamproc",
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "postmaster",
    "leave-",
    "void-",
    "reply.",
    "notification",
    "notifications",
    "alert",
    "alerts",
    "reservation",
    "reservations",
    "booking",
    "bookings",
}
SUPPORT_TICKET_DOMAINS = {"zendesk", "freshdesk", "intercom", "helpscout", "helpdesk"}
TRAVEL_SERVICE_DOMAINS = {
    "airbnb",
    "vrbo",
    "booking.com",
    "hotels.com",
    "expedia",
    "uber.com",
    "lyft.com",
    "united",
    "delta",
    "aa.com",
    "americanairlines",
    "southwest",
    "jetblue",
    "alaska",
    "marriott",
    "hilton",
    "hyatt",
    "hertz",
    "avis",
    "enterprise",
}


def normalize_email(email: str) -> str:
    """Lowercase and validate an email address; raise ValueError when invalid."""
    value = (email or "").strip().lower()
    if not value or "@" not in value:
        raise ValueError("--email must contain a valid email address")
    local, domain = value.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        raise ValueError("--email must contain a valid email address")
    return value


def normalize_name(name: str, email: str = "") -> str:
    """Collapse whitespace in a display name, falling back to a name derived
    from the email local part when the name is blank."""
    value = " ".join((name or "").strip().split())
    if value:
        return value
    local = email.split("@", 1)[0] if "@" in email else ""
    local = re.sub(r"[._+-]+", " ", local)
    return " ".join(part.capitalize() for part in local.split() if part)


def classify_email(email: str) -> str:
    """Classify an address as personal, work, other, or unknown by its domain."""
    if not email or "@" not in email:
        return "unknown"
    domain = email.rsplit("@", 1)[1].lower()
    if domain in PERSONAL_DOMAINS:
        return "personal"
    if domain in NON_WORK_DOMAINS:
        return "other"
    return "work"


def domain_guess(email: str) -> dict[str, str]:
    """Guess a company name from the email domain (local heuristic only)."""
    domain = email.rsplit("@", 1)[1].lower() if "@" in email else ""
    root = domain.split(".")[0] if domain else ""
    company_guess = " ".join(part.capitalize() for part in re.split(r"[-_]", root) if part)
    return {"domain": domain, "company_guess": company_guess, "method": "local_domain_heuristic"}


def parse_email_header(header_value: str) -> list[tuple[str, str]]:
    """Parse a Gmail From/To/Cc header into (display_name, email) pairs.

    Ported from the legacy Gmail ingestion path, but kept stdlib-only and local.
    """
    if not header_value:
        return []

    parts: list[str] = []
    current = ""
    in_quotes = False
    in_angle = False
    for char in header_value:
        if char == '"':
            in_quotes = not in_quotes
        elif char == "<":
            in_angle = True
        elif char == ">":
            in_angle = False
        elif char == "," and not in_quotes and not in_angle:
            if current.strip():
                parts.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current.strip())

    results: list[tuple[str, str]] = []
    angle_pattern = r"^(.*?)\s*<([^>]+)>$"
    email_pattern = r"^[a-zA-Z0-9._%+-]{2,}@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    for part in parts:
        value = part.strip()
        if not value:
            continue
        angle_match = re.match(angle_pattern, value)
        if angle_match:
            name = normalize_name(angle_match.group(1).strip().strip('"'))
            email = angle_match.group(2).strip().lower()
            if re.match(email_pattern, email):
                results.append((name, email))
        else:
            email = value.lower()
            if re.match(email_pattern, email):
                results.append(("", email))
    return results


def is_automated_email(email: str) -> tuple[bool, str]:
    """Detect automated/service addresses; return (is_automated, reason)."""
    if not email or "@" not in email:
        return True, "invalid email"
    local_part, domain = email.lower().rsplit("@", 1)
    for keyword in AUTOMATED_EMAIL_KEYWORDS:
        if keyword in local_part or keyword in domain:
            return True, f"contains '{keyword}'"
    for system in SUPPORT_TICKET_DOMAINS:
        if system in domain:
            return True, f"support ticket system ({system})"
    for service in TRAVEL_SERVICE_DOMAINS:
        if service in domain:
            return True, f"travel/hospitality service ({service})"
    if re.search(r"[a-f0-9]{16,}", local_part):
        return True, "hash-like pattern in email"
    if len(local_part) >= 20:
        vowel_count = sum(1 for c in local_part if c in "aeiou")
        vowel_ratio = vowel_count / len(local_part) if local_part else 0
        if vowel_ratio < 0.15 and re.match(r"^[a-z0-9_-]+$", local_part):
            return True, "random alphanumeric pattern"
    if len(local_part) > 40 and re.match(r"^[a-z0-9_-]+$", local_part):
        return True, "very long alphanumeric local part"
    return False, ""


def split_name(full_name: str) -> tuple[str, str]:
    """Split a full name into (first, last), using the outermost tokens."""
    parts = [part for part in re.split(r"\s+", (full_name or "").strip()) if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def default_name_for_email(email: str) -> str:
    """Derive a capitalized display name from an email's local part."""
    local = email.split("@", 1)[0] if "@" in email else email
    local = re.sub(r"[._+-]+", " ", local)
    return " ".join(part.capitalize() for part in local.split() if part)


def best_display_name(email: str, names: dict[str, int]) -> str:
    """Pick the most frequent non-empty display name observed for an email,
    falling back to a name derived from the address."""
    cleaned: dict[str, int] = {}
    email_l = email.lower()
    for name, count in names.items():
        value = normalize_name(name, email)
        if not value or value.lower() == email_l:
            continue
        cleaned[value] = cleaned.get(value, 0) + count
    if cleaned:
        return sorted(cleaned.items(), key=lambda item: (-item[1], item[0].casefold()))[0][0]
    return default_name_for_email(email)


def normalize_label_names(values: Iterable[str] | None) -> list[str]:
    """Uppercase, trim, and dedupe label names, preserving first-seen order."""
    out: list[str] = []
    for value in values or []:
        label = str(value or "").strip()
        if label and label.upper() not in out:
            out.append(label.upper())
    return out


def default_excluded_labels(include_category_mail: bool, extra_labels: Iterable[str] | None = None) -> list[str]:
    """Return the label-exclusion list: Gmail category labels (unless included)
    plus any extra labels, normalized."""
    labels: list[str] = []
    if not include_category_mail:
        labels.extend(DEFAULT_EXCLUDED_MSGVAULT_LABELS)
    labels.extend(extra_labels or [])
    return normalize_label_names(labels)


def msgvault_db_uri(path: Path) -> str:
    """Return the read-only SQLite URI for a msgvault database path."""
    return f"file:{path.expanduser().resolve()}?mode=ro"


def canonical_message_id(row: Any) -> str:
    """Stable identity for one real email message.

    msgvault can store the same RFC822 message under multiple conversation_ids
    (and across accounts), each with its own msgvault row id. Counting by row
    id double-counts those copies. Prefer rfc822_message_id, then
    source_message_id, then fall back to the msgvault row id.
    """
    for key in ("rfc822_message_id", "source_message_id"):
        try:
            value = str(row[key] or "").strip()
        except (KeyError, IndexError):
            value = ""
        if value:
            return f"{key}:{value}"
    return f"row:{row['message_id']}"


def has_round_trip_interaction(row: dict[str, Any]) -> bool:
    """True when a contact has BOTH sent and received messages (round trip)."""
    return int(row.get("total_sent") or 0) > 0 and int(row.get("total_received") or 0) > 0
