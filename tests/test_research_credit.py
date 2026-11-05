"""A Parallel 402 (insufficient credit) waits for the balance to reload instead of failing research, and
answers already paid for are kept when a later batch cannot be."""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from parallel import APIStatusError

from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.deep_context_v2.enrich import research


def status_error(code: int) -> APIStatusError:
    request = httpx.Request("POST", "https://proxy.powerset.dev/vendor/parallel/v1/tasks/groups/g/runs")
    return APIStatusError("Error code: %d" % code, response=httpx.Response(code, request=request),
                          body={"error": {"message": "Insufficient credit in account"}})


class CreditWaitTests(unittest.TestCase):
    def test_a_402_waits_in_turn_and_then_succeeds(self) -> None:
        attempts: list[int] = []
        slept: list[int] = []

        def call() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise status_error(402)
            return "submitted"

        with patch.object(research.time, "sleep", slept.append), redirect_stderr(io.StringIO()) as err:
            result = research.with_credit(call, "500 runs", waits=(60, 120, 240))
        self.assertEqual((result, slept), ("submitted", [60, 120]))
        self.assertIn("waiting 60s", err.getvalue())

    def test_the_last_402_is_the_error(self) -> None:
        calls: list[int] = []
        slept: list[int] = []

        def call() -> str:
            calls.append(1)
            raise status_error(402)

        with patch.object(research.time, "sleep", slept.append), redirect_stderr(io.StringIO()), \
             self.assertRaises(APIStatusError):
            research.with_credit(call, "500 runs", waits=(60, 120, 240))
        self.assertEqual((len(calls), slept), (4, [60, 120, 240]))

    def test_other_errors_are_raised_at_once(self) -> None:
        slept: list[int] = []

        def call() -> str:
            raise status_error(500)

        with patch.object(research.time, "sleep", slept.append), self.assertRaises(APIStatusError):
            research.with_credit(call, "500 runs")
        self.assertEqual(slept, [])


class RunEvent(SimpleNamespace):
    pass


class StatusEvent(SimpleNamespace):
    pass


class FakeOutput:
    def __init__(self, content: dict) -> None:
        self.content = content

    def model_dump(self, **_: object) -> dict:
        return {"content": self.content}


class FakeParallel:
    """Accepts the first add_runs, answers 402 to every later one, and streams the accepted runs' answers."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.added: list[list[dict]] = []
        self.task_group = self
        self.task_run = self

    def create(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(task_group_id="tgrp_test")

    def add_runs(self, group_id: str, *, inputs: list[dict], default_task_spec: object) -> None:
        if self.added:
            raise status_error(402)
        self.added.append(list(inputs))

    @contextmanager
    def events(self, group_id: str, **_: object):
        def stream():
            for run in self.added[0]:
                handle = run["metadata"]["handle"]
                yield RunEvent(run=SimpleNamespace(metadata={"handle": handle}, status="completed", is_active=False,
                                                   run_id=handle),
                               output=FakeOutput({"linkedin_url": f"https://www.linkedin.com/in/{handle}"}))
            yield StatusEvent(status=SimpleNamespace(is_active=False))
        yield stream()


class LaterBatchTests(unittest.TestCase):
    def test_answers_of_accepted_runs_are_kept_when_a_later_batch_has_no_credit(self) -> None:
        root = Path(tempfile.mkdtemp())
        conn = open_store(root / "store.sqlite")
        self.addCleanup(conn.close)
        todo = [research.ResearchSubject(f"p:{i}", f"handle-{i}", f"dossier {i}") for i in range(3)]
        client = FakeParallel()
        progress: list[tuple[str, int, int]] = []
        with patch.object(research, "PARALLEL_BATCH_SIZE", 2), patch.object(research, "Parallel", lambda *a, **k: client), \
             patch.object(research, "TaskRunEvent", RunEvent), patch.object(research, "TaskGroupStatusEvent", StatusEvent), \
             patch.object(research, "load_env", lambda: None), patch.object(research.auth, "gateway_token", lambda: "t"), \
             patch.object(research.time, "sleep", lambda seconds: None), redirect_stderr(io.StringIO()), \
             self.assertRaises(APIStatusError) as unpaid:
            research.submit(conn, todo, on_progress=lambda *call: progress.append(call))
        self.assertEqual(unpaid.exception.status_code, 402)
        rows = conn.execute("SELECT handle, status FROM research ORDER BY handle").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("handle-0", "complete"), ("handle-1", "complete")])
        self.assertEqual(progress[-1], ("research", 2, 2))
        self.assertEqual(len(client.added), 1)

    def test_a_first_batch_without_credit_raises_with_nothing_to_keep(self) -> None:
        root = Path(tempfile.mkdtemp())
        conn = open_store(root / "store.sqlite")
        self.addCleanup(conn.close)
        client = FakeParallel()
        client.added.append([])  # the next add_runs is refused, as if the first batch had no credit
        with patch.object(research, "Parallel", lambda *a, **k: client), patch.object(research, "load_env", lambda: None), \
             patch.object(research.auth, "gateway_token", lambda: "t"), patch.object(research.time, "sleep", lambda s: None), \
             redirect_stderr(io.StringIO()), self.assertRaises(APIStatusError):
            research.submit(conn, [research.ResearchSubject("p:0", "handle-0", "dossier")])
        self.assertEqual(conn.execute("SELECT count(*) FROM research").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
