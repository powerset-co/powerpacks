"""Map parsed Messages source metadata into canonical people rows.

`contact_floor_reason` is the import floor: the deterministic rules that decide
which source contacts become candidate people at all. Everything after the
floor is identity-neutral mapping.

Changelog:
  2026-09-25: restored the floor deleted by #486 (usable name, at least one
    message, group-only contacts need GROUP_ONLY_MIN_MESSAGES). Email-keyed
    contacts stay eligible; the phone-shape check applies to phone keys only.
    No matcher, no skip flag, no CLI overrides.
"""

from __future__ import annotations

import re
from pathlib import Path

from packs.ingestion.schemas.candidates_schema import candidate_key_for
from packs.ingestion.schemas.message_contacts import MessageContact
from packs.ingestion.schemas.people_schema import latest_interaction, normalize_people_row


MIN_MESSAGE_COUNT = 1
# Group-appearance-only contacts below this volume are low-signal noise
# (someone from a group thread, not a relationship). A positive WhatsApp
# direct-chat count is explicit relationship evidence and bypasses it.
GROUP_ONLY_MIN_MESSAGES = 10

MIN_NAME_TOKENS = 2
MIN_TOKEN_LEN = 2
MIN_TOTAL_ALPHA = 5
BLOCKED_LAST_NAME_TOKENS = frozenset({"hinge", "raya", "tinder", "bumble"})
MIN_PHONE_DIGITS = 10
MAX_PHONE_DIGITS = 15
SHORT_CODE_OR_INVALID_PHONE = "short_code_or_invalid_phone"

_NAME_CLEAN_RE = re.compile(r"[^A-Za-zÀ-ÿ'’\-\s]")
_MULTISPACE_RE = re.compile(r"\s+")


def _clean_name(name: str) -> str:
    """Strip non-name characters and collapse whitespace."""
    cleaned = _NAME_CLEAN_RE.sub(" ", name)
    return _MULTISPACE_RE.sub(" ", cleaned).strip()


def _last_name_tokens(cleaned: str) -> set[str]:
    """Lowercased tokens after the first name (single-token names -> empty set)."""
    parts = cleaned.lower().split()
    if len(parts) < 2:
        return set()
    return set(parts[1:])


def _has_searchable_name(cleaned: str) -> bool:
    """True when the saved name has enough real tokens/letters to research."""
    tokens = [token for token in cleaned.split(" ") if len(token) >= MIN_TOKEN_LEN]
    if len(tokens) < MIN_NAME_TOKENS:
        return False
    return sum(1 for ch in cleaned if ch.isalpha()) >= MIN_TOTAL_ALPHA


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _bad_name_reason(name: str, phone: str) -> str:
    """First reason the saved name is unusable ("" = usable): the name is just
    the phone number, empty, carries a blocked app token (dating-app saves),
    or is too thin to research."""
    phone_digits, name_digits = _digits(phone), _digits(name)
    if phone_digits and name_digits and phone_digits.endswith(name_digits):
        return "name_is_phone"
    cleaned = _clean_name(name)
    if not cleaned:
        return "no_name"
    if _last_name_tokens(cleaned) & BLOCKED_LAST_NAME_TOKENS:
        return "blocked_name_token"
    if not _has_searchable_name(cleaned):
        return "bad_name"
    return ""


def contact_floor_reason(row: MessageContact) -> str:
    """First failing floor reason for a source contact ("" = passes).

    The floor owns identifier validity: a contact that passes always has a
    candidate key, so `contact_to_person` never has to refuse one."""
    is_email = "@" in row.phone
    if not candidate_key_for(row.phone if is_email else "", "" if is_email else row.phone):
        return SHORT_CODE_OR_INVALID_PHONE
    if not is_email and not MIN_PHONE_DIGITS <= len(_digits(row.phone)) <= MAX_PHONE_DIGITS:
        return SHORT_CODE_OR_INVALID_PHONE
    name_reason = _bad_name_reason(row.name, "" if is_email else row.phone)
    if name_reason:
        return name_reason
    if row.message_count < MIN_MESSAGE_COUNT:
        return "below_min_messages"
    if row.is_in_group_chats and row.whatsapp_message_count == 0 and row.message_count < GROUP_ONLY_MIN_MESSAGES:
        return "group_only_low_signal"
    return ""


def messages_source_channels(row: MessageContact) -> list[str]:
    """Channels the contact was seen on ('imessage'/'whatsapp'), from the
    source column plus any positive per-channel count; ['messages'] fallback."""
    channels: list[str] = []
    raw = row.source
    for token in re.split(r"[,|+/;\s]+", raw):
        if token in {"imessage", "whatsapp"} and token not in channels:
            channels.append(token)
    for count, channel in (
        (row.imessage_message_count, "imessage"),
        (row.whatsapp_message_count, "whatsapp"),
    ):
        if count > 0 and channel not in channels:
            channels.append(channel)
    return channels or ["messages"]


def contact_interaction_counts(row: MessageContact) -> dict[str, int]:
    """Positive per-channel DM counts, keyed by channel."""
    counts: dict[str, int] = {}
    for count, channel in (
        (row.imessage_message_count, "imessage"),
        (row.whatsapp_message_count, "whatsapp"),
    ):
        if count > 0:
            counts[channel] = count
    return counts


def contact_last_interaction(row: MessageContact) -> str:
    """Most recent activity across the per-channel and legacy last-message columns."""
    return latest_interaction(
        row.imessage_last_message,
        row.whatsapp_last_message,
        row.last_message,
    )


def contact_to_person(row: MessageContact, contacts_csv: Path) -> dict[str, str]:
    """Keep the source identity and metadata of a contact that cleared the floor."""
    email = row.phone if "@" in row.phone else ""
    phone = "" if email else row.phone
    key = candidate_key_for(email, phone)

    name_parts = row.name.split(None, 1)
    counts = contact_interaction_counts(row)
    return normalize_people_row({
        "id": f"candidate:{key}",
        "full_name": row.name,
        "first_name": name_parts[0] if name_parts else "",
        "last_name": name_parts[1] if len(name_parts) > 1 else "",
        "primary_email": email,
        "all_emails": [email] if email else "",
        "primary_phone": phone,
        "all_phones": [phone] if phone else "",
        "source_channels": ",".join(messages_source_channels(row)),
        "source_artifacts": str(contacts_csv),
        "interaction_counts": counts or "",
        "last_interaction": contact_last_interaction(row),
    })
