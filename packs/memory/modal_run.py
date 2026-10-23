"""Run memory on the operator's Modal volume and mirror its job back to the laptop."""
from __future__ import annotations

import io
import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from aiohttp import ClientError
from modal import Sandbox
from modal.exception import (
    ConnectionError as ModalConnectionError,
    NotFoundError,
    ServiceError,
    TimeoutError as ModalTimeoutError,
)

from packs.indexing.modal import linkedin_modal_pipeline as pipeline
from packs.memory import job
from packs.memory.job import Job, Status
from packs.memory.progress import Step

_RETRY_SECONDS = 3
_READ_ATTEMPTS = 3
_READ_ERRORS = (ModalConnectionError, ServiceError, ModalTimeoutError, OSError, ClientError)


class Remote:
    def __init__(self, root: Path, operator_id: str) -> None:
        self.root = root
        self.operator_id = operator_id
        self.volume = pipeline.get_volume()
        self.prefix = f"operators/{operator_id}"
        self.directory = f"{self.prefix}/.powerpacks/memory"
        self.name = "memory-" + self.prefix.rsplit("/", 1)[-1]

    def _status(self) -> Job | None:
        for attempt in range(_READ_ATTEMPTS):
            try:
                payload = b"".join(self.volume.read_file(f"{self.directory}/job.json"))
            except FileNotFoundError:
                return None
            except _READ_ERRORS:
                if attempt == _READ_ATTEMPTS - 1:
                    raise
                time.sleep(_RETRY_SECONDS)
            else:
                return job.decode(payload)

    def reattach(self, *, resume: bool) -> bool:
        try:
            sandbox = Sandbox.from_name(pipeline.APP_NAME, self.name)
        except NotFoundError:
            sandbox = None
        if sandbox is None:
            status = self._status()
            if status is None:
                return False
            database = job.database(self.root)
            newer = (status.status != Status.RUNNING and database.exists()
                     and datetime.fromisoformat(status.updated_at).timestamp() > database.stat().st_mtime)
            if not newer and (not resume or status.started_at != job.read(self.root).started_at):
                return False
        self._watch(sandbox)
        return True

    def run(self, discover: bool) -> None:
        record = job.read(self.root)
        record.step = Step.GENERATE
        with self.volume.batch_upload(force=True) as batch:
            batch.put_file(job.database(self.root), f"{self.directory}/memory.sqlite")
            batch.put_file(io.BytesIO(json.dumps(asdict(record)).encode()), f"{self.directory}/job.json")
        sandbox = pipeline.memory_sandbox(self.name, self.operator_id, discover=discover)
        self._watch(sandbox)

    def _watch(self, sandbox: Sandbox | None) -> None:
        if sandbox is not None:
            print(f"memory: attached sandbox {sandbox.object_id}", flush=True)
            for attempt in range(_READ_ATTEMPTS):
                try:
                    if attempt:
                        # A failed stdout iterator is closed; reopen the sandbox's log stream.
                        sandbox = Sandbox.from_id(sandbox.object_id)
                    for line in sandbox.stdout:
                        status = job.decode(line)
                        _mirror(self.root, status)
                    sandbox.wait()
                    break
                except _READ_ERRORS:
                    if attempt == _READ_ATTEMPTS - 1:
                        raise
                    time.sleep(_RETRY_SECONDS)
        status = self._status()
        self._download()
        if status is not None:
            with job.LOCK:
                _mirror(self.root, status)
                record = job.read(self.root)
                record.status = status.status
                record.failures = status.failures
                job.write(self.root, record)
        if status is None or status.status == Status.RUNNING:
            raise RuntimeError("Modal memory sandbox stopped before finishing")

    def _download(self) -> None:
        database = job.database(self.root)
        temporary = database.with_suffix(".tmp")
        with temporary.open("wb") as handle:
            for chunk in self.volume.read_file(f"{self.directory}/memory.sqlite"):
                handle.write(chunk)
        database.replace(database.with_suffix(".sqlite.bkup"))
        temporary.replace(database)
        print(f"memory: downloaded {database}", flush=True)


def _mirror(root: Path, status: Job) -> None:
    with job.LOCK:
        record = job.read(root)
        for name in ("step", "steps", "current_item", "people_indexed", "started_at",
                     "tokens", "spent_usd", "stopped", "error"):
            setattr(record, name, getattr(status, name))
        job.write(root, record)
