"""Page actions launch the same detached coordinator and open permission guidance."""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse

from packs.powerset.primitives.install.status import InstallStatus


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

    def qr(self, record: dict) -> Path | None:
        path = self.root / ".powerpacks/messages/wacli-login-qr.png"
        if record["status"] != "waiting" or (record.get("action") or {}).get("kind") != "qr" or not path.is_file():
            return None
        started = datetime.fromisoformat(record["updated_at"].replace("Z", "+00:00")).timestamp()
        return path if path.stat().st_mtime >= started else None

    def qr_image(self) -> bytes | None:
        path = self.qr(InstallStatus(self.root).read())
        return path.read_bytes() if path else None

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
            else:
                handler._json({"error": "Unknown setup action"}, HTTPStatus.NOT_FOUND)
                return True
            handler._json(response, HTTPStatus.ACCEPTED)
        except (ValueError, TypeError, OSError, subprocess.CalledProcessError) as error:
            handler._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        return True
