"""Page actions launch the same detached coordinator and open permission guidance.

Changelog:
  2026-10-24: `POST /api/install/linkedin-forget` moves the LinkedIn sign-in profile, the
      connections file and its record aside (`.bkup-<time>`), so the onboarding page's LinkedIn
      steps can be run again from the sign-in.
  2026-10-13: `POST /api/install/connect` answers once the run has written its own status, so the
      Connect screen never reads the previous run's record after a click.
  2026-10-13: `/api/install/skip` is gone: setup under the desktop app takes only connected sources,
      so it never stops at WhatsApp's QR to be skipped.
  2026-10-13: connect: `POST /api/install/connect` runs one source's tools and login for the
      desktop app's Connect screen (onboard.py --logins-only) in the background, Gmail with every
      mailbox; `GET` reads its record: what the sign-in is doing now, with the QR's URL while
      WhatsApp waits on one. Whether a source is signed in is accounts.signed_in's.
  2026-10-09: preflight: `GET /api/install/preflight` reports what the desktop app's first screen
      checks (a browser for sign-ins, the Google Cloud CLI); `POST` installs one in the background.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse

from packs.ingestion.primitives.discover.linkedin.connections import BROWSER_PROFILE, CONNECTIONS_CSV, SCRAPE_RECORD
from packs.powerset.primitives.install import preflight
from packs.powerset.primitives.install.status import InstallStatus


PERMISSION_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
# The sources the Connect screen signs in to before setup: the rest need no login (Full Disk
# Access is a macOS setting) or none at all.
CONNECTABLE = ("linkedin", "gmail", "whatsapp")



def permission_app() -> str | None:
    launched_by = os.environ.get("POWERPACKS_PERMISSION_APP")
    if launched_by and Path(launched_by).is_dir():
        return launched_by
    pid = os.getpid()
    while pid > 1:
        row = subprocess.check_output(["ps", "-p", str(pid), "-o", "ppid=,comm="], text=True).strip()
        if not row:
            break
        parent, command = row.split(maxsplit=1)
        if ".app/" in command:
            app = command.split(".app/", 1)[0] + ".app"
            if Path(app).exists() and "/Resources/" not in app:
                return app
        pid = int(parent)
    return None


class InstallController:
    def __init__(self, root: Path) -> None:
        self.root = root
        # What the preflight screen installs, by item: running (with its last line), ok, or failed.
        self.installs: dict[str, dict] = {}

    def preflight(self) -> dict:
        checks = preflight.checks()
        return {"browser": {**checks["browser"], "install": self.installs.get("chromium")},
                "gcloud": {**checks["gcloud"], "install": self.installs.get("gcloud")}}

    def install(self, item: object) -> dict:
        if item not in ("chromium", "gcloud"):
            raise ValueError("Unknown preflight item")
        if (self.installs.get(item) or {}).get("status") == "running":
            return {"status": "running"}
        state = self.installs[item] = {"status": "running", "line": ""}

        def run() -> None:
            state.update(preflight.install(item, lambda line: state.update(line=line)))

        threading.Thread(target=run, daemon=True).start()
        return {"status": "running"}

    def qr(self, record: dict) -> Path | None:
        path = self.root / ".powerpacks/messages/wacli-login-qr.png"
        if record["status"] != "waiting" or (record.get("action") or {}).get("kind") != "qr" or not path.is_file():
            return None
        started = datetime.fromisoformat(record["updated_at"].replace("Z", "+00:00")).timestamp()
        return path if path.stat().st_mtime >= started else None

    def qr_image(self, connect: bool = False) -> bytes | None:
        """Setup's QR, or with `connect` the Connect screen's."""
        path = self.qr(InstallStatus(self.root, "connect" if connect else "manifest").read())
        return path.read_bytes() if path else None

    def connection(self) -> dict:
        """The Connect screen's sign-in in progress: its record, with the QR's URL while WhatsApp
        waits on one."""
        record = InstallStatus(self.root, "connect").read()
        qr = self.qr(record)
        if qr:
            record["action"]["qr_url"] = f"/api/install/qr?connect=1&t={qr.stat().st_mtime_ns}"
        return record

    def forget_linkedin(self) -> dict:
        """Move the LinkedIn sign-in and the connections it read aside, so the next connect signs in
        and the next setup reads them again. Nothing is deleted: each path gets a `.bkup-<time>`."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        moved = []
        for path in (BROWSER_PROFILE.expanduser(), self.root / CONNECTIONS_CSV, self.root / SCRAPE_RECORD):
            if path.exists():
                target = path.with_name(f"{path.name}.bkup-{stamp}")
                path.rename(target)
                moved.append(str(target))
        return {"status": "forgotten", "moved": moved}

    def connect(self, source: object, emails: object = None) -> dict:
        """Sign in to one source before setup: its tools and login, in the background like setup
        (one at a time, by setup's own lock), followed by the page in the connect record. Gmail
        takes every mailbox, the app's owner first: an added one reuses the app."""
        if source not in CONNECTABLE:
            raise ValueError("Unknown source")
        command = [sys.executable, "-m", "packs.powerset.primitives.install.onboard", "--root", str(self.root),
                   "--source", str(source), "--logins-only"]
        if source == "gmail":
            if not isinstance(emails, list) or not emails or not all(isinstance(email, str) and "@" in email
                                                                       for email in emails):
                raise ValueError("Which Gmail addresses?")
            for email in dict.fromkeys(email.strip() for email in emails):
                command += ["--gmail-email", email]
        directory = self.root / ".powerpacks/install"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "connect.log").open("a") as log:
            run = subprocess.Popen(command, cwd=self.root, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        # Every write carries the run's pid: answer once the record is this run's (or the run ended).
        record = InstallStatus(self.root, "connect")
        while run.poll() is None and record.read()["installer_pid"] != run.pid:
            time.sleep(0.05)
        return {"status": "started", "source": source}

    @staticmethod
    def _body(handler) -> dict:
        length = int(handler.headers.get("Content-Length") or 0)
        payload = json.loads(handler.rfile.read(length) or b"{}") if length else {}
        if not isinstance(payload, dict):
            raise ValueError("Send a JSON object")
        return payload

    def post(self, handler, path: str) -> bool:
        if not path.startswith("/api/install/"):
            return False
        origin = handler.headers.get("Origin")
        if origin and urlparse(origin).netloc != handler.headers.get("Host"):
            handler._json({"error": "Use the local Powerpacks page"}, HTTPStatus.FORBIDDEN)
            return True
        try:
            response = {"status": "started"}
            if path == "/api/install/permissions":
                # Asked for before setup starts (the desktop app's first screen) or when it waits.
                subprocess.run(["open", PERMISSION_URL], check=True)
                app = permission_app()
                if app:
                    subprocess.run(["open", "-R", app], check=True)
                response["app_path"] = app
            elif path == "/api/install/review":
                subprocess.run(["open", f"http://{handler.headers['Host']}/?stage=linkedin"], check=True)
            elif path == "/api/install/preflight":
                response = self.install(self._body(handler).get("item"))
            elif path == "/api/install/linkedin-forget":
                response = self.forget_linkedin()
            elif path == "/api/install/connect":
                body = self._body(handler)
                response = self.connect(body.get("source"), body.get("emails"))
            else:
                handler._json({"error": "Unknown setup action"}, HTTPStatus.NOT_FOUND)
                return True
            handler._json(response, HTTPStatus.ACCEPTED)
        except (ValueError, TypeError, OSError, subprocess.CalledProcessError) as error:
            handler._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        return True
