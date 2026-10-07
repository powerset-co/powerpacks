"""Run selected local imports and show their progress on the install page.

Source manifests own reuse. Gmail authorization, Messages permission checks
and the LinkedIn login continue in this process; missing setup or failed
primitives stop the flow. It never starts enrichment, provider calls, or uploads.

Changelog:
  2026-10-05: every write names a status_prose event; a primitive's own text goes
      to the action's details. Google Cloud stages, the LinkedIn count and the
      WhatsApp download count show on the page as they happen.
  2026-10-05: LinkedIn's login is its own step, and the plan lists every
      login step before every sync step, the order a run takes them. The
      WhatsApp scan starts its history download in the background; the WhatsApp
      sync step waits for it, then fetches older messages and imports.
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
from packs.powerset.primitives.install.controller import permission_app
from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.status_prose import source_counts
from packs.powerset.primitives.install.steps import PROCESSING_STEPS, InstallState, InstallStep
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
        self.refresh = refresh
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

    def _write(self, event: str, *, step: InstallStep | None = None, action: dict | None = None,
               details: dict | None = None, handed_back: bool = False, **values) -> dict:
        """Write `event`. `handed_back`: this run returns and the agent takes over, so no process owns it."""
        record = self.status.write(event, step=step or self.step, pid=0 if handed_back else os.getpid(),
                                   retry_command=self.retry_command, plan=self.plan, action=action,
                                   details=details, **values)
        self.step = InstallStep(record["step"])
        return record

    def _result(self, payload: dict, done: str, *, waiting: str = "step.waiting", failed: str = "step.failed",
                step: InstallStep | None = None, action: dict | None = None, **values) -> bool:
        """Write the event for a primitive's outcome; its own text stays in the details."""
        if payload["status"] in _SUCCESS:
            self._write(done, step=step, **values)
            return True
        waits = payload["status"] in _WAITING
        self._write(waiting if waits else failed, step=step, action=action, details=payload, handed_back=waits, **values)
        return False

    def _tools(self, source: Source, step: InstallStep) -> bool:
        self._write("tools.preparing", step=step)
        payload = ImportTools(sources=(source.value,)).run()
        return self._result(payload, "tools.ready", waiting="tools.needs_password", failed="tools.failed", step=step,
                            action={"command": payload["command"]} if "command" in payload else None)

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
        self._write("gmail.checking")
        home = Path(os.environ.get("MSGVAULT_HOME", "~/.msgvault")).expanduser()
        local = accounts.status_payload(home)
        if not local["config"]["oauth_configured"]:
            self._write("gmail.app.starting")
            created = BrowserSetup.from_args(msgvault_parser().parse_args(
                ["browser-setup", "--home", str(home), "--email", self.gmail_emails[0], "--no-install-mcp"]),
                on_stage=self._gmail_stage).run()
            # A stop in Google Cloud is the agent's to fix; the user is not handed its steps.
            if not self._result(created, "gmail.app.ready", waiting="gmail.app.stopped", failed="gmail.app.failed"):
                return False
            local = accounts.status_payload(home)
        if local["database"]["exists"]:
            health = accounts.check_accounts_payload(home, list(self.gmail_emails))
            if health["status"] == "error":
                return self._result(health, "gmail.connected", failed="gmail.connect.failed")
            checks = health.get("accounts", [])
        else:
            checks = [accounts.check_account(home, email, stored=False).record()
                      for email in accounts.normalize_email_list(list(self.gmail_emails))]
            health = local
        authorize = [check for check in checks if check["status"] in {"missing_token", "reauthorization_required"}]
        # An authenticated mailbox has already consented. Add test users only
        # for mailboxes that need consent, before opening their OAuth window.
        if authorize:
            allowed = set(accounts.normalize_email_list(list(msgvault_home.load_setup_state(home, "").test_users)))
            missing = [check["email"] for check in authorize if check["email"] not in allowed]
            if missing:
                self._write("gmail.allowing")
                added = TestUsers.from_args(msgvault_parser().parse_args(
                    ["add-test-users", "--home", str(home), "--login-email", self.gmail_emails[0], *missing]),
                    on_stage=self._gmail_stage).run()
                if not self._result(added, "gmail.allowed", waiting="gmail.app.stopped", failed="gmail.allowing.failed"):
                    return False
        for check in authorize:
            self._write("gmail.connect", email=check["email"], details=check)
            result = accounts.add_account(home, check["email"], "", headless=False,
                                          force=check["status"] == "reauthorization_required",
                                          on_progress=lambda _: self._write("gmail.connect.waiting", email=check["email"], details=check))
            if not self._result(result, "gmail.connected", waiting="gmail.connect.waiting", failed="gmail.connect.failed"):
                return False
        if authorize:
            health = accounts.check_accounts_payload(home, list(self.gmail_emails))
        return self._result(health, "gmail.connected", waiting="gmail.connect.waiting", failed="gmail.connect.failed",
                            action={"command": "; ".join(item["authorize_command"] for item in health.get("accounts", [])
                                                         if "authorize_command" in item)})

    def _gmail_stage(self, stage: str) -> None:
        """A stage the Google Cloud automation reached, as its page line."""
        self._write(f"gmail.app.{stage}")

    def _gmail_sync(self) -> bool:
        current = None if self.refresh else import_common.import_manifest_current("gmail")
        imported_emails = {item["account_email"].lower() for item in current.input.get("accounts", [])} if current else set()
        if current and imported_emails == {email.lower() for email in self.gmail_emails} and self._gmail_covered(current):
            self._write("gmail.sync.current")
            self._write("gmail.import.current")
            return True
        self._write("gmail.syncing")
        result = GmailDiscovery(account_emails=list(self.gmail_emails), sync_after=self.sync_after).run()
        if not self._result(result.to_payload(), "gmail.synced", waiting="gmail.sync.waiting", failed="gmail.sync.failed"):
            return False
        self._write("gmail.importing")
        importer = GmailImport()
        importer.run()
        return self._result(importer.written, "gmail.imported", failed="gmail.import.failed")

    def _imessage_access(self) -> bool:
        self._write("imessage.checking")
        extractor = IMessageExtractor()
        access = extractor.check(strict=True)
        chat = access.get("chat_db", {})
        settings = {"url": "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"}
        app = Path(permission_app() or "").stem or "the app running this session"
        if access["status"] == "blocked_user_action" and chat.get("exists") and not chat.get("missing_tables"):
            self._write("imessage.permission", app=app, details=access, action=settings)
            while (access["status"] == "blocked_user_action" and access["chat_db"].get("exists")
                   and not access["chat_db"].get("missing_tables")):
                time.sleep(_PERMISSION_POLL_SECONDS)
                access = extractor.check(strict=True)
        return self._result(access, "imessage.allowed", waiting="imessage.permission", failed="imessage.unavailable",
                            action=settings, app=app)

    def _whatsapp_link(self) -> bool:
        linked = auth.auth_status(self.wacli_store).authenticated
        # auth_report stays alive while wacli refreshes the QR artifact.
        if linked:
            self._write("whatsapp.checking")
        else:
            self._write("whatsapp.qr", details={"store": str(self.wacli_store)})
        while True:
            try:
                result = auth.auth_report(self.wacli_store, open_qr_page=False)
                break
            except PrimitiveBlocked as blocked:
                if "command timed out after" not in blocked.payload.get("detail", ""):
                    raise
                self._write("whatsapp.qr.refreshed")
        return self._result(result, "whatsapp.already_linked" if linked else "whatsapp.linked",
                            waiting="whatsapp.blocked", failed="whatsapp.failed")

    def _messages_sync(self, source: Source) -> bool:
        imessage = source is Source.IMESSAGE
        current = None if self.refresh else import_common.import_manifest_current("messages")
        contacts = self.root / f".powerpacks/messages/{source.value}.contacts.csv"
        store_matches = True
        if not imessage:
            exported = read_json(contacts.with_name("whatsapp.contacts.csv.manifest.json"), {})
            store_matches = bool(exported.get("store")) and Path(exported["store"]).resolve() == self.wacli_store.resolve()
        if current and contacts.exists() and store_matches:
            self._write("imessage.current" if imessage else "whatsapp.sync.current")
            if not imessage:
                self._write("whatsapp.import.current")
            return True
        if imessage:
            self._write("imessage.reading")
        else:
            self._write("whatsapp.downloading")
            auth.wait_for_history(self.wacli_store, on_count=lambda messages: self._write(
                "whatsapp.downloading.count", messages=messages))
            self._write("whatsapp.syncing")
        result = MessagesDiscovery(include_imessage=imessage, include_whatsapp=not imessage,
                                   wacli_store=self.wacli_store, open_qr_page=False).run()
        if imessage and not self._result(result.to_payload(), "imessage.importing", failed="imessage.read.failed"):
            return False
        if not imessage:
            if not self._result(result.to_payload(), "whatsapp.synced", waiting="whatsapp.sync.waiting",
                                failed="whatsapp.sync.failed"):
                return False
            self._write("whatsapp.importing")
        importer = MessagesImport()
        importer.run()
        if imessage:
            return self._result(importer.written, "imessage.imported", failed="imessage.import.failed")
        return self._result(importer.written, "whatsapp.imported", failed="whatsapp.import.failed")

    def _linkedin_current(self) -> bool:
        record = read_json(self.root / SCRAPE_RECORD, {}) or {}
        return bool(record.get("complete")) and (self.root / CONNECTIONS_CSV).is_file() and not self.refresh

    def _linkedin_login(self) -> bool:
        self._write("linkedin.login.checking")
        return self._result(LinkedInConnections(csv_path=self.root / CONNECTIONS_CSV).login(), "linkedin.login.done",
                            waiting="linkedin.login.waiting", failed="linkedin.login.failed")

    def _linkedin_sync(self) -> bool:
        if self._linkedin_current():
            self._write("linkedin.sync.current")
            return True
        self._write("linkedin.reading")
        result = LinkedInConnections(csv_path=self.root / CONNECTIONS_CSV).run(
            on_count=lambda read, total: self._write("linkedin.reading.count", read=read, total=total))
        return self._result(result, f"linkedin.done.{result.get('outcome')}", waiting="linkedin.waiting",
                            failed="linkedin.failed",
                            **{key: result[key] for key in ("connections", "added", "read", "total") if key in result})

    def run(self) -> dict:
        previous = self.status.read()
        try:
            if Path.cwd() != self.root:
                raise ValueError(f"Run bin/onboard from {self.root}")
            self._write("sources.selected")
            for source in self.sources:
                for step in _SOURCE_STEPS[source]:
                    if source in self.skip_sources:
                        self._write("source.skipped", step=step)
                    elif previous.get("steps", {}).get(step.value, {}).get("status") == InstallState.SKIPPED:
                        self._write("source.not_started", step=step)
            if all(source in self.skip_sources for source in self.sources):
                return self._write("sources.all_skipped")
            active = [source for source in self.sources if source not in self.skip_sources]
            if Source.GMAIL in active and not self.gmail_emails:
                account = previous.get("account_email") or ""
                if not account:
                    return self._write("gmail.which_accounts", handed_back=True)
                self.gmail_emails = (account,)
                self.retry_command = shlex.join([*shlex.split(self.retry_command), "--gmail-email", account])
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
            return self._write("sources.ready", counts=source_counts(counts), action={"command": "bin/deep-context check"},
                               details={"counts": counts}, handed_back=True)
        except PrimitiveBlocked as exc:
            return self._write("step.waiting", details=exc.payload, handed_back=True,
                               action={"command": exc.payload["install_command"]} if exc.payload.get("install_command") else None)
        except Exception as exc:
            self.status.directory.mkdir(parents=True, exist_ok=True)
            with self.status.log_path.open("a", encoding="utf-8") as log:
                traceback.print_exc(file=log)
            return self._write("step.failed", details={"error_type": type(exc).__name__, "error": str(exc)})


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
