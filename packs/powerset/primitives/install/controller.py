"""Page actions launch the same detached coordinator and open permission guidance.

Changelog:
  2026-10-09: `/api/install/skip` skips a stopped source (WhatsApp at its QR) for the rest of the
      setup: the waiting run is stopped, the saved command gets `--skip-source`, and the page
      resumes setup.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse

from packs.powerset.primitives.install.status import InstallStatus
from packs.powerset.primitives.install.steps import InstallStep


PERMISSION_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
# The sources the page may skip at a stopped step, and the steps each one stops at.
SKIPPABLE = {"whatsapp": {InstallStep.WHATSAPP_TOOLS.value, InstallStep.WHATSAPP_LOGIN.value,
                          InstallStep.WHATSAPP_SYNC.value, InstallStep.WHATSAPP_IMPORT.value}}
_STOP_SECONDS = 5.0


def _stop(pid: int) -> None:
    """Stop the setup process that owns the wait and let its lock go before a new run takes it."""
    if pid <= 0 or pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + _STOP_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise OSError("Setup did not stop; try again")


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

    def qr(self, record: dict) -> Path | None:
        path = self.root / ".powerpacks/messages/wacli-login-qr.png"
        if record["status"] != "waiting" or (record.get("action") or {}).get("kind") != "qr" or not path.is_file():
            return None
        started = datetime.fromisoformat(record["updated_at"].replace("Z", "+00:00")).timestamp()
        return path if path.stat().st_mtime >= started else None

    def qr_image(self) -> bytes | None:
        path = self.qr(InstallStatus(self.root).read())
        return path.read_bytes() if path else None

    @staticmethod
    def _body(handler) -> dict:
        length = int(handler.headers.get("Content-Length") or 0)
        payload = json.loads(handler.rfile.read(length) or b"{}") if length else {}
        if not isinstance(payload, dict):
            raise ValueError("Send a JSON object")
        return payload

    def skip(self, source: object) -> dict:
        """Skip `source` for the rest of this setup, from the step it stopped at: the run that owns the
        wait is stopped, the saved command remembers the skip, and setup waits to be resumed."""
        steps = SKIPPABLE.get(source) if isinstance(source, str) else None
        if steps is None:
            raise ValueError("This source cannot be skipped")
        status = InstallStatus(self.root)
        record = status.read()
        if record["step"] not in steps or record["status"] not in ("waiting", "failed"):
            raise ValueError(f"Setup is not stopped at {source}")
        _stop(int(record["installer_pid"]))
        command = shlex.split(record["retry_command"])
        skipped = {value for flag, value in zip(command, command[1:]) if flag == "--skip-source"}
        if source not in skipped:
            command.extend(("--skip-source", source))
        status.write("setup.paused", pid=0, retry_command=shlex.join(command))
        return {"status": "skipped", "source": source}

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
            elif path == "/api/install/skip":
                response = self.skip(self._body(handler).get("source"))
            else:
                handler._json({"error": "Unknown setup action"}, HTTPStatus.NOT_FOUND)
                return True
            handler._json(response, HTTPStatus.ACCEPTED)
        except (ValueError, TypeError, OSError, subprocess.CalledProcessError) as error:
            handler._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        return True
