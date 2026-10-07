"""One block of the pipeline: declares the tables it reads and writes, runs once, leaves one manifest.

The declaration is enforced, not documented: while `execute()` runs, a SQLite
authorizer denies reads of undeclared tables and writes to tables outside
`writes`. A violation fails the run.

Created: 2026-10-06
"""
from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar

from packs.ingestion.primitives.deep_context_v2.db.schema import VIEW_TABLES
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso

MANIFEST_RELATIVE_DIR = Path("deep-context") / "v2-manifests"
_WRITE_ACTIONS = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
_ALWAYS_READABLE = {"meta"}


@dataclass(frozen=True)
class Manifest:
    stage: str
    status: str  # completed | failed | not_ready
    started_at: str
    finished_at: str
    counts: dict[str, int]
    error: str | None


class Node(ABC):
    name: ClassVar[str]
    reads: ClassVar[tuple[str, ...]]
    writes: ClassVar[tuple[str, ...]]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        for attr in ("name", "reads", "writes"):
            if not hasattr(cls, attr):
                raise TypeError(f"{cls.__name__} must declare `{attr}`")

    def __init__(self, conn: sqlite3.Connection, data_root: Path) -> None:
        self.conn = conn
        self.data_root = data_root

    def required_files(self) -> tuple[Path, ...]:
        """Files that must exist before `execute()`; the import block declares its inputs here."""
        return ()

    @abstractmethod
    def execute(self) -> dict[str, int]:
        """Do the block's work; return the counts for the manifest."""

    def run(self) -> Manifest:
        started = now_iso()
        missing = [str(path) for path in self.required_files() if not path.exists()]
        if missing:
            return self._finish(started, "not_ready", {}, "missing inputs: " + ", ".join(missing))
        # A write to a child table makes SQLite read the parent for the foreign-key check, and a
        # declared view is read through its tables; both are part of the declaration.
        self._implied_reads: set[str] = set()
        for table in self.writes:
            for row in self.conn.execute(f"PRAGMA foreign_key_list({table})"):
                self._implied_reads.add(row[2])
        for view in self.reads:
            self._implied_reads.update(VIEW_TABLES.get(view, ()))
        self.conn.set_authorizer(self._authorizer)
        try:
            counts = self.execute()
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            self._finish(started, "failed", {}, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self.conn.set_authorizer(None)
        return self._finish(started, "completed", counts, None)

    def _authorizer(self, action: int, table: str | None, column: str | None, db: str | None, trigger: str | None) -> int:
        if table is None or table.startswith("sqlite_"):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            allowed = (table in self.reads or table in self.writes or table in _ALWAYS_READABLE
                       or table in self._implied_reads)
            return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
        if action in _WRITE_ACTIONS:
            return sqlite3.SQLITE_OK if table in self.writes else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def _finish(self, started: str, status: str, counts: dict[str, int], error: str | None) -> Manifest:
        manifest = Manifest(self.name, status, started, now_iso(), counts, error)
        directory = self.data_root / MANIFEST_RELATIVE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{self.name}.json").write_text(json.dumps(asdict(manifest), indent=2) + "\n")
        return manifest
