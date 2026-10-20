"""A daemon thread imports, generates, indexes and discovers from one memory job."""
from __future__ import annotations

import math
import os
import fcntl
import threading
from collections.abc import Generator
from dataclasses import asdict
from typing import TypeVar
from pathlib import Path

from packs.indexing.lib.llm_config import DEFAULT_SYNTHESIS_MODEL
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context_v2.openai import load_env
from packs.memory import job
from packs.memory.cost import Usage
from packs.memory.discovery import Discover
from packs.memory.generate import Generate
from packs.memory.job import Counts, Failure, Job, Status
from packs.memory.progress import Progress, Step
from packs.memory.provider import DEFAULT_EMBEDDING_MODEL, OpenAIProvider, SpendReached
from packs.memory.sources import V2
from packs.memory.store import Store

_threads: dict[Path, threading.Thread] = {}


def provider() -> OpenAIProvider:
    load_env()
    return OpenAIProvider(DEFAULT_SYNTHESIS_MODEL, os.getenv("POWERPACKS_OPENAI_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
                          approve_spend=True)


def start(root: Path, approved_spend: float) -> Job:
    if not math.isfinite(approved_spend) or approved_spend < 0:
        raise ValueError("approved_spend_usd must be a finite nonnegative number")
    root = root.resolve()
    with job.LOCK:
        directory = job.database(root).parent
        directory.mkdir(parents=True, exist_ok=True)
        handle = os.open(directory, os.O_RDONLY)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(handle)
            raise ValueError("memory is already running") from None
        record = job.read(root)
        client = provider()
        record.error = ""
        if not os.getenv("OPENAI_API_KEY"):
            record.error = "OPENAI_API_KEY is not set"
        elif client.usage.cost(client.model, client.embedding_key) is None:
            record.error = "model pricing unavailable"
        if record.error:
            record.status = Status.FAILED
            job.write(root, record)
            client.close()
            os.close(handle)
            return record
        resume = record.status == Status.RUNNING
        if not resume:
            record.step = Step.IMPORT
            record.steps = {step: Counts() for step in job.STEPS}
            record.started_at = now_iso()
            record.tokens = asdict(Usage())
            record.spent_usd = 0
        record.pid = os.getpid()
        record.stopped = ""
        record.enabled = True
        record.approved_spend_usd = approved_spend
        record.status = Status.RUNNING
        job.write(root, record)
        thread = threading.Thread(target=_run, args=(root, resume, handle, client), daemon=True, name="memory")
        _threads[root] = thread
        thread.start()
        return record


def running(root: Path) -> bool:
    with job.LOCK:
        handle = os.open(job.database(root).parent, os.O_RDONLY)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(handle)
    return False


def update(root: Path, approved_spend: float | None = None) -> Job:
    record = job.read(root)
    if not record.enabled:
        raise ValueError("enable memory first")
    return start(root, record.approved_spend_usd if approved_spend is None else approved_spend)


def keep_updated(root: Path) -> None:
    record = job.read(root)
    if record.enabled and record.keep_updated:
        try:
            update(root)
        except ValueError:
            # A completion notification can arrive while the page's run is still working.
            if job.read(root).status != Status.RUNNING:
                raise
    thread = _threads.get(root.resolve())
    if thread is not None:
        thread.join()


def _run(root: Path, resume: bool, handle: int, client: OpenAIProvider) -> None:
    store: Store | None = None
    try:
        record = job.read(root)
        client.approved_spend_usd = record.approved_spend_usd
        client.usage = Usage(**record.tokens)
        store = Store(job.database(root), create=True)
        adapter = V2(root / ".powerpacks")
        generate = Generate(store, client)
        if resume and record.failures:
            record.step = min((record.step, *(failure.step for failure in record.failures)), key=job.STEPS.index)
        for step in job.STEPS:
            if resume and job.STEPS.index(step) < job.STEPS.index(record.step):
                continue
            first = tuple(item.key for item in record.failures if item.step == step)
            with job.LOCK:
                current = job.read(root)
                current.step = step
                current.current_item = ""
                current.steps[step] = Counts()
                job.write(root, current)
            if step == Step.IMPORT:
                _record(root, store.import_progress(adapter, first=first), client)
            elif step == Step.GENERATE:
                _record(root, generate.chunks(limit=2**31 - 1, first=first), client)
            elif step == Step.INDEX:
                _record(root, generate.index(limit=2**31 - 1, first=first), client)
            else:
                _record(root, Discover(store, client).progress(limit=2**31 - 1, first=first), client)
        with job.LOCK:
            record = job.read(root)
            record.people_indexed = store.conn.execute("SELECT count(DISTINCT parent) FROM active_claims").fetchone()[0]
            record.status, record.step = Status.DONE, Step.DONE
            record.current_item = ""
            job.write(root, record)
    except SpendReached:
        with job.LOCK:
            record = job.read(root)
            record.status = Status.DONE
            record.stopped = "approved spend reached"
            record.current_item = ""
            if store is not None:
                record.people_indexed = store.conn.execute("SELECT count(DISTINCT parent) FROM active_claims").fetchone()[0]
            job.write(root, record)
    except Exception as exc:
        with job.LOCK:
            record = job.read(root)
            record.status, record.error = Status.FAILED, str(exc)
            job.write(root, record)
    finally:
        if store is not None:
            store.close()
        client.close()
        os.close(handle)


R = TypeVar("R")


def _record(root: Path, events: Generator[Progress, None, R], client: OpenAIProvider) -> R:
    previous = job.read(root)
    before = previous.steps[previous.step]
    while True:
        try:
            event = next(events)
        except StopIteration as finished:
            result: R = finished.value
            return result
        with job.LOCK:
            record = job.read(root)
            record.steps[event.step] = Counts(before.done + event.done, before.total + event.total)
            record.current_item = event.key
            record.tokens = asdict(client.usage)
            record.spent_usd = client.usage.cost(client.model, client.embedding_key)
            record.failures = [item for item in record.failures if (item.step, item.key) != (event.step, event.key)]
            if event.error:
                record.failures.append(Failure(event.step, event.key, event.error, now_iso()))
            job.write(root, record)
