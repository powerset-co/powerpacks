#!/usr/bin/env python3
"""Refresh the already-connected message sources. No model runs in here.

A Codex or Claude scheduled task runs this every morning (tasks.py installs
it) and reports the JSON it prints. A source that needs the user (expired
Gmail sign-in, WhatsApp unlinked, Full Disk Access off) is reported, never
fixed here. Nothing here opens a browser, a QR page or
System Settings, and nothing runs past import: no fan-in, Deep Context, LLM,
paid lookup, index or upload.

    Gmail     auth-check every msgvault account (or just the one asked for) →
              msgvault sync the healthy ones → rebuild discovery from EVERY stored
              account (skip sync), so an unsynced or expired account keeps its
              archived contacts → Gmail import
    iMessage  if chat.db is readable: Messages discovery for iMessage → Messages import
    WhatsApp  if wacli is still linked: Messages discovery for WhatsApp → Messages import

`sync_gmail`, `sync_imessage` and `sync_whatsapp` are also the Accounts page's
Sync buttons (and its automatic sync after a Gmail reconnect). Each holds
`.powerpacks/refresh/sync.lock` while it runs: the page's server and the
scheduled run are different processes, and their imports rewrite the same files.

Changelog:
  2026-09-28: created; replaces the Codex App automation of PR #365.
"""

from __future__ import annotations

import argparse
import fcntl
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Literal

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit, now_iso  # noqa: E402
from packs.ingestion.primitives.accounts.accounts import MSGVAULT_HOME  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_MSGVAULT_DB  # noqa: E402
from packs.ingestion.primitives.discover.gmail.discover import GmailDiscovery  # noqa: E402
from packs.ingestion.primitives.discover.gmail.msgvault.sync import sync_msgvault_account  # noqa: E402
from packs.ingestion.primitives.discover.gmail.util import resolve_discovery_inputs  # noqa: E402
from packs.ingestion.primitives.discover.messages.discover import MessagesDiscovery  # noqa: E402
from packs.ingestion.primitives.discover.messages.extract_imessage import DEFAULT_CHAT_DB  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.auth import auth_status  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.paths import DEFAULT_STORE  # noqa: E402
from packs.ingestion.primitives.imports.gmail.importer import GmailImport  # noqa: E402
from packs.ingestion.primitives.imports.messages.importer import MessagesImport  # noqa: E402
from packs.ingestion.primitives.setup.automations.accounts import check_accounts_payload  # noqa: E402

Outcome = Literal["refreshed", "needs_you", "failed", "not_connected"]

SYNC_LOCK = Path(".powerpacks/refresh/sync.lock")


@dataclass(frozen=True)
class SourceResult:
    source: Literal["gmail", "imessage", "whatsapp"]
    outcome: Outcome
    note: str
    accounts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RefreshRun:
    started_at: str
    finished_at: str
    status: Literal["ok", "needs_you", "failed"]
    sources: list[SourceResult]


def refresh() -> RefreshRun:
    started_at = now_iso()
    results = [*sync_gmail(), sync_imessage(), sync_whatsapp()]
    return RefreshRun(started_at, now_iso(), _run_status(results), results)


@contextmanager
def _one_sync_at_a_time() -> Iterator[None]:
    SYNC_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with SYNC_LOCK.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _run_status(results: list[SourceResult]) -> Literal["ok", "needs_you", "failed"]:
    outcomes = {result.outcome for result in results}
    if "failed" in outcomes:
        return "failed"
    return "needs_you" if "needs_you" in outcomes else "ok"


# ---------------------------------------------------------------- Gmail


def sync_gmail(only: str | None = None) -> list[SourceResult]:
    """New mail for every signed-in account, or for `only` (one card's Sync). The
    contact rebuild always reads every stored account, so none is ever dropped."""
    with _one_sync_at_a_time():
        return _sync_gmail(only)


def _sync_gmail(only: str | None) -> list[SourceResult]:
    emails = _msgvault_emails()
    if not emails:
        return [SourceResult("gmail", "not_connected", "No Gmail account in msgvault.")]
    check = check_accounts_payload(MSGVAULT_HOME, [only] if only else emails)
    if check["error_accounts"]:
        return [SourceResult("gmail", "failed", "Couldn't reach Google to check sign-ins.", check["error_accounts"])]

    config = resolve_discovery_inputs(account_emails=emails)
    for email in check["healthy_accounts"]:
        synced = sync_msgvault_account(email, config.msgvault_db, config.sync_query)
        if synced["status"] == "failed":
            return [SourceResult("gmail", "failed", f"msgvault sync failed for {email}.", [email])]
    discovered = GmailDiscovery(account_emails=emails, skip_msgvault_sync=True).run()
    if discovered.status == "failed":
        return [SourceResult("gmail", "failed", "Gmail discovery failed.", emails)]
    if GmailImport().run().status == "failed":
        return [SourceResult("gmail", "failed", "Gmail import failed.", emails)]

    results = [SourceResult("gmail", "refreshed", "Synced.", check["healthy_accounts"])] if check["healthy_accounts"] else []
    if check["accounts_to_authorize"]:
        results.append(SourceResult("gmail", "needs_you", "Gmail sign-in expired. Reconnect on the Accounts page.",
                                    check["accounts_to_authorize"]))
    return results


def _msgvault_emails() -> list[str]:
    if not DEFAULT_MSGVAULT_DB.exists():
        return []
    with sqlite3.connect(f"file:{DEFAULT_MSGVAULT_DB}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT identifier FROM sources WHERE source_type = 'gmail' ORDER BY identifier")
        return [row[0] for row in rows]


# ---------------------------------------------------------------- Messages


def sync_imessage() -> SourceResult:
    try:
        with sqlite3.connect(f"file:{DEFAULT_CHAT_DB}?mode=ro", uri=True) as conn:
            conn.execute("SELECT 1 FROM message LIMIT 1")
    except sqlite3.Error:
        return SourceResult("imessage", "needs_you", "Full Disk Access is off for the refresh job.")
    with _one_sync_at_a_time():
        return _discover_and_import(MessagesDiscovery(include_imessage=True), "imessage")


def sync_whatsapp() -> SourceResult:
    if not (DEFAULT_STORE / "wacli.db").exists():
        return SourceResult("whatsapp", "not_connected", "WhatsApp isn't linked.")
    if not auth_status(DEFAULT_STORE).authenticated:
        return SourceResult("whatsapp", "needs_you", "WhatsApp unlinked this Mac. Scan the QR code again.")
    with _one_sync_at_a_time():
        return _discover_and_import(MessagesDiscovery(include_whatsapp=True), "whatsapp")


def _discover_and_import(discovery: MessagesDiscovery, source: Literal["imessage", "whatsapp"]) -> SourceResult:
    discovered = discovery.run()
    if discovered.status != "completed":
        return SourceResult(source, "failed", f"Discovery {discovered.status}.")
    if MessagesImport().run().status == "failed":
        return SourceResult(source, "failed", "Messages import failed.")
    return SourceResult(source, "refreshed", "Synced.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run"])
    parser.parse_args()
    run = refresh()
    emit(asdict(run))
    return 1 if run.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
