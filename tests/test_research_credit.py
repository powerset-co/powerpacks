"""A Parallel 402 (insufficient credit) waits for the balance to reload instead of failing research."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import httpx
from parallel import APIStatusError

from packs.ingestion.primitives.deep_context_v2.enrich import research


def status_error(code: int) -> APIStatusError:
    request = httpx.Request("POST", "https://proxy.powerset.dev/vendor/parallel/v1/tasks/groups/g/runs")
    return APIStatusError("Error code: %d" % code, response=httpx.Response(code, request=request),
                          body={"error": {"message": "Insufficient credit in account"}})


class CreditWaitTests(unittest.TestCase):
    def test_a_402_waits_in_turn_and_then_succeeds(self) -> None:
        answers = [status_error(402), status_error(402), "submitted"]
        slept: list[int] = []
        with patch.object(research.time, "sleep", slept.append), redirect_stderr(io.StringIO()) as err:
            result = research.with_credit(lambda: answers.pop(0) if not isinstance(answers[0], Exception) else (_ for _ in ()).throw(answers.pop(0)),
                                          "500 runs", waits=(60, 120, 240))
        self.assertEqual(result, "submitted")
        self.assertEqual(slept, [60, 120])
        self.assertIn("waiting 60s", err.getvalue())

    def test_the_last_402_is_the_error(self) -> None:
        calls: list[int] = []

        def call() -> str:
            calls.append(1)
            raise status_error(402)

        with patch.object(research.time, "sleep", lambda seconds: None), redirect_stderr(io.StringIO()), \
             self.assertRaises(APIStatusError):
            research.with_credit(call, "500 runs", waits=(60, 120, 240))
        self.assertEqual(len(calls), 4)

    def test_other_errors_are_raised_at_once(self) -> None:
        slept: list[int] = []

        def call() -> str:
            raise status_error(500)

        with patch.object(research.time, "sleep", slept.append), self.assertRaises(APIStatusError):
            research.with_credit(call, "500 runs")
        self.assertEqual(slept, [])


if __name__ == "__main__":
    unittest.main()
