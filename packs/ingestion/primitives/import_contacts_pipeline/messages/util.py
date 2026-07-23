"""Messages-vertical utilities: tolerant field parsers + the deterministic
"worth researching" candidate floor and message-contact field readers.

CSV cells arrive as arbitrary user/state text; these never raise — they map
unparseable input to a neutral value (None / 0 / "") so row processing stays
total."""

from __future__ import annotations

from typing import Any

TRUTHY = {"1", "true", "yes", "y", "on"}
FALSY = {"0", "false", "no", "n", "off"}


def normalize_bool(value: Any) -> bool | None:
    """Tri-state bool: True/False for recognized tokens, None for anything else."""
    raw = str(value or "").strip().lower()
    if raw in TRUTHY:
        return True
    if raw in FALSY:
        return False
    return None


def parse_int_field(value: Any) -> int:
    """Int from a CSV cell ('42', '42.0', '' -> 42, 42, 0); never raises."""
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def split_full_name(full_name: str) -> tuple[str, str]:
    """(first, rest) on the first whitespace; ('', '') for an empty name."""
    parts = (full_name or "").strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


def normalize_phoneish(value: str) -> str:
    """Digits only — the comparable core of a phone-shaped string."""
    return "".join(ch for ch in value or "" if ch.isdigit())


import re  # noqa: E402

from packs.ingestion.schemas.people_schema import latest_interaction  # noqa: E402


DEFAULT_MIN_MESSAGE_COUNT = 1
# Group-appearance-only contacts below this volume are low-signal noise
# (someone from a group thread, not a relationship) unless opted in. A positive
# WhatsApp direct-chat count is explicit relationship evidence and bypasses it.
GROUP_ONLY_MIN_MESSAGES = 10

MIN_NAME_TOKENS = 2
MIN_TOKEN_LEN = 2
MIN_TOTAL_ALPHA = 5
BLOCKED_LAST_NAME_TOKENS = {"hinge", "raya", "tinder", "bumble"}
NAME_CLEAN_RE = re.compile(r"[^A-Za-zÀ-ÿ'’\-\s]")
MULTISPACE_RE = re.compile(r"\s+")
MIN_PHONE_DIGITS = 10
MAX_PHONE_DIGITS = 15


def normalize_name(name: str) -> str:
    """Strip non-name characters and collapse whitespace."""
    cleaned = NAME_CLEAN_RE.sub(" ", name or "")
    return MULTISPACE_RE.sub(" ", cleaned).strip()


def normalize_last_name_tokens(name: str) -> set[str]:
    """Lowercased tokens after the first name ('' names -> empty set)."""
    cleaned = normalize_name(name).lower()
    parts = cleaned.split()
    if len(parts) < 2:
        return set()
    return {token for token in parts[1:] if token}


def has_searchable_name(name: str) -> bool:
    """True when the saved name has enough real tokens/letters to research."""
    cleaned = normalize_name(name)
    if not cleaned:
        return False
    tokens = [t for t in cleaned.split(" ") if len(t) >= MIN_TOKEN_LEN]
    if len(tokens) < MIN_NAME_TOKENS:
        return False
    alpha = sum(1 for ch in cleaned if ch.isalpha())
    return alpha >= MIN_TOTAL_ALPHA


def bad_name_reason(name: str, phone: str = "") -> str:
    """First reason the saved name is unusable ("" = usable): the name is just
    the phone number, empty, carries a blocked app token (dating-app saves),
    or is too thin to research."""
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


def has_whatsapp_direct_messages(row: dict[str, str]) -> bool:
    """Whether discovery found a real WhatsApp direct-message thread."""
    return parse_int_field(row.get("whatsapp_message_count")) > 0


def contact_floor_reason(
    row: dict[str, str],
    *,
    min_message_count: int,
    include_group_only: bool,
) -> str:
    """First failing floor reason for an unmatched contact ("" = passes)."""
    phone = (row.get("phone") or "").strip()
    if "@" in phone:
        return "email_handle"
    digits = normalize_phoneish(phone)
    if not (MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS):
        return "short_code_or_invalid_phone"
    if normalize_bool(row.get("skip", "")) is True:
        return "skip_flag"
    name_reason = bad_name_reason(row.get("name") or "", phone)
    if name_reason:
        return name_reason
    message_count = parse_int_field(row.get("message_count"))
    if message_count < min_message_count:
        return "below_min_messages"
    if (
        not include_group_only
        and normalize_bool(row.get("is_in_group_chats", "")) is True
        and not has_whatsapp_direct_messages(row)
        and message_count < GROUP_ONLY_MIN_MESSAGES
    ):
        return "group_only_low_signal"
    return ""


def messages_source_channels(row: dict[str, str]) -> list[str]:
    """Channels the contact was seen on ('imessage'/'whatsapp'), from the
    source column plus any positive per-channel count; ['messages'] fallback."""
    channels: list[str] = []
    raw = str(row.get("source") or row.get("message_source") or "").strip().lower()
    for token in re.split(r"[,|+/;\s]+", raw):
        if token in {"imessage", "whatsapp"} and token not in channels:
            channels.append(token)
    for key, channel in (
        ("imessage_message_count", "imessage"),
        ("whatsapp_message_count", "whatsapp"),
    ):
        if parse_int_field(row.get(key)) > 0 and channel not in channels:
            channels.append(channel)
    return channels or ["messages"]


def contact_interaction_counts(row: dict[str, str]) -> dict[str, int]:
    """Positive per-channel DM counts, keyed by channel."""
    counts: dict[str, int] = {}
    for count_key, channel in (
        ("imessage_message_count", "imessage"),
        ("whatsapp_message_count", "whatsapp"),
    ):
        count = parse_int_field(row.get(count_key))
        if count > 0:
            counts[channel] = count
    return counts


def contact_last_interaction(row: dict[str, str]) -> str:
    """Most recent activity across the per-channel and legacy last-message columns."""
    return latest_interaction(
        row.get("imessage_last_message"),
        row.get("whatsapp_last_message"),
        row.get("last_message"),
    )
