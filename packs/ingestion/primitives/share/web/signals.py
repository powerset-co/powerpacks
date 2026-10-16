"""Refresh People's signals from saved facts and counts: Worth (one Luna request per family), the settle rule
(as Enrich applies it after worth), then the labels.

With no one selected it asks every family; a selection asks those. An unchanged request is answered
from the saved answers for free. The check never calls OpenAI. A start
is bound to those requests and their cache coverage; without a confirmed check it reads only saved
answers, even if one disappears. Worth and labels run in one transaction, so a failure keeps the
previous signals and human decisions.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store
from packs.ingestion.primitives.deep_context_v2.enrich.enrich import Enrich
from packs.ingestion.primitives.deep_context_v2.enrich.settle import settle
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.ingestion.primitives.deep_context_v2.worth.worth import DEFAULT_LIMIT, Worth
from packs.ingestion.primitives.share.person_labels import PersonLabels


# Requests answered per second, for the time estimate: Luna's measured pace, 1,011 requests in 13 s
# on 510 families (2026-10-13).
REQUESTS_PER_SECOND = 75


@dataclass(frozen=True)
class SignalsPlan:
    people: int
    cached: int
    new_requests: int
    estimated_cost_usd: float
    estimated_seconds: int
    checked: str


@dataclass(frozen=True)
class SignalsStatus:
    status: Literal["idle", "running", "completed", "failed"] = "idle"
    message: str | None = None
    plan: SignalsPlan | None = None
    error: str | None = None
    # Requests answered in the current phase, of `total`.
    done: int = 0
    total: int = 0
    # Families Luna declined to answer: they keep their old signals and are asked next time.
    refused: int = 0


class RefreshSignals(Node):
    name = "signals"
    reads = tuple(dict.fromkeys((*Worth.reads, *Enrich.reads, *PersonLabels.reads)))
    writes = (*Worth.writes, *PersonLabels.writes)
    cache_only = True

    def __init__(self, conn: sqlite3.Connection, data_root: Path, parent_ids: tuple[str, ...]) -> None:
        super().__init__(conn, data_root)
        self.worth = Worth(conn, data_root, limit=DEFAULT_LIMIT, parent_ids=parent_ids)
        self.parent_ids = parent_ids
        self.checked: str | None = None

    def plan(self) -> SignalsPlan:
        families = self.worth.pending(self.worth.families())
        if not set(self.parent_ids) <= {family.key for family in families}:
            raise ValueError("Some selected people no longer have saved dossiers. Reload People and try again.")
        estimate = self.worth.estimate()
        cached = int(estimate["cached"])
        requests = int(estimate["requests"])
        cost = float(estimate["estimated_cost_usd"])
        digest = hashlib.sha256(json.dumps([(family.key, family.members, family.digest) for family in families]).encode())
        digest.update(json.dumps((cached, requests, estimate["input_tokens"])).encode())
        new: int = requests - cached
        return SignalsPlan(len(families), cached, new, cost, math.ceil(new / REQUESTS_PER_SECOND), digest.hexdigest())

    def execute(self) -> dict[str, int]:
        plan = self.plan()
        if self.checked is not None and self.checked != plan.checked:
            raise ValueError("The saved dossiers or cached answers changed. Check the cost again.")
        self.worth.on_progress = self.progress
        counts = self.worth.execute(cache_only=self.cache_only)
        counts["settle_worth_rows"] = settle(self.conn, self.data_root, now_iso())
        self.progress("Updating People", 0, 0)
        PersonLabels(self.conn, self.data_root).execute()
        return counts


class PeopleSignals:
    """The People page's one refresh; same in-process progress shape as its Logbook build."""

    def __init__(self, db_path: Path, data_root: Path) -> None:
        self.db_path = db_path
        self.data_root = data_root
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._status = SignalsStatus()

    def check(self, parent_ids: tuple[str, ...]) -> SignalsPlan:
        conn = open_store(self.db_path)
        try:
            return RefreshSignals(conn, self.data_root, parent_ids).plan()
        finally:
            conn.close()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return asdict(self._status)

    def start(self, parent_ids: tuple[str, ...], checked: str | None) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ValueError("Signals are already refreshing. Wait for them to finish.")
            conn = open_store(self.db_path, shared=True)
            node = RefreshSignals(conn, self.data_root, parent_ids)
            try:
                plan = node.plan()
                if checked is not None and checked != plan.checked:
                    raise ValueError("The saved dossiers or cached answers changed. Check the cost again.")
                if plan.new_requests and checked is None:
                    raise ValueError("Check and confirm the estimated cost before refreshing signals.")
            except Exception:
                conn.close()
                raise
            self._status = SignalsStatus("running", "Reading signals", plan)
            node.cache_only = plan.new_requests == 0
            node.checked = plan.checked
            self._thread = threading.Thread(target=self._run, args=(node,), daemon=True)
            self._thread.start()
        return self.status()

    def _run(self, node: RefreshSignals) -> None:
        def progress(message: str, done: int, total: int) -> None:
            with self._lock:
                self._status = replace(self._status, message=message, done=done, total=total)
        node.on_progress = progress
        try:
            manifest = node.run()
        except Exception as error:
            with self._lock:
                self._status = replace(self._status, status="failed", message=None, error=str(error))
        else:
            with self._lock:
                self._status = replace(self._status, status="completed", message=None,
                                       refused=manifest.counts["refused"])
        finally:
            node.conn.close()
