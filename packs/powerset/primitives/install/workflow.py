"""Run selected local imports and show their progress on the install page.

Source manifests own reuse. Gmail authorization, Messages permission checks
and the LinkedIn login continue in this process; missing setup or failed
primitives stop the flow. It never starts enrichment, provider calls, or uploads.

Changelog:
  2026-10-05: LinkedIn's login is its own step, and the plan lists every
      login step before every sync step, the order a run takes them. Linking
      WhatsApp only pairs it; its sync step downloads the history.
  2026-10-05: Gmail defaults to the Powerset login's address when no address
      was given; it asks only when there is no Powerset account.
  2026-10-05: when the automated Google Cloud setup stops, the step says so in
      one line and keeps Google's details for the agent instead of handing the
      user its manual steps (open the console, download the client secret).
  2026-10-03: Gmail's OAuth app is created in process (headless Chrome after
      one login) instead of stopping for the agent; LinkedIn prepares its own
      tools; the unreachable `skip` source, the Gmail-account question, the
      second "already running" check and the standalone main() are deleted.
      Gmail asks once which accounts to add (the first owns the OAuth app)
      unless msgvault already has some, and makes every one an OAuth test
      user before its consent. A run prepares every tool, then collects every
      login back to back (LinkedIn, Google, Full Disk Access, WhatsApp QR),
      then syncs and imports on its own.
  2026-10-03: LinkedIn is a default source, runs first, and reads the
      connections list in Chrome instead of waiting for LinkedIn's emailed export.
"""
from __future__ import annotations

import argparse
import os
import shlex
import time
import traceback
from datetime import date, timedelta
from enum import Enum
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import read_json
from packs.ingestion.primitives.discover.gmail.discover import GmailDiscovery
from packs.ingestion.primitives.discover.linkedin.connections import CONNECTIONS_CSV, SCRAPE_RECORD, LinkedInConnections
from packs.ingestion.primitives.discover.gmail.msgvault.sync import parse_msgvault_sync_date
from packs.ingestion.primitives.discover.messages.discover import MessagesDiscovery
from packs.ingestion.primitives.discover.messages.extract_imessage import IMessageExtractor
from packs.ingestion.primitives.discover.messages.wacli import auth
from packs.ingestion.primitives.discover.messages.wacli.paths import DEFAULT_STORE
from packs.ingestion.primitives.discover.messages.wacli.runtime import PrimitiveBlocked
from packs.ingestion.primitives.imports import common as import_common
from packs.ingestion.primitives.imports.gmail.importer import GmailImport
from packs.ingestion.primitives.imports.messages.importer import MessagesImport
from packs.ingestion.primitives.setup.automations import accounts, msgvault_home
from packs.ingestion.primitives.setup.automations.browser_flows import BrowserSetup, TestUsers
from packs.ingestion.primitives.setup.msgvault_setup import build_parser as msgvault_parser
from packs.powerset.primitives.install.status import PROCESSING_STEPS, InstallState, InstallStatus, InstallStep
from packs.powerset.primitives.install.tools import ImportTools


class Source(str, Enum):
    """Members run in this order; LinkedIn first so its one login comes up front."""
    LINKEDIN = "linkedin"
    GMAIL = "gmail"
    IMESSAGE = "imessage"
    WHATSAPP = "whatsapp"


# A run walks every source's login steps, then every source's sync steps; the
# plan lists them in that order so the page shows them as they happen.
_LOGIN_STEPS = {
    Source.LINKEDIN: (InstallStep.LINKEDIN_LOGIN,),
    Source.GMAIL: (InstallStep.GMAIL_TOOLS, InstallStep.GMAIL_LOGIN),
    Source.IMESSAGE: (InstallStep.IMESSAGE_ACCESS,),
    Source.WHATSAPP: (InstallStep.WHATSAPP_TOOLS, InstallStep.WHATSAPP_LOGIN),
}
_SYNC_STEPS = {
    Source.LINKEDIN: (InstallStep.LINKEDIN,),
    Source.GMAIL: (InstallStep.GMAIL_SYNC, InstallStep.GMAIL_IMPORT),
    Source.IMESSAGE: (InstallStep.IMESSAGE_IMPORT,),
    Source.WHATSAPP: (InstallStep.WHATSAPP_SYNC, InstallStep.WHATSAPP_IMPORT),
}
_SOURCE_STEPS = {source: _LOGIN_STEPS[source] + _SYNC_STEPS[source] for source in Source}
_SUCCESS = {"ok", "completed", "linked", "skipped"}
_WAITING = {"needs_user_action", "blocked_user_action", "needs_approval"}
_DEFAULT_SOURCES = (Source.LINKEDIN, Source.GMAIL, Source.IMESSAGE, Source.WHATSAPP)
_PERMISSION_POLL_SECONDS = 2
GMAIL_SETUP_STOPPED = "Gmail setup stopped in Google Cloud. I'm looking into it."
GMAIL_QUESTION = "Which Gmail accounts should I add? The first one owns the Gmail setup."
WHATSAPP_SYNCING = "Syncing WhatsApp. The first sync takes 30 minutes to a few hours."
_TOOL_STEPS = {Source.LINKEDIN: InstallStep.LINKEDIN_LOGIN, Source.GMAIL: InstallStep.GMAIL_TOOLS,
               Source.WHATSAPP: InstallStep.WHATSAPP_TOOLS}


class SourceOnboarding:
    def __init__(self, root: Path, *, sources: tuple[str, ...],
                 gmail_emails: tuple[str, ...] = (), sync_after: str = "",
                 wacli_store: Path | None = None, refresh: bool = False,
                 skip_sources: tuple[str, ...] = ()) -> None:
        self.root = root.resolve()
        self.status = InstallStatus(self.root)
        previous = self.status.read()
        command = shlex.split(previous["retry_command"])
        saved, _ = _parser().parse_known_args(command[1:] if Path(command[0]).name == "onboard" else [])
        selected = set(Source(source) for source in (sources or saved.source or _DEFAULT_SOURCES))
        self.sources = tuple(source for source in Source if source in selected)
        skip_sources = skip_sources if sources else (*saved.skip_source, *skip_sources)
        self.skip_sources = tuple(dict.fromkeys(Source(source) for source in skip_sources))
        if not set(self.skip_sources) <= set(self.sources):
            raise ValueError("Skip only a selected source")
        self.gmail_emails = gmail_emails or tuple(saved.gmail_email)
        # Mailboxes msgvault already holds are reused; otherwise Gmail uses the
        # Powerset login (in run()), and asks only without one.
        if not self.gmail_emails and Source.GMAIL in self.sources and Source.GMAIL not in self.skip_sources:
            home = Path(os.environ.get("MSGVAULT_HOME", "~/.msgvault")).expanduser()
            self.gmail_emails = tuple(sorted(
                accounts.VaultHealth.from_status(accounts.status_payload(home)).stored_emails - {""}))
        self.sync_after = sync_after or saved.sync_after or (
            (date.today() - timedelta(days=365)).isoformat() if Source.GMAIL in self.sources else "")
        wacli_store = wacli_store or saved.wacli_store
        self.wacli_store = wacli_store or self.root / DEFAULT_STORE
        self.refresh = refresh or (not sources and saved.refresh)
        self.step = InstallStep.SOURCES
        if all(source in self.skip_sources for source in self.sources):
            next_steps = (InstallStep.READY,)
        else:
            next_steps = tuple(step for step in PROCESSING_STEPS if step is not InstallStep.REVIEW)
        steps = [InstallStep.SOURCES.value,
                 *(step.value for source in self.sources for step in _LOGIN_STEPS[source]),
                 *(step.value for source in self.sources for step in _SYNC_STEPS[source]),
                 *(step.value for step in next_steps)]
        history = [step for step in previous.get("plan", []) if step not in PROCESSING_STEPS and step not in steps
                   and previous.get("steps", {}).get(step, {}).get("status") in {"completed", "skipped"}]
        self.plan = list(dict.fromkeys([*history, *steps]))
        args = [str(self.root / "bin/onboard")]
        for source in self.sources:
            args.extend(("--source", source.value))
        for email in self.gmail_emails:
            args.extend(("--gmail-email", email))
        if self.sync_after:
            args.extend(("--sync-after", self.sync_after))
        if wacli_store is not None:
            args.extend(("--wacli-store", str(wacli_store)))
        if self.refresh:
            args.append("--refresh")
        for source in self.skip_sources:
            args.extend(("--skip-source", source.value))
        self.retry_command = shlex.join(args)

    def _write(self, step: InstallStep, state: InstallState, message: str,
               action: dict | None = None, *, pid: int | None = None) -> dict:
        self.step = step
        return self.status.write(step=step, status=state, message=message,
                                 pid=os.getpid() if pid is None else pid, retry_command=self.retry_command,
                                 plan=self.plan, action=action)

    def _result(self, step: InstallStep, payload: dict, action: dict | None = None) -> bool:
        if payload["status"] in _SUCCESS:
            self._write(step, InstallState.COMPLETED, payload.get("message", "Done"))
            return True
        state = InstallState.WAITING if payload["status"] in _WAITING else InstallState.FAILED
        self._write(step, state, payload.get("message") or payload.get("reason") or "This step needs attention",
                    {**(action or {}), "details": payload}, pid=0 if state is InstallState.WAITING else None)
        return False

    def _tools(self, source: Source, step: InstallStep) -> bool:
        self._write(step, InstallState.RUNNING, "Preparing import tools")
        payload = ImportTools(sources=(source.value,)).run()
        return self._result(step, payload, {"command": payload["command"]} if "command" in payload else None)

    def _gmail_covered(self, current: import_common.ImportManifest) -> bool:
        manifest = Path(current.input.get("discovery_manifest", ".powerpacks/network-import/discover/gmail/manifest.json"))
        discovery = read_json(manifest, {})
        requested = parse_msgvault_sync_date(self.sync_after)
        coverage = {}
        for child in discovery.get("children", []):
            sync = child.get("sync", {})
            after = sync.get("sync_after")
            date = parse_msgvault_sync_date(after)
            covers = (child.get("status") == "completed" and sync.get("status") == "completed"
                      and after is not None and (after == "" or sync.get("sync_after_source") == "explicit_window")
                      and (after == "" or bool(date) and date <= requested)
                      and not sync.get("sync_before") and not sync.get("query") and not sync.get("limit"))
            coverage[child["account_email"].lower()] = covers
        return bool(requested) and all(coverage.get(email.lower(), False) for email in self.gmail_emails)

    def _gmail_connect(self) -> bool:
        self._write(InstallStep.GMAIL_LOGIN, InstallState.RUNNING, "Checking Gmail access")
        home = Path(os.environ.get("MSGVAULT_HOME", "~/.msgvault")).expanduser()
        local = accounts.status_payload(home)
        if not local["config"]["oauth_configured"]:
            self._write(InstallStep.GMAIL_LOGIN, InstallState.RUNNING,
                        "Setting up Gmail access. Sign in to Google in your browser if it asks.")
            created = BrowserSetup.from_args(msgvault_parser().parse_args(
                ["browser-setup", "--home", str(home), "--email", self.gmail_emails[0], "--no-install-mcp"])).run()
            if created["status"] == "needs_user_action":
                # The automation stopped in Google Cloud; the agent reads why, the user is not handed its steps.
                self._write(InstallStep.GMAIL_LOGIN, InstallState.WAITING, GMAIL_SETUP_STOPPED,
                            {"kind": "gmail", "text": GMAIL_SETUP_STOPPED, "details": created}, pid=0)
                return False
            if not self._result(InstallStep.GMAIL_LOGIN, created, {"kind": "gmail"}):
                return False
            local = accounts.status_payload(home)
        # Every mailbox must be an OAuth test user before its consent can succeed.
        allowed = set(accounts.normalize_email_list(list(msgvault_home.load_setup_state(home, "").test_users)))
        missing = [email for email in accounts.normalize_email_list(list(self.gmail_emails)) if email not in allowed]
        if missing:
            self._write(InstallStep.GMAIL_LOGIN, InstallState.RUNNING, "Allowing your Gmail accounts")
            added = TestUsers.from_args(msgvault_parser().parse_args(
                ["add-test-users", "--home", str(home), "--login-email", self.gmail_emails[0], *missing])).run()
            if not self._result(InstallStep.GMAIL_LOGIN, added, {"kind": "gmail"}):
                return False
        if local["database"]["exists"]:
            health = accounts.check_accounts_payload(home, list(self.gmail_emails))
            if health["status"] == "error":
                return self._result(InstallStep.GMAIL_LOGIN, health, {"kind": "gmail"})
            checks = health.get("accounts", [])
        else:
            checks = [accounts.check_account(home, email, stored=False).record()
                      for email in accounts.normalize_email_list(list(self.gmail_emails))]
            health = local
        authorize = [check for check in checks if check["status"] in {"missing_token", "reauthorization_required"}]
        for check in authorize:
            action = {"kind": "gmail", "text": f"Connect {check['email']} in your browser", "details": check}
            self._write(InstallStep.GMAIL_LOGIN, InstallState.WAITING, "Connect Gmail", action)
            while True:
                result = accounts.add_account(home, check["email"], "", headless=False,
                                              force=check["status"] == "reauthorization_required")
                if result.get("message") != "msgvault timed out":
                    break
                self._write(InstallStep.GMAIL_LOGIN, InstallState.WAITING,
                            "The Gmail sign-in expired. Opening a fresh one.", action)
            if not self._result(InstallStep.GMAIL_LOGIN, result, action):
                return False
        if authorize:
            health = accounts.check_accounts_payload(home, list(self.gmail_emails))
        if not self._result(InstallStep.GMAIL_LOGIN, health,
                            {"kind": "gmail", "text": "Connect Gmail in your browser",
                             "command": "; ".join(item["authorize_command"] for item in health.get("accounts", [])
                                                  if "authorize_command" in item)}):
            return False
        return True

    def _gmail_sync(self) -> bool:
        current = None if self.refresh else import_common.import_manifest_current("gmail")
        imported_emails = {item["account_email"].lower() for item in current.input.get("accounts", [])} if current else set()
        if current and imported_emails == {email.lower() for email in self.gmail_emails} and self._gmail_covered(current):
            self._write(InstallStep.GMAIL_SYNC, InstallState.COMPLETED, "Gmail is already imported")
            self._write(InstallStep.GMAIL_IMPORT, InstallState.COMPLETED, "Gmail contacts ready")
            return True
        self._write(InstallStep.GMAIL_SYNC, InstallState.RUNNING, "Syncing Gmail")
        result = GmailDiscovery(account_emails=list(self.gmail_emails), sync_after=self.sync_after).run()
        if not self._result(InstallStep.GMAIL_SYNC, result.to_payload()):
            return False
        self._write(InstallStep.GMAIL_IMPORT, InstallState.RUNNING, "Adding Gmail contacts")
        importer = GmailImport()
        importer.run()
        return self._result(InstallStep.GMAIL_IMPORT, importer.written)

    def _imessage_access(self) -> bool:
        self._write(InstallStep.IMESSAGE_ACCESS, InstallState.RUNNING, "Checking Messages access")
        extractor = IMessageExtractor()
        access = extractor.check(strict=True)
        action = {"kind": "permission", "text": "Allow access to Messages",
                  "url": "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"}
        chat = access.get("chat_db", {})
        if access["status"] == "blocked_user_action" and chat.get("exists") and not chat.get("missing_tables"):
            self._write(InstallStep.IMESSAGE_ACCESS, InstallState.WAITING, "Allow access to Messages",
                        {**action, "details": access})
            while (access["status"] == "blocked_user_action" and access["chat_db"].get("exists")
                   and not access["chat_db"].get("missing_tables")):
                time.sleep(_PERMISSION_POLL_SECONDS)
                access = extractor.check(strict=True)
        return self._result(InstallStep.IMESSAGE_ACCESS, access, action)

    def _whatsapp_link(self) -> bool:
        if auth.auth_status(self.wacli_store).authenticated:
            self._write(InstallStep.WHATSAPP_LOGIN, InstallState.RUNNING, "Checking WhatsApp")
        else:
            # auth_report stays alive while wacli refreshes the QR artifact.
            self._write(InstallStep.WHATSAPP_LOGIN, InstallState.WAITING, "Connect WhatsApp",
                        {"kind": "qr", "text": "Scan with WhatsApp → Linked devices",
                         "details": {"store": str(self.wacli_store)}})
        while True:
            try:
                result = auth.auth_report(self.wacli_store, open_qr_page=False)
                break
            except PrimitiveBlocked as blocked:
                if "command timed out after" not in blocked.payload.get("detail", ""):
                    raise
                self._write(InstallStep.WHATSAPP_LOGIN, InstallState.WAITING, "Refreshing your WhatsApp QR code",
                            {"kind": "qr", "text": "Scan with WhatsApp → Linked devices"})
        return self._result(InstallStep.WHATSAPP_LOGIN, result, {"kind": "qr"})

    def _messages_sync(self, source: Source) -> bool:
        imessage = source is Source.IMESSAGE
        sync_step, import_step = ((InstallStep.IMESSAGE_IMPORT, InstallStep.IMESSAGE_IMPORT) if imessage
                                  else (InstallStep.WHATSAPP_SYNC, InstallStep.WHATSAPP_IMPORT))
        current = None if self.refresh else import_common.import_manifest_current("messages")
        contacts = self.root / f".powerpacks/messages/{source.value}.contacts.csv"
        store_matches = True
        if not imessage:
            exported = read_json(contacts.with_name("whatsapp.contacts.csv.manifest.json"), {})
            store_matches = bool(exported.get("store")) and Path(exported["store"]).resolve() == self.wacli_store.resolve()
        if current and contacts.exists() and store_matches:
            self._write(sync_step, InstallState.COMPLETED, "Contacts already imported")
            self._write(import_step, InstallState.COMPLETED, "Contacts ready")
            return True
        self._write(sync_step, InstallState.RUNNING, "Reading Messages" if imessage else WHATSAPP_SYNCING)
        result = MessagesDiscovery(include_imessage=imessage, include_whatsapp=not imessage,
                                   wacli_store=self.wacli_store, open_qr_page=False).run()
        if not self._result(sync_step, result.to_payload()):
            return False
        self._write(import_step, InstallState.RUNNING, "Adding contacts")
        importer = MessagesImport()
        importer.run()
        return self._result(import_step, importer.written)

    def _linkedin_current(self) -> bool:
        record = read_json(self.root / SCRAPE_RECORD, {}) or {}
        return bool(record.get("complete")) and not self.refresh

    def _linkedin_login(self) -> bool:
        if self._linkedin_current():
            self._write(InstallStep.LINKEDIN_LOGIN, InstallState.COMPLETED, "LinkedIn connections ready")
            return True
        self._write(InstallStep.LINKEDIN_LOGIN, InstallState.RUNNING,
                    "Checking LinkedIn. Log in to LinkedIn in the window that opens if it asks.")
        return self._result(InstallStep.LINKEDIN_LOGIN,
                            LinkedInConnections(csv_path=self.root / CONNECTIONS_CSV).login())

    def _linkedin_sync(self) -> bool:
        if self._linkedin_current():
            self._write(InstallStep.LINKEDIN, InstallState.COMPLETED, "LinkedIn connections ready")
            return True
        self._write(InstallStep.LINKEDIN, InstallState.RUNNING, "Reading your LinkedIn connections")
        return self._result(InstallStep.LINKEDIN, LinkedInConnections(csv_path=self.root / CONNECTIONS_CSV).run())

    def run(self) -> dict:
        previous = self.status.read()
        try:
            if Path.cwd() != self.root:
                raise ValueError(f"Run bin/onboard from {self.root}")
            self._write(InstallStep.SOURCES, InstallState.COMPLETED, "Sources selected")
            for source in self.sources:
                for step in _SOURCE_STEPS[source]:
                    if source in self.skip_sources:
                        self._write(step, InstallState.SKIPPED, "Skipped for now")
                    elif previous.get("steps", {}).get(step.value, {}).get("status") == InstallState.SKIPPED:
                        self._write(step, InstallState.WAITING, "Not started")
            if all(source in self.skip_sources for source in self.sources):
                return self._write(InstallStep.READY, InstallState.COMPLETED, "Powerpacks is installed")
            active = [source for source in self.sources if source not in self.skip_sources]
            if Source.GMAIL in active and not self.gmail_emails:
                # Gmail defaults to the account the user signed in to Powerset with.
                account = previous.get("account_email") or ""
                if not account:
                    return self._write(InstallStep.GMAIL_LOGIN, InstallState.WAITING, GMAIL_QUESTION,
                                       {"kind": "gmail", "text": GMAIL_QUESTION}, pid=0)
                self.gmail_emails = (account,)
            # Tools first (Homebrew can take minutes), then every login back to back while
            # the user is here, then the syncs and imports run without them.
            for source in active:
                if source is Source.LINKEDIN and self._linkedin_current():
                    continue
                if source in _TOOL_STEPS and not self._tools(source, _TOOL_STEPS[source]):
                    return self.status.read()
            logins = {Source.LINKEDIN: self._linkedin_login, Source.GMAIL: self._gmail_connect,
                      Source.IMESSAGE: self._imessage_access, Source.WHATSAPP: self._whatsapp_link}
            for source in active:
                if not logins[source]():
                    return self.status.read()
            for source in active:
                synced = (self._linkedin_sync() if source is Source.LINKEDIN else self._gmail_sync()
                          if source is Source.GMAIL else self._messages_sync(source))
                if not synced:
                    return self.status.read()
            imports = [import_common.ImportManifest.read(source) for source in ("gmail", "messages")]
            counts = {item.source: item.stats["people"] for item in imports
                      if item.status == "completed" and "people" in item.stats}
            message = " · ".join(f"{source.title()}: {count:,} contacts" for source, count in counts.items()) or "Sources are ready"
            return self._write(InstallStep.DEEP_CONTEXT, InstallState.WAITING, message,
                               {"kind": "processing", "text": "Ready to build your network",
                                "command": "bin/deep-context check", "details": {"counts": counts}}, pid=0)
        except PrimitiveBlocked as exc:
            action = {"kind": "qr" if self.step is InstallStep.WHATSAPP_LOGIN else "error", "details": exc.payload}
            if exc.payload.get("install_command"):
                action["command"] = exc.payload["install_command"]
            return self._write(self.step, InstallState.WAITING, str(exc), action, pid=0)
        except Exception as exc:
            self.status.directory.mkdir(parents=True, exist_ok=True)
            with self.status.log_path.open("a", encoding="utf-8") as log:
                traceback.print_exc(file=log)
            return self._write(self.step, InstallState.FAILED, str(exc),
                               {"details": {"error_type": type(exc).__name__, "error": str(exc)}})


def _parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    parser.add_argument("--source", choices=[source.value for source in Source], action="append", default=[])
    parser.add_argument("--skip-source", choices=[source.value for source in Source],
                        action="append", default=[], help="Skip a selected source for now")
    parser.add_argument("--gmail-email", action="append", default=[])
    parser.add_argument("--sync-after", default="")
    parser.add_argument("--wacli-store", type=Path)
    parser.add_argument("--refresh", action="store_true", help="Sync selected sources instead of reusing imported contacts")
    return parser

