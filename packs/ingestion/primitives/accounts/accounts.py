"""Connected message accounts, read live from each source's own store.

Nothing here keeps an account list. Every read goes to the store the source
tool owns, so the page can never disagree with it:

    Gmail     ~/.msgvault/msgvault.db  sources + sync_runs + messages (one row per account)
              sign-in: auth-check's per-account probe (msgvault verify, no mail download)
    iMessage  ~/Library/Messages/chat.db  message/handle counts, newest message
    WhatsApp  .powerpacks/messages/wacli/wacli.db  message/chat counts, newest message
    LinkedIn  discover/linkedin/Connections.csv  connection count; the file's age is its sync

Contact counts and the last import come from each discover manifest. An
expired Gmail sign-in is the 7-day grant of a Testing-status OAuth app lapsing.

Changelog:
  2026-09-28: created for the local Accounts page.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.paths import (  # noqa: E402
    DEFAULT_DISCOVER_DIR,
    DEFAULT_MSGVAULT_DB,
    MESSAGES_OUT_DIR,
)
from packs.ingestion.primitives.discover.messages.chatdb import apple_timestamp_to_iso  # noqa: E402
from packs.ingestion.primitives.discover.messages.extract_imessage import DEFAULT_CHAT_DB  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.paths import DEFAULT_STORE  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.store_db import whatsapp_epoch_to_iso  # noqa: E402
from packs.ingestion.primitives.imports.status import linkedin_connections_count  # noqa: E402
from packs.ingestion.primitives.setup.automations.accounts import check_account  # noqa: E402

Source = Literal["gmail", "imessage", "whatsapp", "linkedin"]
Health = Literal["ok", "warning", "error", "off"]
# auth-check verdicts (setup/automations/accounts.py CHECK_BUCKETS).
SignIn = Literal["healthy", "missing_token", "reauthorization_required", "transient_error"]

# A daily refresh that has not landed for this long is worth a warning.
STALE_SYNC_DAYS = 2
# LinkedIn has no sync: the user re-downloads Connections.csv now and then.
STALE_LINKEDIN_DAYS = 30

MSGVAULT_HOME = DEFAULT_MSGVAULT_DB.parent
IMESSAGE_MANIFEST = MESSAGES_OUT_DIR / "imessage.manifest.json"
WHATSAPP_MANIFEST = MESSAGES_OUT_DIR / "whatsapp.contacts.csv.manifest.json"
GMAIL_MANIFEST = DEFAULT_DISCOVER_DIR / "gmail" / "manifest.json"
LINKEDIN_EXPORT = DEFAULT_DISCOVER_DIR / "linkedin" / "Connections.csv"


@dataclass(frozen=True)
class Account:
    source: Source
    name: str
    health: Health
    note: str
    messages: int
    contacts: int
    latest_message_at: str | None
    last_sync_at: str | None


def read_accounts(signins: dict[str, SignIn] | None = None) -> list[Account]:
    """Every account. `signins` reuses earlier Gmail sign-in checks (the page does
    while a sync runs, instead of asking Google every poll); None checks now."""
    return [*_gmail_accounts(signins), _imessage_account(), _whatsapp_account(), _linkedin_account()]


def gmail_signins() -> dict[str, SignIn]:
    """Each stored Gmail account's sign-in, from msgvault's zero-download verify probe."""
    if not DEFAULT_MSGVAULT_DB.exists():
        return {}
    emails = [row.email for row in _gmail_rows(DEFAULT_MSGVAULT_DB)]
    with ThreadPoolExecutor(max_workers=len(emails) or 1) as pool:
        checks = pool.map(lambda email: check_account(MSGVAULT_HOME, email, stored=True).status, emails)
    return dict(zip(emails, checks))


# ---------------------------------------------------------------- Gmail


@dataclass(frozen=True)
class _GmailRow:
    email: str
    messages: int
    latest_message_at: str | None
    last_sync_at: str | None
    last_run_status: str | None
    last_run_error: str | None


def _gmail_accounts(signins: dict[str, SignIn] | None) -> list[Account]:
    if not DEFAULT_MSGVAULT_DB.exists():
        return [Account("gmail", "Gmail", "off", "Not connected", 0, 0, None, None)]
    rows = _gmail_rows(DEFAULT_MSGVAULT_DB)
    if signins is None or any(row.email not in signins for row in rows):
        signins = gmail_signins()
    contacts = _gmail_contacts()
    return [_gmail_account(row, signins[row.email], contacts.get(row.email, 0)) for row in rows]


def _gmail_rows(db: Path) -> list[_GmailRow]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        sources = conn.execute(
            "SELECT id, identifier, last_sync_at FROM sources WHERE source_type = 'gmail' ORDER BY identifier"
        ).fetchall()
        rows = []
        for source_id, email, last_sync_at in sources:
            messages, latest = conn.execute(
                "SELECT COUNT(*), MAX(sent_at) FROM messages WHERE source_id = ?", (source_id,)
            ).fetchone()
            run = conn.execute(
                "SELECT status, error_message FROM sync_runs WHERE source_id = ? ORDER BY started_at DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            rows.append(_GmailRow(
                email=email,
                messages=messages,
                latest_message_at=_sqlite_iso(latest),
                last_sync_at=_sqlite_iso(last_sync_at),
                last_run_status=run[0] if run else None,
                last_run_error=run[1] if run else None,
            ))
    return rows


def _gmail_contacts() -> dict[str, int]:
    manifest = _read_json(GMAIL_MANIFEST)
    return {child["account_email"]: child["contacts"] for child in manifest.get("children", [])}


def _gmail_account(row: _GmailRow, signin: SignIn, contacts: int) -> Account:
    health, note = _gmail_health(row, signin)
    return Account(
        source="gmail",
        name=row.email,
        health=health,
        note=note,
        messages=row.messages,
        contacts=contacts,
        latest_message_at=row.latest_message_at,
        last_sync_at=row.last_sync_at,
    )


def _gmail_health(row: _GmailRow, signin: SignIn) -> tuple[Health, str]:
    if signin == "reauthorization_required":
        return "error", "Sign-in expired. Reconnect to keep syncing."
    if signin == "missing_token":
        return "error", "Not signed in. Reconnect to sync."
    if row.last_run_status == "failed":
        # Signed in, so it's the sync to retry, not the sign-in.
        return "warning", f"Last sync failed: {row.last_run_error or 'unknown error'}"
    if _days_since(row.last_sync_at) > STALE_SYNC_DAYS:
        return "warning", "Signed in, but no sync in the last 2 days."
    if signin == "transient_error":
        return "warning", "Couldn't reach Google to check the sign-in."
    return "ok", "Signed in and up to date."


# ---------------------------------------------------------------- iMessage


def _imessage_account() -> Account:
    manifest = _read_json(IMESSAGE_MANIFEST)
    contacts = manifest.get("counts", {}).get("contacts", 0)
    imported_at = manifest.get("completed_at")
    try:
        with sqlite3.connect(f"file:{DEFAULT_CHAT_DB}?mode=ro", uri=True) as conn:
            messages, latest = conn.execute("SELECT COUNT(*), MAX(date) FROM message").fetchone()
    except sqlite3.Error:
        return Account("imessage", "iMessage", "error", "Full Disk Access is off for this terminal.",
                       0, contacts, None, imported_at)
    health, note = _import_health(imported_at)
    return Account("imessage", "iMessage", health, note, messages, contacts,
                   apple_timestamp_to_iso(latest), imported_at)


# ---------------------------------------------------------------- WhatsApp


def _whatsapp_account() -> Account:
    db = DEFAULT_STORE / "wacli.db"
    if not db.exists():
        return Account("whatsapp", "WhatsApp", "off", "Not connected", 0, 0, None, None)
    manifest = _read_json(WHATSAPP_MANIFEST)
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        messages, latest = conn.execute("SELECT COUNT(*), MAX(ts) FROM messages").fetchone()
    imported_at = manifest.get("completed_at")
    if not manifest.get("auth", {}).get("authenticated_after", True):
        health, note = "error", "WhatsApp unlinked this Mac. Scan the QR code again."
    else:
        health, note = _import_health(imported_at)
    return Account(
        source="whatsapp",
        name="WhatsApp",
        health=health,
        note=note,
        messages=messages,
        contacts=manifest.get("counts", {}).get("contacts", 0),
        latest_message_at=whatsapp_epoch_to_iso(latest),
        last_sync_at=imported_at,
    )


# ---------------------------------------------------------------- LinkedIn


def _linkedin_account() -> Account:
    """The Connections.csv export: its connection count, and its age from when it
    was saved here. LinkedIn has no live store, so "last sync" is the export."""
    if not LINKEDIN_EXPORT.exists():
        return Account("linkedin", "LinkedIn", "off", "No Connections.csv yet.", 0, 0, None, None)
    saved_at = datetime.fromtimestamp(LINKEDIN_EXPORT.stat().st_mtime, timezone.utc).isoformat()
    age = _days_since(saved_at)
    health, note = (("warning", f"Connections.csv is {int(age)} days old. Download a fresh export from LinkedIn.")
                    if age > STALE_LINKEDIN_DAYS else ("ok", "Up to date"))
    return Account("linkedin", "LinkedIn", health, note, 0, linkedin_connections_count(LINKEDIN_EXPORT),
                   None, saved_at)


# ---------------------------------------------------------------- shared


def _import_health(imported_at: str | None) -> tuple[Health, str]:
    if imported_at is None:
        return "off", "Not imported yet"
    if _days_since(imported_at) > STALE_SYNC_DAYS:
        return "warning", "No refresh in the last 2 days."
    return "ok", "Up to date"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _sqlite_iso(value: str | None) -> str | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace(" ", "T"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat()


def _days_since(iso: str | None) -> float:
    if iso is None:
        return float("inf")
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - moment).total_seconds() / 86_400


if __name__ == "__main__":
    print(json.dumps([asdict(account) for account in read_accounts()], indent=2))
