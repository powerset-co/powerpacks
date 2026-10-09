"""Listen for Ask the Set tasks, answers and agent messages, and announce this laptop's presence.

Changelog:
- 2026-10-08: add the local NATS loop with durable task delivery.
- 2026-10-08: re-fetch the connection and reconnect every REFRESH_SECONDS, before a 24 h credential expires.
- 2026-10-08: ask for the default set's connection; the route refuses a bare call from a member of several sets.
- 2026-10-08: subscribe to the operator's inbox subject and pull agent messages on each nudge.
- 2026-10-08: keep the last heartbeat per operator in .powerpacks/presence.json for the sets page.
- 2026-10-08: keep the relay state for the top bar's status dot; a sign-in wakes a signed-out wait.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from pathlib import Path
from uuid import uuid4

import nats
from dotenv import dotenv_values

from packs.ingestion.primitives.ask_worker import ask_worker
from packs.ingestion.primitives.common.jsonio import write_json
from packs.powerset.primitives.agent_inbox import agent_inbox
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth
from packs.search.primitives.ask_status import ask_status

HEARTBEAT_SECONDS = 30
REFRESH_SECONDS = 20 * 3600
SIGNED_OUT_SECONDS = 300
MAX_BACKOFF_SECONDS = 30
HTTP_TIMEOUT_SECONDS = 30
_LOG = logging.getLogger(__name__)
# The relay state the page shows: connected, signed_out or offline. The loop is the only writer.
STATUS = {"state": "offline"}
WAKE = threading.Event()  # set after a sign-in so a signed-out wait reconnects at once


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

    async def work() -> None:
        async with worker_lock:
            await asyncio.to_thread(ask_worker.run, repo_root=repo_root,
                                    env_file=env_file, device_id=device_id)

    async def tasks(message) -> None:
        if json.loads(message.data)["kind"] == "tasks":
            await work()
        await message.ack()

    async def inbox(message) -> None:
        if json.loads(message.data)["kind"] == "message":
            await asyncio.to_thread(agent_inbox.pull, repo_root=repo_root, env_file=env_file)
        await message.ack()

    presence_file = repo_root / ".powerpacks" / "presence.json"
    seen = json.loads(presence_file.read_text(encoding="utf-8")) if presence_file.exists() else {}

    async def heartbeat(message) -> None:
        beat = json.loads(message.data)
        seen[beat["operator_id"]] = beat["at"]
        write_json(presence_file, seen)

    async def answer(run_dir: Path, message) -> None:
        if json.loads(message.data)["kind"] == "answer":
            await asyncio.to_thread(ask_status.run, run_dir, env_file=env_file)

    subjects = connection["subjects"]
    nc = await nats.connect(connection["url"], token=connection["token"],
                            allow_reconnect=False, closed_cb=on_close, error_cb=on_error)

    async def watch() -> None:
        subscribed = set()
        connected_at = time.monotonic()
        while not closed.is_set():
            if time.monotonic() - connected_at >= REFRESH_SECONDS:
                return
            for directory in ("deep-search", "search"):
                for path in (repo_root / ".powerpacks" / directory).glob("*/ask.json"):
                    ask_id = json.loads(path.read_text(encoding="utf-8"))["ask_id"]
                    subject = subjects["asks"] + ask_id
                    if subject not in subscribed:
                        await nc.subscribe(subject, cb=partial(answer, path.parent))
                        subscribed.add(subject)
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
        await nc.jetstream().subscribe(subjects["tasks"], stream="asks", durable=device_id,
                                       cb=tasks, manual_ack=True)
        await nc.jetstream().subscribe(subjects["inbox"], stream="asks", durable=device_id + "-inbox",
                                       cb=inbox, manual_ack=True)
        await nc.subscribe(subjects["presence"], cb=heartbeat)
        watcher = asyncio.create_task(watch())
        STATUS["state"] = "connected"
        await work()
        await asyncio.to_thread(agent_inbox.pull, repo_root=repo_root, env_file=env_file)
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
