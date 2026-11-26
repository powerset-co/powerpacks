#!/usr/bin/env python3
"""Ask a teammate's install for remote assist from a cloud session, signed in by the relay key.

The API's AGENT_RELAY_API_KEY signs these calls as the relay bot (claude@powerset.co) on the relay
routes only. Pass it as POWERSET_RELAY_KEY, or leave that unset when the network proxy adds the
X-API-Key header itself. Replies land in the bot's inbox, never a person's.

    relay_ask.py presence                       who has an install, and when it last polled
    relay_ask.py ask <email|operator_id> TEXT   send a support request, then wait for its result
    relay_ask.py wait <request_id>              keep waiting for a result (acked once printed)

Changelog:
- 2026-11-26: created so cloud threads can use the relay without a person's laptop.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.powerset.primitives.agent_inbox.messages import MAX_RESULT, SupportRequest, SupportResult
from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import API_BASE_ENV_KEY, DEFAULT_API_BASE

POLL_SECONDS = 15


def _call(path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if os.environ.get("POWERSET_RELAY_KEY"):
        headers["X-API-Key"] = os.environ["POWERSET_RELAY_KEY"]
    base = (os.environ.get(API_BASE_ENV_KEY) or DEFAULT_API_BASE).rstrip("/")
    request = urllib.request.Request(base + path, headers=headers, method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode("utf-8"))
    with urllib.request.urlopen(request, timeout=30) as response:
        answer: dict[str, Any] = json.load(response)
    return answer


def presence() -> list[dict[str, Any]]:
    agents: list[dict[str, Any]] = _call("/v2/agents/presence")["agents"]
    return agents


def ask(to: str, text: str) -> str:
    """Send a support request; returns its id. An email is resolved to the operator's id when present."""
    try:
        operator_id = str(uuid.UUID(to))
    except ValueError:
        operator_id = next((row["operator_id"] for row in presence() if row["email"].lower() == to.lower()), "")
    request = SupportRequest.parse({"text": text[:MAX_RESULT], "thread_id": operator_id or to})
    sent = _call("/v2/agent-messages", {"to": operator_id or to, "kind": "support_request",
                                        "payload": {"text": request.text, "thread_id": request.thread_id}})
    return str(sent["id"])


def wait(request_id: str, seconds: float) -> str | None:
    """The result's text once it arrives (and is acked), or None when `seconds` pass first."""
    deadline = time.monotonic() + seconds
    while True:
        for message in _call("/v2/agent-messages")["messages"]:
            if message["kind"] == "support_result" and message["payload"].get("request_id") == request_id:
                result = SupportResult.parse(message["payload"])
                _call(f"/v2/agent-messages/{message['id']}/ack", {})
                return result.text
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("presence")
    asking = commands.add_parser("ask")
    asking.add_argument("to")
    asking.add_argument("text")
    waiting = commands.add_parser("wait")
    waiting.add_argument("request_id")
    for command in (asking, waiting):
        command.add_argument("--wait", type=float, default=540, help="seconds to wait for the result")
    args = parser.parse_args(argv)
    if args.command == "presence":
        print(json.dumps(presence(), indent=2))
        return 0
    request_id = ask(args.to, args.text) if args.command == "ask" else args.request_id
    print(f"request {request_id}", file=sys.stderr)
    text = wait(request_id, args.wait)
    if text is None:
        print(f"no result yet; run: relay_ask.py wait {request_id}", file=sys.stderr)
        return 2
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
