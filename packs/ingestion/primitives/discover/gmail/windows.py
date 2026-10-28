"""How far back a Gmail sync could go: what each window holds and how long it takes.

Powerpacks counts a mailbox's mail before syncing it, with the same query the sync uses, by
paging message ids from Gmail (500 a page, under a second a page). The Accounts page shows the
count per window next to what is already synced and the time at the pace msgvault's own sync
runs have shown. Standard library only: the page server answers before the project's venv exists.

Created: 2026-10-28
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.discover.gmail.util import resolve_discovery_inputs
from packs.ingestion.primitives.setup.automations import msgvault_home

# The windows the slider offers; 0 years is everything.
WINDOWS = (1, 3, 5, 0)
PAGE = 500
# A pace to quote before any sync has run here: what the syncs measured on 2026-10-27.
DEFAULT_PACE = 2.0
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def access_token(home: Path, email: str) -> str:
    """msgvault's stored token for the mailbox, refreshed through its own OAuth client when stale."""
    token_path = home / "tokens" / f"{email}.json"
    token = json.loads(token_path.read_text(encoding="utf-8"))
    if token.get("expiry") and _expired(str(token["expiry"])) is False:
        return str(token["access_token"])
    secret = json.loads(Path(msgvault_home.parse_client_secret_paths(msgvault_home.config_path(home))["default"])
                        .expanduser().read_text(encoding="utf-8"))
    client = secret.get("installed") or secret.get("web") or {}
    body = urllib.parse.urlencode({
        "client_id": token.get("client_id") or client["client_id"], "client_secret": client["client_secret"],
        "refresh_token": token["refresh_token"], "grant_type": "refresh_token"}).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body), timeout=30) as response:
        return str(json.load(response)["access_token"])


def _expired(expiry: str) -> bool:
    # msgvault writes RFC 3339 with a zone; a minute of slack covers the request itself.
    try:
        from datetime import datetime
        stamp = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        return True
    return stamp.timestamp() - 60 < time.time()


def count(token: str, query: str, after: date | None) -> int:
    """How many messages match `query` (and `after`, when given), by paging ids."""
    q = f"{query} after:{after:%Y/%m/%d}".strip() if after else query
    total, page_token = 0, ""
    while True:
        params = {"q": q, "maxResults": PAGE, "fields": "nextPageToken,messages/id"}
        if page_token:
            params["pageToken"] = page_token
        request = urllib.request.Request(f"{GMAIL}?{urllib.parse.urlencode(params)}",
                                         headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
        total += len(payload.get("messages") or [])
        page_token = payload.get("nextPageToken") or ""
        if not page_token:
            return total


def pace(db: Path, email: str) -> float:
    """Messages a second over the mailbox's completed syncs here; the measured default before any."""
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT SUM(r.messages_processed), SUM(strftime('%s', r.completed_at) - strftime('%s', r.started_at)) "
            "FROM sync_runs r JOIN sources s ON s.id = r.source_id WHERE lower(s.identifier) = lower(?) "
            "AND r.status = 'completed' AND r.messages_processed > 0", (email,)).fetchone()
    processed, seconds = row or (0, 0)
    return processed / seconds if processed and seconds and seconds > 0 else DEFAULT_PACE


def synced(db: Path, email: str) -> int:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT COUNT(*) FROM messages m JOIN sources s ON s.id = m.source_id "
                           "WHERE lower(s.identifier) = lower(?)", (email,)).fetchone()
    return int(row[0]) if row else 0


def windows(home: Path, email: str) -> dict[str, Any]:
    """{"windows": [{"years", "messages"}...], "synced", "pace"} for the mailbox."""
    inputs = resolve_discovery_inputs(account_emails=[email])
    db = Path(inputs.msgvault_db)
    token = access_token(home, email)
    today = date.today()
    counts = [{"years": years, "messages": count(token, inputs.sync_query, today - timedelta(days=365 * years) if years else None)}
              for years in WINDOWS]
    return {"email": email, "windows": counts, "synced": synced(db, email), "pace": round(pace(db, email), 2)}


def window_start(years: int) -> str:
    """The sync's `after` date for a window; everything is no date."""
    return (date.today() - timedelta(days=365 * years)).isoformat() if years else ""
