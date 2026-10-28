"""A stopped setup step reports itself: one `POST /v2/feedback` row (the same table Report a
problem fills) with the step's record and the run's log tails, so the team reads what happened
without asking the person for files. Sent as whoever is signed in to Powerset; before the sign-in
there is no one to send it as, and the row is skipped.

What goes up: the manifest without the account email, and the last lines of the run's logs, with
the home folder and every email address blanked. No names, no mail, no connections.

Created: 2026-10-27
"""
from __future__ import annotations

import base64
import gzip
import json
import platform
import re
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


def report(root: Path, record: dict) -> dict:
    """Send the stopped step's record and log tails; the sender's reply, or why nothing was sent."""
    release_path = root / RELEASE_MANIFEST
    if not release_path.is_file():
        return {"status": "skipped", "error": "no release manifest"}
    release = json.loads(release_path.read_text(encoding="utf-8"))["."]
    manifest = {key: value for key, value in record.items() if key not in ("account_email", "prose")}
    artifacts = [_artifact("manifest.json", scrub(json.dumps(manifest, indent=1, sort_keys=True)))]
    for name in LOGS:
        path = root / ".powerpacks/install" / name
        if path.is_file():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-TAIL_LINES:]
            artifacts.append(_artifact(name, scrub("\n".join(lines))))
    request = FeedbackRequest(
        comment=scrub(f"Setup stopped at {record['step']} ({record['event']}): {record['message']}"),
        category="setup",
        metadata={"source": "powerpacks-setup", "step": record["step"], "status": record["status"],
                  "event": record["event"], "powerpacks_version": release, "macos": platform.mac_ver()[0]},
        artifacts=tuple(artifacts), set_id=default_set_id(account.config(root)))
    try:
        return SendFeedback(request, env_file=root / ".env").run()
    except SystemExit as exc:  # the body outgrew the wire cap: the run is stopped either way
        return {"status": "failed", "error": str(exc)}
