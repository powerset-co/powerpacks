"""Errors report themselves, from one place: `install(root)` puts a handler on the root logger, and
every error logged from then on (a stopped setup step, a request that throws, a background thread
that fails, any `logger.exception`) posts one `POST /v2/feedback` row, the same table Report a
problem fills, with the traceback or record and the log tails, so the team reads what happened
without asking the person for files. Sent as whoever is signed in to Powerset; before the sign-in
there is no one to send it as, and the row is skipped.

What goes up: the error's line, its traceback or record (a setup manifest without the account
email), and the last lines of the logs, with the home folder and every email address blanked.
No names, no mail, no connections.

Created: 2026-10-27
"""
from __future__ import annotations

import atexit
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


def report(root: Path, where: str, detail: str, category: str = "app") -> dict:
    """Send one error: its logged line, its traceback or record, and the log tails; the sender's
    reply, or why nothing was sent."""
    release_path = root / RELEASE_MANIFEST
    if not release_path.is_file():
        return {"status": "skipped", "error": "no release manifest"}
    release = json.loads(release_path.read_text(encoding="utf-8"))["."]
    request = FeedbackRequest(
        comment=scrub(where), category=category,
        metadata={"source": "powerpacks-app", "powerpacks_version": release, "macos": platform.mac_ver()[0]},
        artifacts=(_artifact("error.txt", scrub(detail)), *_log_tails(root)),
        set_id=default_set_id(account.config(root)))
    try:
        return SendFeedback(request, env_file=root / ".env").run()
    except SystemExit as exc:  # the body outgrew the wire cap: the error stands either way
        return {"status": "failed", "error": str(exc)}


class ErrorReporter(logging.Handler):
    """Every error logged reports itself, off the logging thread so a request's reply is not held
    for it. A record logged with `extra={"record": ...}` (a setup stop) sends that record; one
    logged with a traceback sends the traceback."""

    def __init__(self, root: Path) -> None:
        super().__init__(level=logging.ERROR)
        self.root = root
        # The reports in flight: a setup run writes its stop and exits at once, which would
        # kill a daemon thread mid-request, so the process waits for them before it ends.
        self.inflight: list[threading.Thread] = []
        atexit.register(self.drain)

    def emit(self, record: logging.LogRecord) -> None:
        stop = getattr(record, "record", None)
        if stop is not None:
            detail = json.dumps({key: value for key, value in stop.items() if key not in ("account_email", "prose")},
                                indent=1, sort_keys=True)
        elif record.exc_info:
            detail = "".join(traceback.format_exception(*record.exc_info))
        else:
            detail = record.getMessage()
        category = "setup" if stop is not None else "app"
        thread = threading.Thread(target=self._report, args=(record.getMessage(), detail, category),
                                  name="error-report", daemon=True)
        self.inflight.append(thread)
        thread.start()

    def drain(self, timeout: float = 20.0) -> None:
        """Wait for the reports in flight, each for at most `timeout` seconds."""
        for thread in self.inflight:
            thread.join(timeout)
        self.inflight.clear()

    def _report(self, where: str, detail: str, category: str) -> None:
        try:
            report(self.root, where, detail, category)
        except Exception:  # noqa: BLE001 - the report is best effort; a failed one never logs another error
            pass


def install(root: Path) -> None:
    """Report every error this process logs, and every thread that dies, once per process."""
    logger = logging.getLogger()
    if any(isinstance(handler, ErrorReporter) for handler in logger.handlers):
        return
    logger.addHandler(ErrorReporter(root))
    threading.excepthook = lambda args: logging.getLogger(__name__).error(
        "Background thread failed", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
