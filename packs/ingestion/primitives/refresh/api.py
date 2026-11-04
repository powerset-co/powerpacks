"""Scheduled tasks' JSON routes (the native daily refresh, daily.py).

GET  /tasks/api/refresh            settings, next run, the running run and past runs
POST /tasks/api/refresh/settings   enabled=true|false, time=HH:MM
POST /tasks/api/refresh/run        start a refresh now (the running one when there is one)

Changelog:
  2026-11-03: the native daily refresh replaces the Codex/Claude install routes; tasks.py keeps
    their code for the CLI.
  2026-09-28: created for the local Scheduled tasks page.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from packs.ingestion.primitives.refresh import daily

REFRESH_PATH = "/tasks/api/refresh"
SETTINGS_PATH = "/tasks/api/refresh/settings"
RUN_PATH = "/tasks/api/refresh/run"


def refresh_payload(root: Path) -> dict:
    settings = daily.read_settings(root)
    current = daily.running(root)
    runs = daily.list_runs(root)
    last = daily._parse(runs[0].started_at) if runs else None
    upcoming = daily.next_run_at(daily.now(), settings, last)
    return {
        "settings": {"enabled": settings.enabled, "time": settings.time, "label": settings.label()},
        "next_run_at": upcoming.isoformat(timespec="seconds") if upcoming and not current else None,
        "running": asdict(current) if current else None,
        "runs": [asdict(run) for run in runs],
    }


class TasksApi:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path.cwd()).resolve()

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if parsed.path != REFRESH_PATH:
            return False
        _send_json(handler, refresh_payload(self.root))
        return True

    def post(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if parsed.path not in (SETTINGS_PATH, RUN_PATH):
            return False
        length = int(handler.headers.get("Content-Length") or 0)
        fields = {key: values[0] for key, values in urllib.parse.parse_qs(handler.rfile.read(length).decode()).items()}
        try:
            if parsed.path == SETTINGS_PATH:
                current = daily.read_settings(self.root)
                enabled = fields.get("enabled", str(current.enabled).lower()) == "true"
                daily.write_settings(self.root, enabled, fields.get("time") or current.time)
            else:
                daily.start(self.root, "manual")
        except (ValueError, RuntimeError) as error:
            _send_json(handler, {"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return True
        except Exception as error:  # noqa: BLE001 - the page shows why
            _send_json(handler, {"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return True
        _send_json(handler, refresh_payload(self.root))
        return True


def _send_json(handler: BaseHTTPRequestHandler, payload: object, status: int = HTTPStatus.OK) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)
