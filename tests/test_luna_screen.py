"""The JD screen: one Luna Decisions choice per candidate, cached per exact request."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from packs.ingestion.primitives.deep_context_v2 import decisions
from packs.search.primitives.llm_rerank_candidates import luna_screen

AS_OF = "2026-11-06"
JD = "Build and operate distributed storage systems."


def profile(name: str, positions: int = 2) -> dict[str, Any]:
    return {"headline": name, "positions": [
        {"title": f"Engineer {index}", "company": f"Company {index}", "start": f"{2000 + index}-01",
         "end": None if index == positions - 1 else f"{2001 + index}-01", "is_current": index == positions - 1}
        for index in range(positions)]}


class LunaScreenTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sent: list[dict[str, Any]] = []
        self.ratings: dict[str, dict[str, float] | None] = {}
        real = decisions.AsyncOpenAI

        def respond(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.sent.append(body)
            name = json.loads(body["input"][0]["content"][1]["text"])["profile"]["headline"]
            probabilities = self.ratings[name]
            if probabilities is None:
                return httpx.Response(200, json={"answers": [{"type": "refusal", "name": "capability"}]})
            choice = max(probabilities, key=lambda value: probabilities[value])
            return httpx.Response(200, json={"answers": [{
                "type": "choice", "name": "capability", "choice": choice,
                "probabilities": [{"value": value, "probability": probabilities.get(value, 0.0)}
                                  for value in luna_screen.RATINGS]}]})

        def client(**options: Any) -> Any:
            return real(**options, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))

        patches = [mock.patch.object(decisions, "AsyncOpenAI", client),
                   mock.patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic",
                                                "POWERPACKS_USAGE_LOG": str(self.root / "usage.jsonl")})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def screen(self, profiles: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return asyncio.run(luna_screen.score_candidates(jd=JD, profiles=profiles, output_dir=self.root, as_of=AS_OF))

    def test_score_is_the_chance_of_three_or_more_and_passes_at_the_threshold(self) -> None:
        self.ratings = {"Ada": {"4": 0.7, "3": 0.2, "2": 0.1}, "Ben": {"2": 0.7, "3": 0.3},
                        "Cy": {"1": 0.9, "3": 0.1}}
        result = self.screen({"a": profile("Ada"), "b": profile("Ben"), "c": profile("Cy")})
        scores = {row["id"]: row for row in result["scores"]}
        self.assertEqual((result["status"], result["score_type"], result["requests"]),
                         ("ok", "qualification_score", 3))
        self.assertEqual([scores[key]["score"] for key in "abc"], [0.9, 0.3, 0.1])
        self.assertEqual([scores[key]["passed"] for key in "abc"],
                         [True, 0.3 >= luna_screen.THRESHOLD, False])
        self.assertIn("Luna rating 4", scores["a"]["evidence"])

    def test_the_job_is_the_marked_cache_prefix_and_the_profile_follows(self) -> None:
        self.ratings = {"Ada": {"3": 1.0}, "Ben": {"3": 1.0}}
        self.screen({"a": profile("Ada"), "b": profile("Ben")})
        jobs = set()
        for body in self.sent:
            [message] = body["input"]
            job, candidate = message["content"]
            self.assertEqual(job["prompt_cache_breakpoint"], {"mode": "explicit"})
            self.assertNotIn("prompt_cache_breakpoint", candidate)
            self.assertEqual(json.loads(job["text"])["job"], JD)
            self.assertIn("profile", json.loads(candidate["text"]))
            self.assertEqual(body["prompt_cache_options"], {"mode": "explicit"})
            self.assertEqual((body["model"], body["questions"]), (decisions.MODEL, [luna_screen.QUESTION]))
            jobs.add(job["text"])
        self.assertEqual(len(jobs), 1)

    def test_answers_are_reused_and_a_refusal_never_passes_or_settles(self) -> None:
        self.ratings = {"Ada": {"4": 1.0}, "Ben": None}
        first = self.screen({"a": profile("Ada"), "b": profile("Ben")})
        refused = next(row for row in first["scores"] if row["id"] == "b")
        self.assertEqual((refused["score"], refused["passed"]), (0.0, False))
        second = self.screen({"a": profile("Ada"), "b": profile("Ben")})
        self.assertEqual((second["requests"], second["cached_candidates"]), (1, 1))
        self.assertEqual(len(self.sent), 3)

    def test_profiles_keep_their_most_recent_positions_only(self) -> None:
        request = luna_screen.build_request(jd=JD, profile=profile("Ada", positions=60), as_of=AS_OF)
        positions = json.loads(request["input"][0]["content"][1]["text"])["profile"]["positions"]
        self.assertEqual(len(positions), luna_screen.MAX_POSITIONS)
        self.assertEqual(positions[-1]["title"], "Engineer 59")

    def test_a_blank_jd_stops_before_a_request(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(luna_screen.score_candidates(jd=" ", profiles={"a": profile("Ada")},
                                                     output_dir=self.root, as_of=AS_OF))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
