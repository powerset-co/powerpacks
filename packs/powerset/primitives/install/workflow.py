"""Run selected local imports and show their progress on the install page.

Source manifests own reuse. Gmail authorization and Messages permission checks
continue in this process; missing setup or failed primitives stop the flow.
It never starts enrichment, provider calls, or uploads.
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

from packs.ingestion.primitives.common.jsonio import emit, read_json
from packs.ingestion.primitives.discover.gmail.discover import GmailDiscovery
from packs.ingestion.primitives.discover.gmail.msgvault.sync import parse_msgvault_sync_date
from packs.ingestion.primitives.discover.messages.discover import MessagesDiscovery
from packs.ingestion.primitives.discover.messages.extract_imessage import IMessageExtractor
from packs.ingestion.primitives.discover.messages.wacli import auth
from packs.ingestion.primitives.discover.messages.wacli.paths import DEFAULT_STORE
from packs.ingestion.primitives.imports import common as import_common
from packs.ingestion.primitives.imports.gmail.importer import GmailImport
from packs.ingestion.primitives.imports.messages.importer import MessagesImport
from packs.ingestion.primitives.setup.automations import accounts
from packs.powerset.primitives.install.status import PROCESSING_STEPS, InstallState, InstallStatus, InstallStep
from packs.powerset.primitives.install.tools import ImportTools


class Source(str, Enum):
    GMAIL = "gmail"
    IMESSAGE = "imessage"
    WHATSAPP = "whatsapp"
    LINKEDIN = "linkedin"
    SKIP = "skip"


_SOURCE_STEPS = {
    Source.GMAIL: (InstallStep.GMAIL_TOOLS, InstallStep.GMAIL_LOGIN,
                   InstallStep.GMAIL_SYNC, InstallStep.GMAIL_IMPORT),
    Source.IMESSAGE: (InstallStep.IMESSAGE_ACCESS, InstallStep.IMESSAGE_IMPORT),
    Source.WHATSAPP: (InstallStep.WHATSAPP_TOOLS, InstallStep.WHATSAPP_LOGIN,
                      InstallStep.WHATSAPP_SYNC, InstallStep.WHATSAPP_IMPORT),
    Source.LINKEDIN: (InstallStep.LINKEDIN,),
    Source.SKIP: (),
}
_SUCCESS = {"ok", "completed", "linked", "skipped"}
_WAITING = {"needs_user_action", "blocked_user_action", "needs_approval"}
_DEFAULT_SOURCES = (Source.GMAIL, Source.IMESSAGE, Source.WHATSAPP)
_PERMISSION_WAIT_SECONDS = 900
_PERMISSION_POLL_SECONDS = 2


class SourceOnboarding:
    def __init__(self, root: Path, *, sources: tuple[str, ...],
                 gmail_emails: tuple[str, ...] = (), sync_after: str = "",
                 wacli_store: Path | None = None, refresh: bool = False,
                 skip_sources: tuple[str, ...] = ()) -> None:
        self.root = root.resolve()
        self.status = InstallStatus(self.root)
        previous = self.status.read()
        command = shlex.split(previous["retry_command"])
        saved = _parser().parse_args(command[1:] if Path(command[0]).name == "onboard" else [])
        self.sources = tuple(sorted(dict.fromkeys(Source(source) for source in
                                    (sources or saved.source or _DEFAULT_SOURCES)),
                                    key=lambda source: source is Source.LINKEDIN))
        if Source.SKIP in self.sources and len(self.sources) != 1:
            raise ValueError("Choose sources or skip, not both")
        skip_sources = skip_sources if sources else (*saved.skip_source, *skip_sources)
        self.skip_sources = tuple(dict.fromkeys(Source(source) for source in skip_sources))
        if Source.SKIP in self.skip_sources or not set(self.skip_sources) <= set(self.sources):
            raise ValueError("Skip only a selected source")
        self.gmail_emails = gmail_emails or tuple(saved.gmail_email)
        if not self.gmail_emails and Source.GMAIL in self.sources and Source.GMAIL not in self.skip_sources:
            if previous.get("account_email"):
                self.gmail_emails = (previous["account_email"],)
            else:
                home = Path(os.environ.get("MSGVAULT_HOME", "~/.msgvault")).expanduser()
                configured = accounts.VaultHealth.from_status(accounts.status_payload(home)).stored_emails - {""}
                if len(configured) == 1:
                    self.gmail_emails = tuple(configured)
        self.sync_after = sync_after or saved.sync_after or (
            (date.today() - timedelta(days=365)).isoformat() if Source.GMAIL in self.sources else "")
        wacli_store = wacli_store or saved.wacli_store
        self.wacli_store = wacli_store or self.root / DEFAULT_STORE
        self.refresh = refresh or (not sources and saved.refresh)
        self.step = InstallStep.SOURCES
        history = [step for step in previous.get("plan", []) if step not in PROCESSING_STEPS
                   and previous.get("steps", {}).get(step, {}).get("status") in {"completed", "skipped"}]
        if self.sources == (Source.SKIP,) or all(source in self.skip_sources for source in self.sources):
            next_steps = (InstallStep.READY,)
        else:
            next_steps = tuple(step for step in PROCESSING_STEPS if step is not InstallStep.REVIEW)
        self.plan = list(dict.fromkeys([
            *history, InstallStep.SOURCES.value,
            *(step.value for source in self.sources for step in _SOURCE_STEPS[source]),
            *(step.value for step in next_steps)]))
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

    def _gmail(self) -> bool:
        if not self.gmail_emails:
            self._write(InstallStep.GMAIL_LOGIN, InstallState.WAITING,
                        "Which Gmail account should I use?",
                        {"kind": "gmail", "text": "Which Gmail account should I use?"}, pid=0)
            return False
        if not self._tools(Source.GMAIL, InstallStep.GMAIL_TOOLS):
            return False
        self._write(InstallStep.GMAIL_LOGIN, InstallState.RUNNING, "Checking Gmail access")
        home = Path(os.environ.get("MSGVAULT_HOME", "~/.msgvault")).expanduser()
        local = accounts.status_payload(home)
        oauth = "uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py"
        if not local["config"]["oauth_configured"]:
            self._write(InstallStep.GMAIL_LOGIN, InstallState.WAITING, "Connect Gmail",
                        {"kind": "gmail", "text": "Set up Gmail access in your browser",
                         "command": f"{oauth} browser-setup --email {shlex.quote(self.gmail_emails[0])} --add-account",
                         "details": local}, pid=0)
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
            result = accounts.add_account(home, check["email"], "", headless=False,
                                          force=check["status"] == "reauthorization_required")
            if not self._result(InstallStep.GMAIL_LOGIN, result, action):
                return False
        if authorize:
            health = accounts.check_accounts_payload(home, list(self.gmail_emails))
        if not self._result(InstallStep.GMAIL_LOGIN, health,
                            {"kind": "gmail", "text": "Connect Gmail in your browser",
                             "command": "; ".join(item["authorize_command"] for item in health.get("accounts", [])
                                                  if "authorize_command" in item)}):
            return False
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

    def _messages(self, source: Source) -> bool:
        imessage = source is Source.IMESSAGE
        if imessage:
            self._write(InstallStep.IMESSAGE_ACCESS, InstallState.RUNNING, "Checking Messages access")
            extractor = IMessageExtractor()
            access = extractor.check(strict=True)
            action = {"kind": "permission", "text": "Allow access to Messages",
                      "url": "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"}
            chat = access.get("chat_db", {})
            if access["status"] == "blocked_user_action" and chat.get("exists") and not chat.get("missing_tables"):
                self._write(InstallStep.IMESSAGE_ACCESS, InstallState.WAITING, "Allow access to Messages",
                            {**action, "details": access})
                deadline = time.monotonic() + _PERMISSION_WAIT_SECONDS
                while access["status"] == "blocked_user_action" and time.monotonic() < deadline:
                    time.sleep(_PERMISSION_POLL_SECONDS)
                    access = extractor.check(strict=True)
            if not self._result(InstallStep.IMESSAGE_ACCESS, access, action):
                return False
            sync_step = import_step = InstallStep.IMESSAGE_IMPORT
        else:
            if not self._tools(source, InstallStep.WHATSAPP_TOOLS):
                return False
            if auth.auth_status(self.wacli_store).authenticated:
                self._write(InstallStep.WHATSAPP_LOGIN, InstallState.RUNNING, "Checking WhatsApp")
            else:
                # auth_report stays alive while wacli refreshes the QR artifact.
                self._write(InstallStep.WHATSAPP_LOGIN, InstallState.WAITING, "Connect WhatsApp",
                            {"kind": "qr", "text": "Scan with WhatsApp → Linked devices",
                             "details": {"store": str(self.wacli_store)}})
            if not self._result(InstallStep.WHATSAPP_LOGIN,
                                auth.auth_report(self.wacli_store, open_qr_page=False), {"kind": "qr"}):
                return False
            sync_step, import_step = InstallStep.WHATSAPP_SYNC, InstallStep.WHATSAPP_IMPORT
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
        self._write(sync_step, InstallState.RUNNING, "Reading Messages" if imessage else "Syncing WhatsApp")
        result = MessagesDiscovery(include_imessage=imessage, include_whatsapp=not imessage,
                                   wacli_store=self.wacli_store, open_qr_page=False).run()
        if not self._result(sync_step, result.to_payload()):
            return False
        self._write(import_step, InstallState.RUNNING, "Adding contacts")
        importer = MessagesImport()
        importer.run()
        return self._result(import_step, importer.written)

    def run(self) -> dict:
        previous = self.status.read()
        active = previous["status"] == InstallState.RUNNING or (
            previous["status"] == InstallState.WAITING
            and previous["installer_pid"] > 0
            and previous["step"] in {InstallStep.ACCOUNT, InstallStep.GMAIL_LOGIN,
                                     InstallStep.IMESSAGE_ACCESS, InstallStep.WHATSAPP_LOGIN})
        if active and previous["installer_pid"] != os.getpid():
            return previous
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
            if self.sources == (Source.SKIP,) or all(source in self.skip_sources for source in self.sources):
                return self._write(InstallStep.READY, InstallState.COMPLETED, "Powerpacks is installed")
            for source in self.sources:
                if source in self.skip_sources:
                    continue
                if source is Source.GMAIL and not self._gmail():
                    return self.status.read()
                if source in {Source.IMESSAGE, Source.WHATSAPP} and not self._messages(source):
                    return self.status.read()
                if source is Source.LINKEDIN:
                    csv = self.root / ".powerpacks/network-import/discover/linkedin/Connections.csv"
                    if not csv.is_file():
                        return self._write(InstallStep.LINKEDIN, InstallState.WAITING, "Waiting for your LinkedIn export",
                                           {"kind": "linkedin", "text": "Request your connections, then send me the CSV in chat.",
                                            "url": "https://www.linkedin.com/mypreferences/d/download-my-data"}, pid=0)
                    self._write(InstallStep.LINKEDIN, InstallState.COMPLETED, "LinkedIn export ready")
            imports = [import_common.ImportManifest.read(source) for source in ("gmail", "messages")]
            counts = {item.source: item.stats["people"] for item in imports
                      if item.status == "completed" and "people" in item.stats}
            message = " · ".join(f"{source.title()}: {count:,} contacts" for source, count in counts.items()) or "Sources are ready"
            return self._write(InstallStep.DEEP_CONTEXT, InstallState.WAITING, message,
                               {"kind": "processing", "text": "Ready to build your network",
                                "command": "bin/deep-context check", "details": {"counts": counts}}, pid=0)
        except Exception as exc:
            self.status.directory.mkdir(parents=True, exist_ok=True)
            with self.status.log_path.open("a", encoding="utf-8") as log:
                traceback.print_exc(file=log)
            return self._write(self.step, InstallState.FAILED, str(exc),
                               {"details": {"error_type": type(exc).__name__, "error": str(exc)}})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=[source.value for source in Source], action="append", default=[])
    parser.add_argument("--skip-source", choices=[source.value for source in Source if source is not Source.SKIP],
                        action="append", default=[], help="Skip a selected source for now")
    parser.add_argument("--gmail-email", action="append", default=[])
    parser.add_argument("--sync-after", default="")
    parser.add_argument("--wacli-store", type=Path)
    parser.add_argument("--refresh", action="store_true", help="Sync selected sources instead of reusing imported contacts")
    return parser


def main() -> None:
    args = _parser().parse_args()
    payload = SourceOnboarding(Path.cwd(), sources=tuple(args.source),
                               gmail_emails=tuple(args.gmail_email), sync_after=args.sync_after,
                               wacli_store=args.wacli_store, refresh=args.refresh,
                               skip_sources=tuple(args.skip_source)).run()
    emit(payload)
    raise SystemExit({"completed": 0, "waiting": 10, "running": 10, "failed": 1}[payload["status"]])


if __name__ == "__main__":
    main()
