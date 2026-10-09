#!/usr/bin/env python3
"""Steer a teammate's coding agent over the relay: send an instruction, read it, report back.

A steer is an agent message of kind `steer` with payload {"text"}. The receiving agent finds it in
`.powerpacks/inbox/<id>.json` (written by agent_inbox), decides what to do, and answers with `done`,
which marks the file and sends kind `steer_done` with payload {"steer_id", "note"} to the sender.
Nothing here executes the instruction.

Changelog:
- 2026-10-08: created: send, inbox, done, replies.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import now_iso, read_json, write_json
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth

MESSAGES_PATH = "/v2/agent-messages"
STEER = "steer"
STEER_DONE = "steer_done"
HTTP_TIMEOUT_SECONDS = 30


def _post(env_file: Path, body: dict) -> dict:
    """POST one agent message to the relay; return its {"id", "status"}."""
    request = urllib.request.Request(
        auth.api_base(env_file) + MESSAGES_PATH, method="POST", data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {auth.bearer_token(env_file)}", "Accept": "application/json",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.load(response)


def _messages(repo_root: Path, kind: str) -> list[dict]:
    """This machine's inbox messages of one kind, oldest first."""
    found = [read_json(path, {}) for path in (repo_root / ".powerpacks" / "inbox").glob("*.json")]
    return sorted((message for message in found if message.get("kind") == kind),
                  key=lambda message: message["created_at"])


def send(*, env_file: Path, to: str, text: str) -> dict:
    return _post(env_file, {"to": to, "kind": STEER, "payload": {"text": text}})


def inbox(*, repo_root: Path) -> list[dict]:
    """Open steers: not yet answered with `done`."""
    return [{"id": message["id"], "from": message["from"]["name"], "created_at": message["created_at"],
             "text": message["payload"]["text"]}
            for message in _messages(repo_root, STEER) if "done_at" not in message]


def done(*, repo_root: Path, env_file: Path, steer_id: str, note: str) -> dict:
    """Mark the steer answered and send the note back to its sender."""
    path = repo_root / ".powerpacks" / "inbox" / f"{steer_id}.json"
    message = json.loads(path.read_text(encoding="utf-8"))
    write_json(path, message | {"done_at": now_iso(), "note": note})
    return _post(env_file, {"to": message["from"]["operator_id"], "kind": STEER_DONE,
                            "payload": {"steer_id": steer_id, "note": note}})


def replies(*, repo_root: Path) -> list[dict]:
    """What the other agents reported, newest first."""
    return [{"id": message["id"], "from": message["from"]["name"], "created_at": message["created_at"],
             "steer_id": message["payload"]["steer_id"], "note": message["payload"]["note"]}
            for message in reversed(_messages(repo_root, STEER_DONE))]


def main(argv: list[str] | None = None) -> int:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--env-file", type=Path, default=_REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sending = commands.add_parser("send", parents=[shared], help="send an instruction to a teammate's agent")
    sending.add_argument("--to", required=True, help="email or operator id")
    sending.add_argument("--text", required=True)
    commands.add_parser("inbox", parents=[shared], help="open steers sent to this machine")
    finishing = commands.add_parser("done", parents=[shared], help="mark a steer answered and send the note back")
    finishing.add_argument("steer_id")
    finishing.add_argument("--note", required=True)
    commands.add_parser("replies", parents=[shared], help="notes other agents sent back")
    args = parser.parse_args(argv)
    if args.command == "send":
        result = send(env_file=args.env_file, to=args.to, text=args.text)
    elif args.command == "inbox":
        result = inbox(repo_root=_REPO_ROOT)
    elif args.command == "done":
        result = done(repo_root=_REPO_ROOT, env_file=args.env_file, steer_id=args.steer_id, note=args.note)
    else:
        result = replies(repo_root=_REPO_ROOT)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
