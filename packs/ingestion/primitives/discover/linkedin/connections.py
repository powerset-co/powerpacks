"""Read the user's LinkedIn connections in Chrome into `discover/linkedin/Connections.csv`.

Flow: open the saved Chrome profile headless on LinkedIn's connections page (a
logs in there once; the profile keeps the session) -> scroll the newest-first
list -> put the new connections on top of the existing rows -> write the CSV in
LinkedIn's export columns, which the Modal `import-linkedin` step reads.

One run makes at most LOADS_PER_RUN scroll requests, 10 people each, with a
random pause between them. A run stops once KNOWN_OVERLAP already-known
connections have loaded. `connections.json` beside the CSV records whether the
whole list has been read and the signed-in user's own profile URL; until the
list is read, each run goes LOADS_PER_RUN further down than the last. A CSV with
no record is a LinkedIn export (or an earlier setup's copy of one): complete
as of its date, so only newer people are read. The CSV is rewritten only when
there are new people, so the Modal import reruns only then.

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
from packs.ingestion.schemas.people_schema import extract_public_identifier
from packs.ingestion.primitives.setup.automations.shell import (
    command_error,
    parse_json_fragment,
    run_streaming_command,
)
from packs.shared.csv_io import CsvIO

CONNECTIONS_CSV = Path(".powerpacks/network-import/discover/linkedin/Connections.csv")
SCRAPE_RECORD = CONNECTIONS_CSV.with_name("connections.json")
BROWSER_PROFILE = Path("~/.powerpacks/browser-profiles/linkedin")
BROWSER_SCRIPT = Path(__file__).with_name("connections_browser.js")
EXPORT_COLUMNS = ["First Name", "Last Name", "URL", "Email Address", "Company", "Position", "Connected On"]
PROFILE_URL = "https://www.linkedin.com/in/{slug}"
LOGIN_TIMEOUT_SECONDS = 900
# A load takes 0.5-1.5 s plus page time; a deep backfill run needs room for all of them.
SECONDS_PER_LOAD = 3
LOADS_PER_RUN = 300
KNOWN_OVERLAP = 25


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
    """Scrape new connections and keep the CSV newest-first; login() only signs in."""

    def __init__(self, *, csv_path: Path = CONNECTIONS_CSV, profile_dir: Path = BROWSER_PROFILE,
                 login_timeout_seconds: int = LOGIN_TIMEOUT_SECONDS) -> None:
        self.csv_path = csv_path
        self.profile_dir = profile_dir.expanduser()
        self.login_timeout_seconds = login_timeout_seconds
        self.record_path = csv_path.with_name(SCRAPE_RECORD.name)

    def _browser(self, *args: str, timeout: int) -> dict[str, Any]:
        deps = ensure_playwright_core()
        if deps["status"] != "ok":
            return {"status": "failed", "message": deps["message"]}
        result = run_streaming_command(
            ["node", str(BROWSER_SCRIPT), "--profile-dir", str(self.profile_dir),
             "--timeout-seconds", str(self.login_timeout_seconds), *args],
            timeout=timeout, env={**os.environ, "NODE_PATH": deps["node_path"]})
        return parse_json_fragment(result.stdout) if result.stdout.strip() else {
            "status": "error", "message": command_error(result)}

    def login(self) -> dict[str, Any]:
        """Make sure the saved profile is signed in; a window opens only if it is not."""
        payload = self._browser("--login-only", "1", timeout=self.login_timeout_seconds + 60)
        if payload["status"] == "ok":
            return {"status": "completed", "message": "Signed in to LinkedIn"}
        return payload if payload["status"] == "needs_user_action" else {"status": "failed", "message": payload["message"]}

    def run(self) -> dict[str, Any]:
        existing = _read_export(self.csv_path)
        known = {extract_public_identifier(row["URL"]) for row in existing} - {""}
        previous = read_json(self.record_path, {}) or {}
        backfill = previous.get("complete") is False
        max_loads = previous.get("loads", 0) + LOADS_PER_RUN if backfill else LOADS_PER_RUN
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(sorted(known), handle)
        try:
            payload = self._browser(
                "--known-file", handle.name, "--stop-after-known", "0" if backfill else str(KNOWN_OVERLAP),
                "--max-loads", str(max_loads), timeout=self.login_timeout_seconds + max_loads * SECONDS_PER_LOAD)
        finally:
            os.unlink(handle.name)
        if payload["status"] == "needs_user_action":
            return payload
        if payload["status"] != "ok":
            return {"status": "failed", "message": payload["message"]}

        added = [_export_row(card) for card in payload["connections"]
                 if extract_public_identifier(PROFILE_URL.format(slug=card["slug"])) not in known]
        rows = [*added, *existing]
        if not rows:
            return {"status": "failed", "message": "LinkedIn showed no connections."}
        if added:
            CsvIO.write_dict_rows(self.csv_path, EXPORT_COLUMNS, rows)
        complete = payload["stopped"] != "limit"
        write_json(self.record_path, {
            "status": "completed", "complete": complete, "connections": len(rows), "added": len(added),
            "loads": payload["loads"], "stopped": payload["stopped"],
            "owner_url": PROFILE_URL.format(slug=payload["owner_slug"]) if payload["owner_slug"] else "",
            "updated_at": now_iso()})
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
