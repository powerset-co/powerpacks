#!/usr/bin/env python3
"""This laptop's side of the relay's agent messages: send one, pull new ones into .powerpacks/inbox, read
them back typed.

The relay carries any kind; this laptop keeps only the kinds in messages.py whose fields check out.
Every pulled message is acked, so anything else (an unknown kind, a bad field) is discarded, logged by
kind. An inbox file is written once, by the pull, and deleted by whatever consumes it (asks_loop for
set changes, asks and debug requests; the Sets page for an answered invite). Answers to this laptop's
own asks and debug requests stay as results.

Changelog:
- 2026-10-09: one home for relay calls: send() and request() (with NeedsSignIn and CloudError) replace
  the copies in sets.py, ask_worker and agent_debug; read() returns typed envelopes.
- 2026-10-08: keep only messages that parse (messages.py); ack and discard the rest.
- 2026-10-08: created for the Ask the Set comms pipeline.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import write_json
from packs.powerset.primitives.agent_inbox.messages import Envelope, Rejected, parse
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth

MESSAGES_PATH = "/v2/agent-messages"
HTTP_TIMEOUT_SECONDS = 30


class NeedsSignIn(Exception):
    """Powerset has no usable sign-in on this machine."""


class CloudError(Exception):
    """The relay refused or could not be reached; the words are what the page shows."""


def request(env_file: Path, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """One call to the Powerset API as this machine's operator."""
    try:
        token = auth.bearer_token(env_file)
    except SystemExit as error:
        raise NeedsSignIn(str(error)) from error
    call = urllib.request.Request(
        auth.api_base(env_file) + path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(call, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise NeedsSignIn("Powerset rejected the sign-in") from error
        raise CloudError(f"Powerset answered {error.code} for {path}") from error
    except urllib.error.URLError as error:
        raise CloudError(f"Couldn't reach Powerset: {error.reason}") from error
    if not raw:
        return None
    return json.loads(raw)


def send(env_file: Path, to: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One agent message to an email or operator id; the relay holds it until that laptop pulls it.
    Returns the relay's {"id", "status"}."""
    sent: dict[str, Any] = request(env_file, "POST", MESSAGES_PATH, {"to": to, "kind": kind, "payload": payload})
    return sent


def pull(*, repo_root: Path, env_file: Path) -> list[Envelope]:
    """Fetch unacked messages, keep each one that parses as .powerpacks/inbox/<id>.json, ack every one;
    return the kept ones."""
    inbox = repo_root / ".powerpacks" / "inbox"
    kept = []
    for message in request(env_file, "GET", MESSAGES_PATH)["messages"]:
        try:
            envelope = parse(message)
        except Rejected as reason:
            print(f"agent-inbox: discarded {str(message.get('kind'))[:40]!r}: {reason}", file=sys.stderr)
        else:
            write_json(inbox / f"{envelope.id}.json", message)
            kept.append(envelope)
            print(f"agent-inbox: {envelope.kind} from {envelope.from_name}", file=sys.stderr)
        request(env_file, "POST", f"{MESSAGES_PATH}/{message['id']}/ack", {})
    return kept


def read(data_root: Path, *kinds: str) -> list[Envelope]:
    """The inbox's messages of these kinds, typed, oldest first."""
    found = []
    for path in (data_root / "inbox").glob("*.json"):
        message = json.loads(path.read_text(encoding="utf-8"))
        if message["kind"] in kinds:
            found.append(parse(message))
    found.sort(key=lambda envelope: envelope.created_at)
    return found


def remove(data_root: Path, envelope: Envelope) -> None:
    """Delete a consumed message from the inbox."""
    (data_root / "inbox" / f"{envelope.id}.json").unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["pull"])
    parser.add_argument("--env-file", type=Path, default=_REPO_ROOT / ".env")
    args = parser.parse_args(argv)
    for envelope in pull(repo_root=_REPO_ROOT, env_file=args.env_file):
        print(f"{envelope.kind} {envelope.id} from {envelope.from_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
