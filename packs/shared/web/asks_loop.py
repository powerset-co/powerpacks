"""Pull this laptop's agent messages as they arrive, answer asks among them, and announce its presence.

Changelog:
- 2026-10-08: add the local NATS loop with durable task delivery.
- 2026-10-08: re-fetch the connection and reconnect every REFRESH_SECONDS, before a 24 h credential expires.
- 2026-10-08: ask for the default set's connection; the route refuses a bare call from a member of several sets.
- 2026-10-08: subscribe to the operator's inbox subject and pull agent messages on each nudge.
- 2026-10-08: keep the last heartbeat per operator in .powerpacks/presence.json for the sets page.
- 2026-10-08: keep the relay state for the top bar's status dot; a sign-in wakes a signed-out wait.
- 2026-10-08: asks are agent messages: the inbox pull answers each unanswered `ask` (and `debug_request`)
  from someone in one of this owner's sets, and marks the rest refused; no ask tasks or ask subjects.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from uuid import uuid4

import nats
from dotenv import dotenv_values

from packs.ingestion.primitives.ask_worker import ask_worker
from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.share.web.sets import Sets
from packs.powerset.primitives.agent_debug import agent_debug
from packs.powerset.primitives.agent_inbox import agent_inbox
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth

HEARTBEAT_SECONDS = 30
REFRESH_SECONDS = 20 * 3600
SIGNED_OUT_SECONDS = 300
MAX_BACKOFF_SECONDS = 30
HTTP_TIMEOUT_SECONDS = 30
_LOG = logging.getLogger(__name__)
# Message kinds this machine answers without a person, and only for someone in one of its sets: an ask,
# a teammate's debug checks.
ANSWERED_HERE = {"ask": ask_worker.answer_message, "debug_request": agent_debug.answer}
# The relay state the page shows: connected, signed_out or offline. The loop is the only writer.
STATUS = {"state": "offline"}
WAKE = threading.Event()  # set after a sign-in so a signed-out wait reconnects at once


def _set_members(repo_root: Path, env_file: Path) -> set[str]:
    """The operators in this owner's sets, read from the store the People page keeps."""
    with closing(open_store(store_path(repo_root / ".powerpacks"))) as conn:
        sets = Sets(conn, env_file)
        sets.settle()  # a delete or a leave the relay just delivered counts before anyone is answered
        return sets.known_operators()


def _device_id(repo_root: Path) -> str:
    path = repo_root / ".powerpacks" / "device-id"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(uuid4()), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def _set_id(env_file: Path) -> str:
    """The default set from the env file, the same keys the search primitives read."""
    values = dotenv_values(env_file) if env_file.exists() else {}
    return values.get("POWERPACKS_DEFAULT_SET_ID") or values.get("POWERSET_DEFAULT_SET_ID") or ""


def _connection(env_file: Path) -> dict:
    token = auth.bearer_token(env_file)
    query = f"?set_id={_set_id(env_file)}" if _set_id(env_file) else ""
    request = urllib.request.Request(
        auth.api_base(env_file) + "/v2/nats/connection" + query,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == HTTPStatus.UNAUTHORIZED:
            raise SystemExit("signed out") from exc
        raise


async def _connected(connection: dict, *, repo_root: Path, env_file: Path, device_id: str) -> None:
    closed = asyncio.Event()
    worker_lock = asyncio.Lock()

    async def on_close() -> None:
        closed.set()

    async def on_error(exc: Exception) -> None:
        _LOG.warning("Ask NATS: %s", type(exc).__name__)

    async def pull() -> None:
        """Pull the inbox, then answer what this machine answers by itself (asks, debug requests), one
        at a time. A failed answer stays unanswered and is tried again on the next pull."""
        async with worker_lock:
            await asyncio.to_thread(agent_inbox.pull, repo_root=repo_root, env_file=env_file)
            known = await asyncio.to_thread(_set_members, repo_root, env_file)
            for path in sorted((repo_root / ".powerpacks" / "inbox").glob("*.json")):
                message = json.loads(path.read_text(encoding="utf-8"))
                handler = ANSWERED_HERE.get(message["kind"])
                if handler is None or "answered_at" in message:
                    continue
                if message["from"]["operator_id"] not in known:
                    # Not someone in a set with this owner: no answer, no model call, no checks.
                    write_json(path, {**message, "answered_at": now_iso(), "refused": "not in a set"})
                    continue
                try:
                    await asyncio.to_thread(handler, message, repo_root=repo_root, env_file=env_file)
                except Exception as exc:
                    _LOG.warning("Answering %s: %s", message["kind"], type(exc).__name__)
                    continue
                write_json(path, {**json.loads(path.read_text(encoding="utf-8")), "answered_at": now_iso()})

    async def inbox(message) -> None:
        if json.loads(message.data)["kind"] == "message":
            await pull()
        await message.ack()

    presence_file = repo_root / ".powerpacks" / "presence.json"
    seen = json.loads(presence_file.read_text(encoding="utf-8")) if presence_file.exists() else {}

    async def heartbeat(message) -> None:
        beat = json.loads(message.data)
        seen[beat["operator_id"]] = beat["at"]
        write_json(presence_file, seen)

    subjects = connection["subjects"]
    nc = await nats.connect(connection["url"], token=connection["token"],
                            allow_reconnect=False, closed_cb=on_close, error_cb=on_error)

    async def watch() -> None:
        connected_at = time.monotonic()
        while not closed.is_set():
            if time.monotonic() - connected_at >= REFRESH_SECONDS:
                return
            await nc.publish(subjects["presence"], json.dumps({
                "kind": "heartbeat", "operator_id": subjects["tasks"].split(".")[-1],
                "device_id": device_id, "at": datetime.now(timezone.utc).isoformat(),
            }).encode())
            try:
                await asyncio.wait_for(closed.wait(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                continue
        raise ConnectionError("NATS connection closed")

    watcher = None
    try:
        await nc.jetstream().subscribe(subjects["inbox"], stream="asks", durable=device_id + "-inbox",
                                       cb=inbox, manual_ack=True)
        await nc.subscribe(subjects["presence"], cb=heartbeat)
        watcher = asyncio.create_task(watch())
        STATUS["state"] = "connected"
        await pull()
        await watcher
    finally:
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        STATUS["state"] = "offline"
        await nc.close()


async def _run(*, repo_root: Path, env_file: Path, device_id: str) -> None:
    backoff = 1
    while True:
        started = time.monotonic()
        try:
            connection = await asyncio.to_thread(_connection, env_file)
            await _connected(connection, repo_root=repo_root, env_file=env_file, device_id=device_id)
            backoff = 1  # a refresh: the watcher returned, reconnect at once with fresh credentials
        except SystemExit:
            STATUS["state"] = "signed_out"
            WAKE.clear()
            await asyncio.to_thread(WAKE.wait, SIGNED_OUT_SECONDS)
            STATUS["state"] = "offline"
            backoff = 1
        except Exception as exc:
            if time.monotonic() - started > MAX_BACKOFF_SECONDS:
                backoff = 1
            _LOG.warning("Ask loop: %s; retrying in %ss", type(exc).__name__, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)


def run(*, repo_root: Path, env_file: Path) -> None:
    """Run inside the local server's daemon thread."""
    asyncio.run(_run(repo_root=repo_root, env_file=env_file, device_id=_device_id(repo_root)))
