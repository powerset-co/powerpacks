"""Listen for Ask the Set tasks and answers, and announce this laptop's presence.

Changelog:
- 2026-10-08: add the local NATS loop with durable task delivery.
- 2026-10-08: re-fetch the connection and reconnect every REFRESH_SECONDS, before a 24 h credential expires.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from pathlib import Path
from uuid import uuid4

import nats

from packs.ingestion.primitives.ask_worker import ask_worker
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth
from packs.search.primitives.ask_status import ask_status

HEARTBEAT_SECONDS = 30
REFRESH_SECONDS = 20 * 3600
SIGNED_OUT_SECONDS = 300
MAX_BACKOFF_SECONDS = 30
HTTP_TIMEOUT_SECONDS = 30
_LOG = logging.getLogger(__name__)


def _device_id(repo_root: Path) -> str:
    path = repo_root / ".powerpacks" / "device-id"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(uuid4()), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def _connection(env_file: Path) -> dict:
    token = auth.bearer_token(env_file)
    request = urllib.request.Request(
        auth.api_base(env_file) + "/v2/nats/connection",
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
        watcher = asyncio.create_task(watch())
        await work()
        await watcher
    finally:
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
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
            await asyncio.sleep(SIGNED_OUT_SECONDS)
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
