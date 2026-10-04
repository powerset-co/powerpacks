"""Read the user's LinkedIn connections in Chrome into `discover/linkedin/Connections.csv`.

Flow: open the saved Chrome profile headless on LinkedIn's connections page (a
logs in there once; the profile keeps the session) -> scroll the newest-first
list -> put the new connections on top of the existing rows -> write the CSV in
LinkedIn's export columns, which the Modal `import-linkedin` step reads.

One run makes at most LOADS_PER_RUN scroll requests, 10 people each, with a
random pause between them. A run stops once KNOWN_OVERLAP already-known
connections have loaded. `manifest.json` beside the CSV records whether the
whole list has been read; until it has, each run goes LOADS_PER_RUN further down
than the last. A CSV with no manifest is a LinkedIn export (or an earlier
`$setup` copy of one): complete as of its date, so only newer people are read.

Changelog:
  2026-10-03: created. Replaces waiting on LinkedIn's emailed export.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import emit, now_iso, read_json, write_json
from packs.ingestion.primitives.setup.automations.oauth_browser import ensure_playwright_core
from packs.ingestion.primitives.setup.automations.shell import (
    command_error,
    parse_json_fragment,
    run_streaming_command,
)
from packs.shared.csv_io import CsvIO

CONNECTIONS_CSV = Path(".powerpacks/network-import/discover/linkedin/Connections.csv")
BROWSER_PROFILE = Path("~/.powerpacks/browser-profiles/linkedin")
BROWSER_SCRIPT = Path(__file__).with_name("connections_browser.js")
EXPORT_COLUMNS = ["First Name", "Last Name", "URL", "Email Address", "Company", "Position", "Connected On"]
PROFILE_URL = "https://www.linkedin.com/in/{slug}"
LOGIN_TIMEOUT_SECONDS = 900
SCROLL_TIMEOUT_SECONDS = 1800
LOADS_PER_RUN = 300
KNOWN_OVERLAP = 25


def _slug(url: str) -> str:
    return url.rstrip("/").rsplit("/in/", 1)[-1]


def _read_export(path: Path) -> list[dict[str, str]]:
    """Rows of an existing export; LinkedIn's own file opens with a Notes preamble."""
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("First Name,"))
    return list(CsvIO.dict_reader(lines[start:]))


def _export_row(card: dict[str, str]) -> dict[str, str]:
    first, _, last = card["name"].partition(" ")
    return {
        "First Name": first,
        "Last Name": last,
        "URL": PROFILE_URL.format(slug=card["slug"]),
        "Email Address": "",
        "Company": "",
        "Position": card["headline"],
        "Connected On": card["connected_on"],
    }


class LinkedInConnections:
    """Scrape new connections and keep the CSV newest-first."""

    def __init__(self, *, csv_path: Path = CONNECTIONS_CSV, profile_dir: Path = BROWSER_PROFILE,
                 login_timeout_seconds: int = LOGIN_TIMEOUT_SECONDS) -> None:
        self.csv_path = csv_path
        self.profile_dir = profile_dir.expanduser()
        self.login_timeout_seconds = login_timeout_seconds
        self.manifest_path = csv_path.with_name("manifest.json")

    def run(self) -> dict[str, Any]:
        existing = _read_export(self.csv_path)
        known = {_slug(row["URL"]) for row in existing}
        previous = read_json(self.manifest_path, {}) or {}
        backfill = previous.get("complete") is False
        max_loads = previous.get("loads", 0) + LOADS_PER_RUN if backfill else LOADS_PER_RUN
        deps = ensure_playwright_core()
        if deps["status"] != "ok":
            return {"status": "failed", "message": deps["message"]}

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(sorted(known), handle)
        try:
            result = run_streaming_command(
                ["node", str(BROWSER_SCRIPT), "--profile-dir", str(self.profile_dir),
                 "--known-file", handle.name, "--stop-after-known", "0" if backfill else str(KNOWN_OVERLAP),
                 "--max-loads", str(max_loads), "--timeout-seconds", str(self.login_timeout_seconds)],
                timeout=self.login_timeout_seconds + SCROLL_TIMEOUT_SECONDS,
                env={**os.environ, "NODE_PATH": deps["node_path"]},
            )
        finally:
            os.unlink(handle.name)
        payload = parse_json_fragment(result.stdout) if result.stdout.strip() else {
            "status": "error", "message": command_error(result)}
        if payload["status"] == "needs_user_action":
            return payload
        if payload["status"] != "ok":
            return {"status": "failed", "message": payload["message"]}

        added = [_export_row(card) for card in payload["connections"] if card["slug"] not in known]
        rows = [*added, *existing]
        if not rows:
            return {"status": "failed", "message": "LinkedIn showed no connections."}
        CsvIO.write_dict_rows(self.csv_path, EXPORT_COLUMNS, rows)
        complete = payload["stopped"] != "limit"
        write_json(self.manifest_path, {
            "status": "completed", "complete": complete, "connections": len(rows), "added": len(added),
            "loads": payload["loads"], "stopped": payload["stopped"], "updated_at": now_iso()})
        message = f"{len(rows):,} LinkedIn connections ({len(added):,} new)"
        if not complete:
            message += ". The rest keep syncing on your next run; your contacts are ready to process now."
        return {
            "status": "completed",
            "message": message,
            "connections": len(rows),
            "added": len(added),
            "complete": complete,
            "path": str(self.csv_path),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=CONNECTIONS_CSV)
    parser.add_argument("--profile-dir", type=Path, default=BROWSER_PROFILE)
    parser.add_argument("--login-timeout-seconds", type=int, default=LOGIN_TIMEOUT_SECONDS)
    args = parser.parse_args()
    payload = LinkedInConnections(csv_path=args.csv, profile_dir=args.profile_dir,
                                  login_timeout_seconds=args.login_timeout_seconds).run()
    emit(payload)
    return 0 if payload["status"] == "completed" else 10 if payload["status"] == "needs_user_action" else 1


if __name__ == "__main__":
    raise SystemExit(main())
