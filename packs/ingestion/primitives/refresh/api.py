"""The Scheduled tasks page's JSON routes.

GET  /tasks/api/task       the refresh task: where it's installed and its past runs
POST /tasks/api/install    runner=codex|claude, cadence, time, day, timezone: install or update
POST /tasks/api/uninstall  runner=...: remove that runner's install

Changelog:
  2026-09-28: created for the local Scheduled tasks page.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from packs.ingestion.primitives.refresh.tasks import RUNNERS, Schedule, install, read_task, uninstall

TASK_PATH = "/tasks/api/task"
INSTALL_PATH = "/tasks/api/install"
UNINSTALL_PATH = "/tasks/api/uninstall"


class TasksApi:
    def __init__(self) -> None:
        self.repo = Path.cwd().resolve()

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if parsed.path != TASK_PATH:
            return False
        _send_json(handler, asdict(read_task(self.repo)))
        return True

    def post(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if parsed.path not in (INSTALL_PATH, UNINSTALL_PATH):
            return False
        length = int(handler.headers.get("Content-Length") or 0)
        fields = urllib.parse.parse_qs(handler.rfile.read(length).decode())
        runner = (fields.get("runner") or [""])[0]
        if runner not in RUNNERS:
            _send_json(handler, {"error": f"runner must be one of {', '.join(RUNNERS)}"}, HTTPStatus.BAD_REQUEST)
            return True
        try:
            if parsed.path == INSTALL_PATH:
                schedule = Schedule((fields.get("cadence") or ["daily"])[0], (fields.get("time") or ["06:00"])[0],
                                    (fields.get("day") or ["MO"])[0], (fields.get("timezone") or [""])[0])
                install(runner, self.repo, schedule)
            else:
                uninstall(runner)
        except ValueError as error:
            _send_json(handler, {"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return True
        except Exception as error:  # noqa: BLE001 - the page shows why the install failed
            _send_json(handler, {"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return True
        _send_json(handler, asdict(read_task(self.repo)))
        return True


def _send_json(handler: BaseHTTPRequestHandler, payload: object, status: int = HTTPStatus.OK) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)
