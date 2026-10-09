#!/usr/bin/env python3
"""Pull this operator's agent messages from the relay into .powerpacks/inbox and ack them.

The relay carries any kind; this laptop keeps only the kinds in messages.py whose fields check out.
Every message is acked, so anything else (an unknown kind, a bad field) is discarded, logged by kind.

Changelog:
- 2026-10-09: share relay message sending through send.
- 2026-10-08: keep only messages that parse (messages.py); ack and discard the rest.
- 2026-10-08: created for the Ask the Set comms pipeline.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import write_json
from packs.powerset.primitives.agent_inbox.messages import Rejected, parse
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth

HTTP_TIMEOUT_SECONDS = 30


def _request(base: str, path: str, token: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        base + path, headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                              "Content-Type": "application/json"},
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        answer: dict[str, Any] = json.load(response)
    return answer


def send(env_file: Path, to: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST one agent message to the relay; returns its {"id", "status"}."""
    return _request(auth.api_base(env_file), "/v2/agent-messages", auth.bearer_token(env_file),
                    {"to": to, "kind": kind, "payload": payload})


def pull(*, repo_root: Path, env_file: Path) -> list[dict[str, Any]]:
    """Fetch unacked messages, keep each one that parses as .powerpacks/inbox/<id>.json, ack every one;
    return the kept ones."""
    token = auth.bearer_token(env_file)
    base = auth.api_base(env_file)
    inbox = repo_root / ".powerpacks" / "inbox"
    kept = []
    for message in _request(base, "/v2/agent-messages", token)["messages"]:
        try:
            envelope = parse(message)
        except Rejected as reason:
            print(f"agent-inbox: discarded {str(message.get('kind'))[:40]!r}: {reason}", file=sys.stderr)
        else:
            write_json(inbox / f"{envelope.id}.json", message)
            kept.append(message)
            print(f"agent-inbox: {envelope.kind} from {envelope.from_name}", file=sys.stderr)
        _request(base, f"/v2/agent-messages/{message['id']}/ack", token, {})
    return kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["pull"])
    parser.add_argument("--env-file", type=Path, default=_REPO_ROOT / ".env")
    args = parser.parse_args(argv)
    print(json.dumps(pull(repo_root=_REPO_ROOT, env_file=args.env_file), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
