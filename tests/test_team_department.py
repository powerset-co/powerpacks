"""One JD selects one employee department with one cached Luna choice."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from packs.ingestion.primitives.deep_context_v2 import decisions
from packs.search.primitives.deep_search.team_department import Department


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
        self.calls: list[dict[str, Any]] = []
        self.refuse = False
        self.jd = "Backend engineer building APIs; partner with sales and marketing."
        self.as_of = "2026-09-28"
        real = decisions.AsyncOpenAI

        def respond(request: httpx.Request) -> httpx.Response:
            self.calls.append(json.loads(request.content))
            if self.refuse:
                return httpx.Response(200, json={"answers": [{"type": "refusal", "name": "department"}]})
            probabilities = [{"value": name, "probability": 0.8 if name == "technical" else 0.01}
                             for name in sorted(DEPARTMENTS)]
            return httpx.Response(200, json={"answers": [
                {"type": "choice", "name": "department", "choice": "technical", "probabilities": probabilities}]})

        def client(**options: Any) -> Any:
            return real(**options, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))

        self.enterContext(mock.patch.object(decisions, "AsyncOpenAI", client))
        self.enterContext(mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "synthetic", "POWERPACKS_USAGE_LOG": str(self.run_dir / "usage.jsonl")}))

    async def test_fixed_choices_and_the_chosen_department_is_cached(self):
        self.assertEqual({department.value for department in Department}, DEPARTMENTS)
        first = await Department.from_jd(self.jd, as_of=self.as_of, run_dir=self.run_dir)
        second = await Department.from_jd(self.jd, as_of=self.as_of, run_dir=self.run_dir)
        self.assertEqual((first, second), (Department("technical"), Department("technical")))
        self.assertEqual(len(self.calls), 1)
        request = self.calls[0]
        state = json.loads(request["input"])
        self.assertEqual((state["job_description"], state["reference_date"]), (self.jd, self.as_of))
        [question] = request["questions"]
        self.assertEqual((question["type"], question["name"]), ("choice", "department"))
        self.assertEqual({choice["value"] for choice in question["choices"]}, DEPARTMENTS)
        self.assertEqual(len(list(self.run_dir.rglob("luna/*.json"))), 1)

    async def test_changed_jd_gets_a_new_exact_request(self):
        await Department.from_jd(self.jd, as_of=self.as_of, run_dir=self.run_dir)
        await Department.from_jd("Finance analyst", as_of=self.as_of, run_dir=self.run_dir)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(list(self.run_dir.rglob("luna/*.json"))), 2)

    async def test_a_refusal_is_an_error_and_is_asked_again(self):
        self.refuse = True
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "declined"):
                await Department.from_jd(self.jd, as_of=self.as_of, run_dir=self.run_dir)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(list(self.run_dir.rglob("luna/*.json")), [])

    async def test_blank_jd_stops_before_a_request(self):
        with self.assertRaises(ValueError):
            await Department.from_jd("  ", as_of=self.as_of, run_dir=self.run_dir)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
