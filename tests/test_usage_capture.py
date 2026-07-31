"""E2E tests for the shared client's POWERPACKS_USAGE_LOG capture -> usage.jsonl -> cost_report.

One flow, not isolated units: mocked-transport calls go through BOTH factories with the
env set, rows land in a real tmp file, and cost_report prices that same file against a
hand-computed golden. Capture is always on: unset env means the global sink.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "packs" / "search" / "primitives" / "shared"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


oc = _load("openai_client_under_test", SHARED / "openai_client.py")
cr = _load("cost_report_under_test", ROOT / "packs" / "search" / "reflect" / "cost_report.py")


def _fake_response() -> SimpleNamespace:
    return SimpleNamespace(
        model="test-model",
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=30,  # includes 10 reasoning tokens, per OpenAI usage semantics
            completion_tokens_details=SimpleNamespace(reasoning_tokens=10),
        ),
    )


class TestUsageCaptureSetPath(unittest.TestCase):
    """POWERPACKS_USAGE_LOG set: both factories write rows; cost_report prices the same file."""

    def setUp(self) -> None:
        self.log = Path(tempfile.mkdtemp()) / "usage.jsonl"

    def _sync_calls(self, n: int) -> None:
        with mock.patch("openai.resources.chat.completions.Completions.create",
                        return_value=_fake_response()):
            with mock.patch.dict(os.environ, {"POWERPACKS_USAGE_LOG": str(self.log),
                                              "POWERPACKS_USAGE_STAGE": "triage"}):
                client = oc.make_openai_client(api_key="test-key")
                for _ in range(n):
                    client.chat.completions.create(model="test-model", messages=[])

    def _async_calls(self, n: int) -> None:
        async def fake_create(*args, **kwargs):
            return _fake_response()

        with mock.patch("openai.resources.chat.completions.AsyncCompletions.create",
                        new=fake_create):
            with mock.patch.dict(os.environ, {"POWERPACKS_USAGE_LOG": str(self.log),
                                              "POWERPACKS_USAGE_STAGE": "judge"}):
                client = oc.make_async_openai_client(api_key="test-key")

                async def burst():
                    await asyncio.gather(*[
                        client.chat.completions.create(model="test-model", messages=[])
                        for _ in range(n)
                    ])

                asyncio.run(burst())

    def test_capture_to_file_to_cost_report(self) -> None:
        self._sync_calls(2)
        self._async_calls(3)  # concurrent, proves append safety

        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(rows), 5)
        for row in rows:
            self.assertEqual(row["model"], "test-model")
            self.assertEqual(row["prompt_tokens"], 100)
            self.assertEqual(row["completion_tokens"], 20)  # reasoning broken out, not double-counted
            self.assertEqual(row["reasoning_tokens"], 10)
            self.assertIn(row["stage"], ("triage", "judge"))
            self.assertGreaterEqual(row["latency_ms"], 0)
        self.assertEqual(sum(1 for r in rows if r["stage"] == "triage"), 2)
        self.assertEqual(sum(1 for r in rows if r["stage"] == "judge"), 3)

        # Same file straight into cost_report, priced against a hand-computed golden:
        # per row: 100/1M*$2 + 20/1M*$4 + 10/1M*$4 = 0.0002 + 0.00008 + 0.00004 = $0.00032
        prices = {"test-model": {"input_per_1m": 2.0, "output_per_1m": 4.0}}
        report = cr.build_report(cr.load_rows(self.log), prices)
        self.assertEqual(report["totals"]["calls"], 5)
        self.assertAlmostEqual(report["totals"]["cost_usd"], 0.0016, places=6)
        self.assertTrue(report["totals"]["fully_priced"])
        self.assertAlmostEqual(report["by_stage"]["triage"]["cost_usd"], 0.00064, places=6)
        self.assertAlmostEqual(report["by_stage"]["judge"]["cost_usd"], 0.00096, places=6)
        self.assertEqual(report["by_model"]["test-model"]["prompt_tokens"], 500)


class TestUsageCaptureAlwaysOn(unittest.TestCase):
    def test_unset_env_captures_to_the_global_sink(self) -> None:
        default_sink = Path(tempfile.mkdtemp()) / "usage" / "usage.jsonl"  # dir must be auto-created
        env = {k: v for k, v in os.environ.items() if k != "POWERPACKS_USAGE_LOG"}
        with mock.patch("openai.resources.chat.completions.Completions.create",
                        return_value=_fake_response()):
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(oc, "DEFAULT_USAGE_LOG", default_sink):
                    client = oc.make_openai_client(api_key="test-key")
                    resp = client.chat.completions.create(model="test-model", messages=[])
        self.assertEqual(resp.model, "test-model")
        rows = [json.loads(line) for line in default_sink.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "test-model")

    def test_logging_failure_never_breaks_the_call(self) -> None:
        bad_log = "/nonexistent-dir-for-usage-capture/usage.jsonl"
        with mock.patch("openai.resources.chat.completions.Completions.create",
                        return_value=_fake_response()):
            with mock.patch.dict(os.environ, {"POWERPACKS_USAGE_LOG": bad_log}):
                client = oc.make_openai_client(api_key="test-key")
                resp = client.chat.completions.create(model="test-model", messages=[])
        self.assertEqual(resp.model, "test-model")  # fail-open


class TestDatedModelIdPricing(unittest.TestCase):
    def test_dated_ids_prefix_match_and_mini_stays_separate(self) -> None:
        prices = {"gpt-4.1": {"input_per_1m": 2.0, "output_per_1m": 8.0},
                  "gpt-4.1-mini": {"input_per_1m": 0.4, "output_per_1m": 1.6}}
        row = {"prompt_tokens": 1_000_000, "completion_tokens": 0, "reasoning_tokens": 0}
        self.assertAlmostEqual(cr.row_cost_usd({**row, "model": "gpt-4.1-2025-04-14"}, prices), 2.0)
        self.assertAlmostEqual(cr.row_cost_usd({**row, "model": "gpt-4.1-mini-2025-04-14"}, prices), 0.4)
        self.assertIsNone(cr.row_cost_usd({**row, "model": "gpt-9-2030-01-01"}, prices))


if __name__ == "__main__":
    unittest.main()
