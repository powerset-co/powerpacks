"""The Accounts page's JSON routes.

GET  /accounts/api/accounts   every connected account (accounts.read_accounts), the
                              Sync/Reconnect jobs this server started (by card key),
                              and the daily refresh's time when it is on.
GET  /accounts/api/signed-in  which sources need no sign-in (accounts.signed_in): what
                              setup will take, as its Connect screen shows it;
                              ?sources=imessage,... reads only those
                              Jobs and health arrive together, so a finished job never
                              shows next to the account's pre-sync health.
GET  /accounts/api/chrome-profiles  the user's Chrome profiles with a LinkedIn or Google session,
                              and the folder the sign-in browser was copied from
POST /accounts/api/chrome-profile   folder=<profile>: copy it into the sign-in browsers
POST /accounts/api/sync       source=imessage|whatsapp, or source=gmail&email=<address>:
                              sync that one card
POST /accounts/api/reconnect  email=<gmail>: open msgvault's Google sign-in, then sync
                              that account as soon as the sign-in lands

One user, one process. Jobs live in memory, keyed by card, so a reloaded page still
sees a running sync (a server restart forgets it; msgvault resumes on the next sync).
Gmail's sign-in check (msgvault list-accounts, then verify per mailbox) takes seconds on a large
mailbox, so both routes reuse one check until a job ends, a sign-in writes a token, or it is
SIGNIN_TTL_SECONDS old; a screen polling for Full Disk Access asks only for iMessage.
Starting a job that is already running is a no-op. The sync functions take turns on
refresh_sources' lock file (shared with the scheduled run); a Reconnect waits for the
browser sign-in before that, so an abandoned sign-in never blocks other syncs. 
Changelog:
  2026-11-04: signed-in takes ?sources=; both routes share one Gmail check (token change, job end, TTL).
  2026-11-03: `scheduled` is the native daily refresh's time, or null while it is off.
  2026-10-30: Chrome profiles to pick from.
  2026-10-13: GET /accounts/api/signed-in for setup's Connect screen.
  2026-09-28: created for the local Accounts page.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Callable, Literal

from packs.ingestion.primitives.accounts.accounts import (
    ALL_SOURCES,
    MSGVAULT_HOME,
    SignIn,
    gmail_signins,
    gmail_tokens,
    read_accounts,
    signed_in,
)
from packs.ingestion.primitives.refresh.refresh_sources import (
    SourceResult,
    sync_gmail,
    sync_imessage,
    sync_whatsapp,
)
from packs.ingestion.primitives.refresh.daily import read_settings
from packs.ingestion.primitives.setup.automations.accounts import add_account
from packs.ingestion.primitives.setup.automations.chrome_profiles import chrome_profiles, copied_from, use_chrome_profile

ACCOUNTS_PATH = "/accounts/api/accounts"
SIGNED_IN_PATH = "/accounts/api/signed-in"
CHROME_PROFILES_PATH = "/accounts/api/chrome-profiles"
CHROME_PROFILE_PATH = "/accounts/api/chrome-profile"
SYNC_PATH = "/accounts/api/sync"
RECONNECT_PATH = "/accounts/api/reconnect"

MESSAGE_SYNCS: dict[str, Callable[[], SourceResult]] = {"imessage": sync_imessage, "whatsapp": sync_whatsapp}


BUSY = ("running",)
# How long a Gmail sign-in check stands when no token changed: Google can still expire a grant
# without msgvault writing anything, and setup checks again itself before it takes Gmail.
SIGNIN_TTL_SECONDS = 300


@dataclass(frozen=True)
class Job:
    state: Literal["running", "done", "failed"]
    step: str


class AccountsApi:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._starting = threading.Lock()
        self._signins: dict[str, SignIn] | None = None
        self._tokens: tuple[tuple[str, int], ...] = ()
        self._checked = 0.0

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if parsed.path == SIGNED_IN_PATH:
            asked = urllib.parse.parse_qs(parsed.query).get("sources", [""])[0].split(",")
            sources = tuple(source for source in ALL_SOURCES if source in asked) or ALL_SOURCES
            signins = self._gmail() if "gmail" in sources else {}
            _send_json(handler, asdict(signed_in(signins, sources)))
            return True
        if parsed.path == CHROME_PROFILES_PATH:
            _send_json(handler, {"profiles": [asdict(profile) for profile in chrome_profiles()],
                                 "linked": copied_from()})
            return True
        if parsed.path != ACCOUNTS_PATH:
            return False
        _send_json(handler, {
            "accounts": [asdict(account) for account in read_accounts(self._gmail())],
            "jobs": {key: asdict(job) for key, job in self.jobs.items()},
            "scheduled": self._schedule(),
        })
        return True

    def _gmail(self) -> dict[str, SignIn]:
        """Every mailbox's sign-in, checked again only once the last check is out of date."""
        tokens = gmail_tokens()
        stale = time.monotonic() - self._checked > SIGNIN_TTL_SECONDS
        if self._signins is None or tokens != self._tokens or stale:
            self._signins = gmail_signins()
            self._tokens, self._checked = tokens, time.monotonic()
        return self._signins

    @staticmethod
    def _schedule() -> str | None:
        """The daily refresh's time ("6:00 AM"), or None while it is off."""
        settings = read_settings(Path.cwd())
        return settings.label() if settings.enabled else None

    def post(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if parsed.path not in (SYNC_PATH, RECONNECT_PATH, CHROME_PROFILE_PATH):
            return False
        length = int(handler.headers.get("Content-Length") or 0)
        form = {key: values[0] for key, values in urllib.parse.parse_qs(handler.rfile.read(length).decode()).items()}
        source, email = form.get("source"), form.get("email")
        if parsed.path == CHROME_PROFILE_PATH:
            use_chrome_profile(form["folder"])
            _send_json(handler, {"status": "copied"})
            return True
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
