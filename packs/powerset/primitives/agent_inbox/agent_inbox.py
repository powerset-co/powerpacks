#!/usr/bin/env python3
"""Pull this operator's agent messages from the relay into .powerpacks/inbox and ack them.

Any kind rides here: an invite, a note from a teammate's agent, whatever the relay carries.
The local agent reads the files; nothing here interprets them.

Changelog:
- 2026-10-08: created for the Ask the Set comms pipeline.
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

from packs.ingestion.primitives.common.jsonio import write_json
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth

HTTP_TIMEOUT_SECONDS = 30


def _request(base: str, path: str, token: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        base + path, headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                              "Content-Type": "application/json"},
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.load(response)


def pull(*, repo_root: Path, env_file: Path) -> list[dict]:
    """Fetch unacked messages, write each to .powerpacks/inbox/<id>.json, ack it; return them."""
    token = auth.bearer_token(env_file)
    base = auth.api_base(env_file)
    inbox = repo_root / ".powerpacks" / "inbox"
    messages = _request(base, "/v2/agent-messages", token)["messages"]
    for message in messages:
        write_json(inbox / f"{message['id']}.json", message)
        _request(base, f"/v2/agent-messages/{message['id']}/ack", token, {})
        print(f"agent-inbox: {message['kind']} from {message['from']['name']}", file=sys.stderr)
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["pull"])
    parser.add_argument("--env-file", type=Path, default=_REPO_ROOT / ".env")
    args = parser.parse_args(argv)
    print(json.dumps(pull(repo_root=_REPO_ROOT, env_file=args.env_file), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
