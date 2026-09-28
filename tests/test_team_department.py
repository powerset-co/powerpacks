"""One JD selects one employee department using the shared Jev request cache."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from packs.search.primitives.deep_search.team_department import Department
from packs.search.primitives.llm_rerank_candidates.jev import client as jev


DEPARTMENTS = {
    "technical", "product", "design", "marketing", "sales", "customer_service",
    "hr", "finance", "legal", "operations", "consulting", "general_management",
    "research", "project_management", "education", "medical", "trades",
    "real_estate", "administrative", "other_department",
}


class TeamDepartmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_dir = Path(directory.name)
        self.calls = []
        self.jd = "Backend engineer building APIs; partner with sales and marketing."
        self.as_of = "2026-09-28"
        self.enterContext(mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "synthetic-key"}))
        self.usage = self.enterContext(mock.patch.object(jev, "append_usage_row"))

    def _client(self, *, invalid=False):
        def respond(request):
            payload = json.loads(request.content)
            self.calls.append(payload)
            probabilities = {name: 0.0 for name in DEPARTMENTS}
            probabilities.update(technical=0.8, sales=0.2)
            if invalid:
                probabilities.pop("technical")
            return httpx.Response(200, json={
                "model": jev.MODEL,
                "answers": {"department": {"type": "choice", "probabilities": probabilities}},
                "usage": {"input_tokens": 500, "output_tokens": 20},
            })
        return httpx.AsyncClient(transport=httpx.MockTransport(respond))

    async def test_fixed_choices_and_max_probability_cached_without_credentials(self):
        self.assertEqual({department.value for department in Department}, DEPARTMENTS)
        client = self._client()
        with mock.patch.object(jev.httpx, "AsyncClient", return_value=client) as create:
            first = await Department.from_jd(self.jd, as_of=self.as_of, run_dir=self.run_dir)
            with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
                second = await Department.from_jd(self.jd, as_of=self.as_of, run_dir=self.run_dir)
        self.assertEqual(first, Department("technical"))
        self.assertEqual(second, first)
        create.assert_called_once()
        self.assertEqual(len(self.calls), 1)
        request = self.calls[0]
        self.assertEqual(request["state"]["job_description"], self.jd)
        self.assertEqual(request["state"]["reference_date"], self.as_of)
        self.assertEqual(set(request["questions"]), {"department"})
        self.assertEqual(request["questions"]["department"]["type"], "choice")
        self.assertEqual(set(request["questions"]["department"]["criteria"]), DEPARTMENTS)
        self.assertEqual(len(list(self.run_dir.rglob("jev/*.json"))), 1)
        self.usage.assert_called_once()

    async def test_changed_jd_gets_a_new_exact_request(self):
        clients = [self._client(), self._client()]
        with mock.patch.object(jev.httpx, "AsyncClient", side_effect=clients):
            await Department.from_jd(self.jd, as_of=self.as_of, run_dir=self.run_dir)
            await Department.from_jd("Finance analyst", as_of=self.as_of, run_dir=self.run_dir)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(list(self.run_dir.rglob("jev/*.json"))), 2)

    async def test_invalid_response_is_preserved_and_not_rebilled(self):
        client = self._client(invalid=True)
        with mock.patch.object(jev.httpx, "AsyncClient", return_value=client) as create:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "invalid response"):
                    await Department.from_jd(self.jd, as_of=self.as_of, run_dir=self.run_dir)
        create.assert_called_once()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(list(self.run_dir.rglob("jev/*.json"))), 1)
        self.usage.assert_called_once()

    async def test_blank_jd_stops_before_http(self):
        with mock.patch.object(jev.httpx, "AsyncClient") as create:
            with self.assertRaises(ValueError):
                await Department.from_jd("  ", as_of=self.as_of, run_dir=self.run_dir)
        create.assert_not_called()

    async def test_missing_credentials_stops_before_http(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}), \
             mock.patch.object(jev.httpx, "AsyncClient") as create:
            with self.assertRaisesRegex(RuntimeError, "TYPESAFE_API_KEY"):
                await Department.from_jd(self.jd, as_of=self.as_of, run_dir=self.run_dir)
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
