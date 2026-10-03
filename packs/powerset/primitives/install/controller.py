"""The local page starts explicit source imports and opens OS permission guidance."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse

from packs.powerset.primitives.install.status import InstallState, InstallStatus, InstallStep


PERMISSION_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


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
        self.lock = threading.Lock()

    def qr(self, record: dict) -> Path | None:
        path = self.root / ".powerpacks/messages/wacli-login-qr.png"
        if record["status"] != "waiting" or (record.get("action") or {}).get("kind") != "qr" or not path.is_file():
            return None
        started = datetime.fromisoformat(record["updated_at"].replace("Z", "+00:00")).timestamp()
        return path if path.stat().st_mtime >= started else None

    def qr_image(self) -> bytes | None:
        path = self.qr(InstallStatus(self.root).read())
        return path.read_bytes() if path else None

    def start(self, record: dict) -> None:
        sources = record.get("sources", [])
        if not isinstance(sources, list) or any(source not in {"gmail", "imessage", "whatsapp", "linkedin"} for source in sources):
            raise ValueError("Choose Gmail, iMessage, WhatsApp, or LinkedIn")
        if not sources and not record.get("skip"):
            raise ValueError("Choose a source or skip")
        if sources and record.get("skip"):
            raise ValueError("Choose sources or skip, not both")
        emails = record.get("gmail_emails", [])
        sync_after = record.get("sync_after", "")
        if not isinstance(emails, list) or not isinstance(sync_after, str) or any(not isinstance(email, str) for email in emails):
            raise ValueError("Gmail accounts and history must be text")
        if not self.lock.acquire(blocking=False):
            raise ValueError("Setup is already running")
        status = InstallStatus(self.root)
        current = status.read()
        if current["status"] == "running" or (current["status"] == "waiting" and current["installer_pid"] > 0
                                               and current["step"] in {"account", "gmail_login", "imessage_access", "whatsapp_login"}):
            self.lock.release()
            raise ValueError("Setup is already running")

        def run() -> None:
            try:
                from packs.powerset.primitives.install.workflow import SourceOnboarding
                SourceOnboarding(self.root, sources=tuple(sources) if sources else ("skip",),
                                 gmail_emails=tuple(emails), sync_after=sync_after).run()
            except Exception as error:
                status.write(step=InstallStep.SOURCES, status=InstallState.FAILED,
                             message=str(error), pid=os.getpid())
            finally:
                self.lock.release()

        threading.Thread(target=run, daemon=True).start()

    def post(self, handler, path: str) -> bool:
        if not path.startswith("/api/install/"):
            return False
        origin = handler.headers.get("Origin")
        if origin and urlparse(origin).netloc != handler.headers.get("Host"):
            handler._json({"error": "Use the local Powerpacks page"}, HTTPStatus.FORBIDDEN)
            return True
        try:
            response = {"status": "started"}
            if path == "/api/install/sources":
                length = int(handler.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("Source selection is too large")
                record = json.loads(handler.rfile.read(length))
                if not isinstance(record, dict):
                    raise ValueError("Choose your sources")
                self.start(record)
            elif path == "/api/install/permissions":
                if (InstallStatus(self.root).read().get("action") or {}).get("kind") != "permission":
                    raise ValueError("No permission is needed right now")
                subprocess.run(["open", PERMISSION_URL], check=True)
                app = permission_app()
                if app:
                    subprocess.run(["open", "-R", app], check=True)
                response["app_path"] = app
            elif path == "/api/install/linkedin":
                action = InstallStatus(self.root).read().get("action") or {}
                if action.get("kind") != "linkedin":
                    raise ValueError("No LinkedIn export is needed right now")
                subprocess.run(["open", "https://www.linkedin.com/mypreferences/d/download-my-data"], check=True)
            elif path == "/api/install/review":
                if InstallStatus(self.root).read()["step"] != InstallStep.REVIEW:
                    raise ValueError("No review is needed right now")
                subprocess.run(["open", f"http://{handler.headers['Host']}/?stage=linkedin"], check=True)
            else:
                handler._json({"error": "Unknown setup action"}, HTTPStatus.NOT_FOUND)
                return True
            handler._json(response, HTTPStatus.ACCEPTED)
        except (ValueError, TypeError, OSError, subprocess.CalledProcessError) as error:
            handler._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        return True
