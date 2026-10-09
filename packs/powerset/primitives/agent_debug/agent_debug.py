#!/usr/bin/env python3
"""Answer a teammate's debug_request with a fixed set of read-only checks of this install.

A debug_request names no commands: the payload may pick checks by name from CHECKS, nothing more.
Every check only reads (files, a read-only SQLite connection, `git rev-parse`/`describe`); none
writes, deletes or spends. The answer is one debug_result agent message back to the sender.

Changelog:
- 2026-10-09: answer() takes the typed DebugRequest; sending and reading go through agent_inbox.
- 2026-10-08: created for remote diagnostics over the Ask the Set relay.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import cast

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.deep_context_v2.db.schema import SCHEMA_VERSION
from packs.powerset.primitives.agent_inbox import agent_inbox
from packs.powerset.primitives.agent_inbox.messages import DebugRequest, DebugResult

REQUEST = "debug_request"
RESULT = "debug_result"
OUTPUT_CAP = 20_000  # characters per check; the tail is kept
LOG_LINES = 200
INSTALL_FIELDS = ("status", "step", "event", "message", "note", "retry_command", "steps", "updated_at",
                  "person_count", "network_name", "log_path")
UPLOAD_FIELDS = ("status", "stage", "dry_run", "started_at", "finished_at", "progress", "error", "error_type",
                 "http_status", "last_upload")


def _git(repo_root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True,
                              check=True).stdout.strip()
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"git {' '.join(args)}: {error.stderr.strip()}") from error


def _fields(path: Path, names: tuple[str, ...]) -> str:
    record = json.loads(path.read_text(encoding="utf-8"))
    return json.dumps({name: record.get(name) for name in names}, indent=2)


def version(repo_root: Path) -> str:
    """The release in .release-please-manifest.json."""
    return json.loads((repo_root / ".release-please-manifest.json").read_text(encoding="utf-8"))["."]


def git(repo_root: Path) -> str:
    """The checkout's branch, commit and describe; a downloaded install has no .git and fails here."""
    return json.dumps({
        "branch": _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD"),
        "commit": _git(repo_root, "rev-parse", "HEAD"),
        "describe": _git(repo_root, "describe", "--tags", "--always"),
    }, indent=2)


def install(repo_root: Path) -> str:
    """The installer's progress record, without its prose, plan and fingerprints."""
    return _fields(repo_root / ".powerpacks" / "install" / "manifest.json", INSTALL_FIELDS)


def store(repo_root: Path) -> str:
    """The deep-context store's schema version and every table's row count, opened read-only."""
    path = repo_root / ".powerpacks" / "deep-context" / "deep-context-v2.sqlite"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        schema = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        counts = {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
    finally:
        conn.close()
    return json.dumps({"schema_version": schema, "code_schema_version": SCHEMA_VERSION, "rows": counts}, indent=2)


def server_log(repo_root: Path) -> str:
    """The last LOG_LINES lines of the local server's log."""
    lines = (repo_root / ".powerpacks" / "install" / "server.log").read_text(
        encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-LOG_LINES:])


def upload(repo_root: Path) -> str:
    """The share upload's manifest, without its per-person hashes."""
    return _fields(repo_root / ".powerpacks" / "upload-powerset" / "manifest.json", UPLOAD_FIELDS)


def relay(repo_root: Path) -> str:
    """This device's id, the last heartbeat seen per operator, and the inbox's messages by kind."""
    data = repo_root / ".powerpacks"
    presence = data / "presence.json"
    inbox = Counter(json.loads(path.read_text(encoding="utf-8"))["kind"]
                    for path in (data / "inbox").glob("*.json"))
    return json.dumps({
        "device_id": (data / "device-id").read_text(encoding="utf-8").strip(),
        "presence": json.loads(presence.read_text(encoding="utf-8")) if presence.exists() else {},
        "inbox": dict(inbox),
    }, indent=2)


CHECKS = {check.__name__: check for check in (version, git, install, store, server_log, upload, relay)}


def run_checks(names: list[str], repo_root: Path) -> dict[str, dict]:
    results = {}
    for name in names:
        if name not in CHECKS:
            results[name] = {"ok": False, "output": "unknown check"}
            continue
        try:
            results[name] = {"ok": True, "output": CHECKS[name](repo_root)[-OUTPUT_CAP:]}
        except Exception as error:
            results[name] = {"ok": False, "output": f"{type(error).__name__}: {error}"[-OUTPUT_CAP:]}
    return results


def answer(message_id: str, sender: str, request: DebugRequest, *, repo_root: Path, env_file: Path) -> dict:
    """Run the requested checks (all when none are named) and send one debug_result to the sender."""
    names = list(request.checks) or list(CHECKS)
    payload = {"request_id": message_id, "results": run_checks(names, repo_root)}
    return agent_inbox.send(env_file, sender, RESULT, payload)


def find_result(data_root: Path, request_id: str) -> DebugResult | None:
    """The debug_result in the inbox that answers request_id."""
    for envelope in agent_inbox.read(data_root, RESULT):
        result = cast(DebugResult, envelope.payload)
        if result.request_id == request_id:
            return result
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    request = commands.add_parser("request", help="Ask a teammate's install to run its checks")
    request.add_argument("--to", required=True, help="Teammate's email or operator id")
    request.add_argument("--check", action="append", help=f"Run only these checks: {', '.join(CHECKS)}")
    request.add_argument("--env-file", type=Path, default=_REPO_ROOT / ".env")
    show = commands.add_parser("show", help="Print the debug_result for a request id from this inbox")
    show.add_argument("request_id")
    args = parser.parse_args(argv)
    if args.command == "request":
        sent = agent_inbox.send(args.env_file, args.to, REQUEST, {"checks": args.check} if args.check else {})
        print(json.dumps(sent, indent=2))
        return 0
    result = find_result(_REPO_ROOT / ".powerpacks", args.request_id)
    if result is None:
        print(json.dumps({"request_id": args.request_id, "status": "not arrived"}, indent=2))
    else:
        print(json.dumps(asdict(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
