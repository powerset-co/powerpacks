"""Errors report themselves: one `POST /v2/feedback` row (the same table Report a problem fills)
per stopped setup step (`report`) or error the page server logs (`report_error`), with the record
or traceback and the log tails, so the team reads what happened without asking the person for
files. Sent as whoever is signed in to Powerset; before the sign-in there is no one to send it as,
and the row is skipped.

What goes up: the manifest without the account email, the traceback, and the last lines of the
logs, with the home folder and every email address blanked. No names, no mail, no connections.

Created: 2026-10-27

Changelog:
- 2026-10-27: `report_error` for the page server's logged errors and request crashes.
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
import platform
import re
import threading
import traceback
from pathlib import Path

from packs.powerset.primitives.send_feedback.send_feedback import Artifact, FeedbackRequest, SendFeedback, default_set_id
from packs.shared.web import account

# The checkout's version, which the report names; a root without one (a test folder) sends nothing.
RELEASE_MANIFEST = Path(".release-please-manifest.json")
LOGS = ("install.log", "connect.log", "desktop-onboard.log", "server.log")
TAIL_LINES = 200
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def scrub(text: str) -> str:
    """The text with the home folder and every email address blanked."""
    return _EMAIL.sub("<email>", text.replace(str(Path.home()), "~"))


def _artifact(name: str, text: str) -> Artifact:
    raw = text.encode("utf-8")
    return Artifact(name=name, source_path=name, raw_bytes=len(raw),
                    data=base64.b64encode(gzip.compress(raw, 9)).decode("ascii"))


def _log_tails(root: Path) -> list[Artifact]:
    artifacts = []
    for name in LOGS:
        path = root / ".powerpacks/install" / name
        if path.is_file():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-TAIL_LINES:]
            artifacts.append(_artifact(name, scrub("\n".join(lines))))
    return artifacts


def _send(root: Path, comment: str, category: str, metadata: dict, artifacts: list[Artifact]) -> dict:
    """One row; the sender's reply, or why nothing was sent."""
    release_path = root / RELEASE_MANIFEST
    if not release_path.is_file():
        return {"status": "skipped", "error": "no release manifest"}
    release = json.loads(release_path.read_text(encoding="utf-8"))["."]
    request = FeedbackRequest(
        comment=scrub(comment), category=category,
        metadata={**metadata, "powerpacks_version": release, "macos": platform.mac_ver()[0]},
        artifacts=tuple(artifacts), set_id=default_set_id(account.config(root)))
    try:
        return SendFeedback(request, env_file=root / ".env").run()
    except SystemExit as exc:  # the body outgrew the wire cap: the error stands either way
        return {"status": "failed", "error": str(exc)}


def report(root: Path, record: dict) -> dict:
    """Send the stopped step's record and log tails."""
    manifest = {key: value for key, value in record.items() if key not in ("account_email", "prose")}
    artifacts = [_artifact("manifest.json", scrub(json.dumps(manifest, indent=1, sort_keys=True))), *_log_tails(root)]
    return _send(root, f"Setup stopped at {record['step']} ({record['event']}): {record['message']}", "setup",
                 {"source": "powerpacks-setup", "step": record["step"], "status": record["status"],
                  "event": record["event"]}, artifacts)


def report_error(root: Path, where: str, detail: str) -> dict:
    """Send an error the page server hit (`where` is its logged line) with its traceback and log tails."""
    artifacts = [_artifact("error.txt", scrub(detail)), *_log_tails(root)]
    return _send(root, f"Page server: {where}", "app", {"source": "powerpacks-server", "where": where}, artifacts)


class ErrorReporter(logging.Handler):
    """A logging handler: every error logged with its traceback reports itself, off the logging
    thread so a request's reply is not held for it."""

    def __init__(self, root: Path) -> None:
        super().__init__(level=logging.ERROR)
        self.root = root

    def emit(self, record: logging.LogRecord) -> None:
        if not record.exc_info:
            return
        detail = "".join(traceback.format_exception(*record.exc_info))
        threading.Thread(target=self._report, args=(record.getMessage(), detail), name="error-report",
                         daemon=True).start()

    def _report(self, where: str, detail: str) -> None:
        try:
            report_error(self.root, where, detail)
        except Exception:  # noqa: BLE001 - the report is best effort; a failed one never logs another error
            pass
