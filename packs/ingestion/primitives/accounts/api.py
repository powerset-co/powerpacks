"""The Accounts page's JSON routes.

GET  /accounts/api/accounts   every connected account (accounts.read_accounts), the
                              Sync/Reconnect jobs this server started (by card key),
                              and whether a daily refresh task is installed.
                              Jobs and health arrive together, so a finished job never
                              shows next to the account's pre-sync health.
POST /accounts/api/sync       source=imessage|whatsapp, or source=gmail&email=<address>:
                              sync that one card
POST /accounts/api/reconnect  email=<gmail>: open msgvault's Google sign-in, then sync
                              that account as soon as the sign-in lands

One user, one process. Jobs live in memory, keyed by card, so a reloaded page still
sees a running sync (a server restart forgets it; msgvault resumes on the next sync).
Starting a job that is already running is a no-op. The sync functions take turns on
refresh_sources' lock file (shared with the scheduled run); a Reconnect waits for the
browser sign-in before that, so an abandoned sign-in never blocks other syncs. While a job runs, the
page's polls reuse the last Gmail sign-in checks instead of asking Google each time.

Changelog:
  2026-09-28: created for the local Accounts page.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Callable, Literal

from packs.ingestion.primitives.accounts.accounts import MSGVAULT_HOME, SignIn, gmail_signins, read_accounts
from packs.ingestion.primitives.refresh.refresh_sources import (
    SourceResult,
    sync_gmail,
    sync_imessage,
    sync_whatsapp,
)
from packs.ingestion.primitives.refresh.tasks import installed_runners
from packs.ingestion.primitives.setup.automations.accounts import add_account

ACCOUNTS_PATH = "/accounts/api/accounts"
SYNC_PATH = "/accounts/api/sync"
RECONNECT_PATH = "/accounts/api/reconnect"

MESSAGE_SYNCS: dict[str, Callable[[], SourceResult]] = {"imessage": sync_imessage, "whatsapp": sync_whatsapp}


BUSY = ("running",)


@dataclass(frozen=True)
class Job:
    state: Literal["running", "done", "failed"]
    step: str


class AccountsApi:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._starting = threading.Lock()
        self._signins: dict[str, SignIn] | None = None

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if parsed.path != ACCOUNTS_PATH:
            return False
        if not any(job.state in BUSY for job in self.jobs.values()) or self._signins is None:
            self._signins = gmail_signins()
        _send_json(handler, {
            "accounts": [asdict(account) for account in read_accounts(self._signins)],
            "jobs": {key: asdict(job) for key, job in self.jobs.items()},
            "scheduled": bool(installed_runners()),
        })
        return True

    def post(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if parsed.path not in (SYNC_PATH, RECONNECT_PATH):
            return False
        length = int(handler.headers.get("Content-Length") or 0)
        form = {key: values[0] for key, values in urllib.parse.parse_qs(handler.rfile.read(length).decode()).items()}
        source, email = form.get("source"), form.get("email")
        if parsed.path == SYNC_PATH and source in MESSAGE_SYNCS:
            self._start(source, lambda: self._sync(source, lambda: [MESSAGE_SYNCS[source]()]))
        elif parsed.path == SYNC_PATH and source == "gmail" and email:
            self._start(email, lambda: self._sync(email, lambda: sync_gmail(only=email)))
        elif parsed.path == RECONNECT_PATH and email:
            self._start(email, lambda: self._reconnect(email))
        else:
            _send_json(handler, {"error": "source or email is required"}, HTTPStatus.BAD_REQUEST)
            return True
        _send_json(handler, {"status": "started"})
        return True

    def _start(self, key: str, work: Callable[[], None]) -> None:
        with self._starting:
            if key in self.jobs and self.jobs[key].state in BUSY:
                return
            self.jobs[key] = Job("running", "Syncing…")

        def run() -> None:
            try:
                work()
            except Exception as error:  # noqa: BLE001 - the card shows why the job died
                self.jobs[key] = Job("failed", f"Failed: {error}")

        threading.Thread(target=run, daemon=True).start()

    def _reconnect(self, email: str) -> None:
        self.jobs[email] = Job("running", "Finish signing in to Google in your browser…")
        signed_in = add_account(MSGVAULT_HOME, email, "", headless=False, force=True)
        self._signins = None
        if signed_in["status"] != "ok":
            self.jobs[email] = Job("failed", "Sign-in didn't finish. Try Reconnect again.")
            return
        self._sync(email, lambda: sync_gmail(only=email))

    def _sync(self, key: str, sync: Callable[[], list[SourceResult]]) -> None:
        self.jobs[key] = Job("running", "Syncing…")
        results = sync()
        failed = [result.note for result in results if result.outcome == "failed"]
        # A finished job (a Reconnect above all) can change a sign-in; ask Google again.
        self._signins = None
        self.jobs[key] = Job("failed", " ".join(failed)) if failed else Job("done", "Synced just now.")


def _send_json(handler: BaseHTTPRequestHandler, payload: object, status: int = HTTPStatus.OK) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)
