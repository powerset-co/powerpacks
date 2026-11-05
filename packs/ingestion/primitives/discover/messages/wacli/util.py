"""Pure value helpers for the wacli client: no subprocess, no SQLite, no I/O.

Two groups, both taking plain values so they are safe to reuse and unit-test in
isolation:

- WhatsApp identity: `canonicalize_phone` / `jid_to_phone` / `clean_name` turn
  wacli's JIDs, phone strings, and display names into the `+E164` / squeezed-name
  forms the contact rows use. PINNED as WhatsApp-specific: a JID carries the
  country code with no `+`, so this pair is not interchangeable with the
  generic `common/contact_fields.py` normalizers.
- History-depth values: the hashed `history_chat_ref` (raw JIDs are never
  persisted in stage artifacts), the three-calendar-year `history_depth_cutoff_ts`,
  and `history_depth_state_digest` over `{chat_jid: (count, latest_ts)}`.

`linked_device_blocked` and `result_int` are the two tolerant readers that go
with them: WhatsApp's "can't link new devices" text in any wacli output, and the
int coercion for CSV/JSON fields that may be blank, missing, or non-numeric.

Changelog:
  2026-07-30 (wacli split): extracted from the single-file `whatsapp_wacli.py`
    as the connection-free half (mirrors `gmail/msgvault/util.py`). Behavior
    unchanged.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

MIN_PHONE_DIGITS = 7
MAX_PHONE_DIGITS = 15
DEFAULT_HISTORY_DEPTH_LOOKBACK_YEARS = 3


def canonicalize_phone(raw: str | None) -> str:
    value = (raw or "").strip()
    if "@" in value:
        return jid_to_phone(value) or ""
    digits = re.sub(r"[^\d]", "", value)
    if len(digits) < MIN_PHONE_DIGITS:
        return ""
    if value.startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) <= MAX_PHONE_DIGITS:
        return f"+{digits}"
    return ""


def jid_to_phone(jid: str | None) -> str | None:
    value = (jid or "").strip()
    if not value or "@g.us" in value or "@lid" in value or "@newsletter" in value:
        return None
    match = re.match(r"(\d+)@", value)
    if not match:
        if "@" not in value:
            return canonicalize_phone(value) or None
        return None
    digits = match.group(1)
    if MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
        return f"+{digits}"
    return None


def clean_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def linked_device_blocked(text: str) -> bool:
    lowered = text.lower()
    return (
        "can't link new devices right now" in lowered
        or "cannot link new devices right now" in lowered
        or ("link" in lowered and "device" in lowered and "try again later" in lowered)
        or "cannot link more devices" in lowered
    )


# What wacli prints when WhatsApp refuses a QR scan (whatsmeow's QR channel errors; passkey-gated
# accounts from wacli 0.17.1, openclaw/wacli#355), and what the page says about each instead.
PASSKEY_LINE = ("WhatsApp asked this account for passkey verification, which linking by QR can't do yet. "
                "Link with your phone number instead.")
_WRONG_SCREEN = ("That scan didn't link. Link with your phone number, or scan from Settings → Linked devices → "
                 "Link a device.")
_REFUSED = "WhatsApp didn't accept the QR scan. Link with your phone number instead."
# A QR run that ended unlinked with no reason wacli names (a stale code WhatsApp refused silently,
# or wacli's own QR timeout).
QR_ENDED_LINE = "The QR code didn't link. Link with your phone number instead."
# wacli's own wording (powerset-co/wacli, from upstream #356); in --events mode these arrive as
# {"event":"error","data":{"message":...}}.
_QR_FAILURES = (
    ("passkey", PASSKEY_LINE),
    ("multi-device is not enabled", _WRONG_SCREEN),
    ("unsupported qr pairing state", _REFUSED),
    ("unexpected qr pairing state", _REFUSED),
    ("qr pairing failed", _REFUSED),
    ("qr code timed out", QR_ENDED_LINE),
)


def qr_link_failure(text: str) -> str | None:
    """What to tell the person when wacli's output says WhatsApp refused the scan, else None."""
    lowered = text.lower()
    return next((line for marker, line in _QR_FAILURES if marker in lowered), None)


def result_int(row: dict[str, Any], key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def history_chat_ref(jid: str) -> str:
    return "wa-" + hashlib.sha256(jid.encode("utf-8")).hexdigest()[:16]


def history_depth_cutoff_ts(now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    try:
        cutoff = current.replace(year=current.year - DEFAULT_HISTORY_DEPTH_LOOKBACK_YEARS)
    except ValueError:
        cutoff = current.replace(
            year=current.year - DEFAULT_HISTORY_DEPTH_LOOKBACK_YEARS,
            day=28,
        )
    return int(cutoff.timestamp())


def history_depth_state_digest(states: dict[str, tuple[int, int]]) -> str:
    digest = hashlib.sha256()
    for chat_jid, (message_count, latest_ts) in sorted(states.items()):
        digest.update(history_chat_ref(chat_jid).encode("ascii"))
        digest.update(f":{message_count}:{latest_ts}\n".encode("ascii"))
    return digest.hexdigest()
