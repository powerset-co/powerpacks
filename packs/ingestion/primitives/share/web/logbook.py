"""Build the raw Logbook for people selected on the People page, one build at a time.

Flow: POST `/api/people/logbook` resolves the selected parents
(`logbook_people.people_for_parents`) and starts one thread running
`build_logbook` over the local stores; GET `/api/people/logbook` reads the status
and never builds. The status lives in this process only: a restart is idle; the
saved archive is read from disk by logbook_archive.py. Nothing here uploads or shares
the archive.

Changelog:
  2026-09-30: the zip download is gone; the People page reads the saved archive.
  2026-09-30: created.
"""

from __future__ import annotations

import sys
import threading
import traceback
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

import sqlite3
from packs.ingestion.primitives.common.person import Person
from packs.ingestion.primitives.logbook.logbook_common import LOGBOOK_ROOT
from packs.ingestion.primitives.logbook.logbook_export import LogbookBuild, build_logbook, default_paths
from packs.ingestion.primitives.logbook.logbook_people import people_for_parents

BUILD_ACTIVE = "A logbook is already being built. Wait for it to finish."


class LogbookState(StrEnum):
    IDLE = "idle"
    BUILDING = "building"
    COMPLETED = "completed"
    FAILED = "failed"


class PeopleLogbook:
    def __init__(self, conn: sqlite3.Connection, *, root: Path = LOGBOOK_ROOT, stores: dict[str, Path] | None = None) -> None:
        self.conn = conn
        self.root = root
        self.stores = stores or default_paths()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._people: tuple[str, ...] = ()
        self._build: LogbookBuild | None = None
        self._error: str | None = None

    def _state(self) -> LogbookState:
        if self._thread is not None and self._thread.is_alive():
            return LogbookState.BUILDING
        if self._error is not None:
            return LogbookState.FAILED
        return LogbookState.COMPLETED if self._build is not None else LogbookState.IDLE

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._state()
            return {
                "status": state.value,
                "people": list(self._people),
                "result": self._build.to_payload() if state is LogbookState.COMPLETED else None,
                "error": self._error if state is LogbookState.FAILED else None,
            }

    def start(self, parent_ids: Sequence[str]) -> dict[str, Any]:
        """Start one build for the selected parents; an unknown parent raises ``LookupError``,
        a build already running raises ``RuntimeError``."""
        people = people_for_parents(self.conn, parent_ids)
        with self._lock:
            if self._state() is LogbookState.BUILDING:
                raise RuntimeError(BUILD_ACTIVE)
            self._people = tuple(person.person_id for person in people)
            self._build = None
            self._error = None
            self._thread = threading.Thread(target=self._run, args=(people,), daemon=True)
            self._thread.start()
        return self.status()

    def _run(self, people: list[Person]) -> None:
        try:
            build = build_logbook(people, paths=self.stores, root=self.root)
        except BaseException as exc:
            # The msgvault store exits (SystemExit) on a bad database; a thread would end silently.
            if isinstance(exc, KeyboardInterrupt):
                raise
            traceback.print_exc(file=sys.stderr)
            with self._lock:
                self._error = str(exc) or type(exc).__name__
            return
        with self._lock:
            self._build = build
