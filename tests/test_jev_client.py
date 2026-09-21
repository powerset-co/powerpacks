from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.search.primitives.llm_rerank_candidates.jev import client as jev
from packs.search.primitives.llm_rerank_candidates.jev.questions import REQUEST_VERSION, base_questions


BASE_QUESTION_NAMES = (
    "function_match",
    "direct_execution",
    "coverage",
    "specialty",
    "transfer",
    "evidence_basis",
    "continuity",
    "historical_match",
    "repeated_practice",
    "relevant_leadership",
    "scope",
    "company_domain",
    "company_quality",
    "environment_fit",
    "funding_context",
    "independent_execution_quality",
    "education_relevance",
    "wrong_function",
)


class _Response:
    def __init__(self, status_code: int, payload: object = None, *, text: str = "", retry_after: str = "0"):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {"Retry-After": retry_after}

    def json(self) -> object:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class _Client:
    def __init__(self, *responses: _Response, pause: bool = False):
        self.responses = list(responses)
        self.pause = pause
        self.calls = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.closed = False

    async def post(self, endpoint: str, **kwargs) -> _Response:
        self.calls.append((endpoint, kwargs))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.pause:
                await asyncio.sleep(0.01)
            return self.responses.pop(0)
        finally:
            self.in_flight -= 1

    async def aclose(self) -> None:
        self.closed = True


def _answer(question: dict) -> dict:
    if question["type"] == "noul":
        return {"type": "noul", "noul": 0.6}
    options = list(question["criteria"] if question["type"] == "choice" else map(str, range(len(question["criteria"]))))
    remainder = 0.3 / (len(options) - 1)
    probabilities = {option: 0.7 if index == 0 else remainder for index, option in enumerate(options)}
    result = {"type": question["type"], "probabilities": probabilities}
    if question["type"] == "choice":
        result["choice"] = options[0]
    return result


def _payload(request: dict) -> dict:
    return {
        "model": jev.MODEL,
        "answers": {name: _answer(question) for name, question in request["questions"].items()},
        "usage": {"input_tokens": 2000, "output_tokens": 300},
    }


class JevClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.profile = {
            "headline": "Distributed systems engineer",
            "summary": "Built synthetic storage systems.",
            "positions": [
                {
                    "title": "Engineer",
                    "company": "Example Systems",
                    "start": "2022-01",
                    "is_current": True,
                    "description": "Owned recovery tooling.",
                    "company_description": "Generated context that supplied normalized context replaces.",
                },
                {
                    "title": "Developer",
                    "company": "Earlier Tools",
                    "start": {"year": 2017},
                    "end": "2019-10",
                    "description": "Built APIs.",
                },
            ],
            "companies": [
                {
                    "company": "Example Systems",
                    "company_description": "Supplied normalized company context.",
                    "stage": "series_a",
                    "funding_total": 1000000,
                    "funding_date": "2025-01-01",
                    "investors": ["Private Investor"],
                    "private_id": "company-secret",
                }
            ],
        }

    def _request(self, **overrides) -> dict:
        args = {"jd": "Own distributed storage reliability.", "profile": self.profile, "as_of": "2026-09-19"}
        args.update(overrides)
        return jev.build_request(**args)

    async def _score(self, client, **overrides) -> dict:
        args = {
            "jd": "Own distributed storage reliability.",
            "profiles": {"person-a": self.profile},
            "output_dir": self.output,
            "as_of": "2026-09-19",
            "api_key": "synthetic-key",
            "client": client,
        }
        args.update(overrides)
        with mock.patch.dict("os.environ", {"POWERPACKS_USAGE_LOG": str(self.output / "usage.jsonl")}):
            return await jev.score_candidates(**args)

    def test_request_matches_frozen_questions_rubric_and_role_dates(self) -> None:
        request = self._request()
        self.assertEqual(request["model"], "jev-1.13.0")
        self.assertEqual(tuple(base_questions()), BASE_QUESTION_NAMES)
        self.assertEqual(len(request["questions"]), 24)
        digest = hashlib.sha256(
            json.dumps(base_questions(), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(digest, "809bc6be0667af9b758af2b4fbcda56ecf8b0483741937b3550bee71288fa1b0")
        rubric = request["state"]["rating_rubric"]
        self.assertEqual(
            hashlib.sha256(rubric.replace("2026-09-19", "2026-09-16", 1).encode()).hexdigest(),
            "2c43b8fa6ef45083125591884aa2a0a9ff8d6833e033debac9a151635d8dee98",
        )
        company = request["state"]["profile"]["companies"][0]
        self.assertEqual(
            set(company),
            {"company", "company_description", "stage", "funding_total", "funding_date"},
        )
        self.assertEqual(company["funding_date"], "2025-01-01")
        self.assertNotIn("Private Investor", json.dumps(request))
        self.assertNotIn("company-secret", json.dumps(request))
        current, historical = request["state"]["roles"]
        self.assertEqual(current["dates"]["years_in_role"], 4)
        self.assertEqual(current["dates"]["recency"], "current")
        self.assertEqual(current["company_context"], [company])
        self.assertEqual(historical["dates"]["years_in_role"], 2)
        self.assertEqual(historical["dates"]["recency"], "ended_5plus_years_ago")

    async def test_success_scores_probability_and_records_paid_usage(self) -> None:
        request = self._request()
        api = _Client(_Response(200, _payload(request)))
        result = await self._score(api)

        self.assertEqual(result["model"], jev.MODEL)
        self.assertEqual(result["request_version"], REQUEST_VERSION)
        self.assertEqual(result["score_type"], "qualification_score")
        self.assertEqual(result["threshold"], jev.THRESHOLD)
        self.assertEqual(result["revision"], jev.MODEL_ASSET_SHA256)
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["paid_usage"], {"pairs": 1, "input_tokens": 2000, "output_tokens": 300})
        self.assertEqual(result["cached_usage"], {"pairs": 0, "input_tokens": 0, "output_tokens": 0})
        score = result["scores"][0]
        self.assertEqual(set(score), {"id", "score", "passed", "evidence", "threshold"})
        self.assertTrue(0 <= score["score"] <= 1)
        self.assertEqual(score["passed"], score["score"] >= jev.THRESHOLD)
        self.assertIn("Jev signals:", score["evidence"])
        endpoint, kwargs = api.calls[0]
        self.assertEqual(endpoint, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer synthetic-key")
        self.assertEqual(kwargs["json"], request)
        usage_row = json.loads((self.output / "usage.jsonl").read_text())
        self.assertEqual(usage_row["model"], jev.MODEL)
        self.assertEqual(usage_row["prompt_tokens"], 2000)
        self.assertAlmostEqual(usage_row["cost_usd"], 2000 * 0.042 / 1_000_000)
        self.assertEqual(usage_row["cost_basis"], "reported_input_tokens")

    async def test_exact_cache_reuses_without_key_and_separates_cached_usage(self) -> None:
        request = self._request()
        api = _Client(_Response(200, _payload(request)))
        first = await self._score(api)
        with mock.patch.dict("os.environ", {}, clear=True):
            second = await self._score(None, api_key=None)

        self.assertEqual(first["scores"], second["scores"])
        self.assertEqual(second["requests"], 0)
        self.assertEqual(second["cached_candidates"], 1)
        self.assertEqual(second["paid_usage"], {"pairs": 0, "input_tokens": 0, "output_tokens": 0})
        self.assertEqual(second["cached_usage"], {"pairs": 1, "input_tokens": 2000, "output_tokens": 300})
        saved = json.loads(Path(first["artifacts"][0]).read_text())
        self.assertEqual(saved["model_asset_sha256"], jev.MODEL_ASSET_SHA256)
        self.assertEqual(saved["request_version"], REQUEST_VERSION)
        self.assertEqual(saved["threshold"], jev.THRESHOLD)
        self.assertNotIn("synthetic-key", json.dumps(saved))
        self.assertEqual(len((self.output / "usage.jsonl").read_text().splitlines()), 1)

    async def test_request_binding_changes_for_date_job_and_profile(self) -> None:
        requests = [self._request() for _ in range(4)]
        api = _Client(*(_Response(200, _payload(request)) for request in requests))
        await self._score(api)
        await self._score(api, as_of="2026-09-20")
        await self._score(api, jd="Different cleaned job")
        changed = {**self.profile, "summary": "Different synthetic evidence."}
        await self._score(api, profiles={"person-a": changed})
        self.assertEqual(len(api.calls), 4)
        self.assertEqual(len(list((self.output / "jev").glob("*.json"))), 4)

    async def test_new_classifier_rescores_cached_answers_without_api_spend(self) -> None:
        request = self._request()
        await self._score(_Client(_Response(200, _payload(request))))
        with (
            mock.patch.object(jev, "predict", return_value=0.9),
            mock.patch.object(jev, "MODEL_ASSET_SHA256", "new-head-revision"),
            mock.patch.object(jev, "THRESHOLD", 0.8),
        ):
            result = await self._score(None, api_key=None)
        self.assertEqual(result["requests"], 0)
        self.assertEqual(result["revision"], "new-head-revision")
        self.assertEqual(result["scores"][0]["score"], 0.9)
        self.assertEqual(result["scores"][0]["threshold"], 0.8)
        self.assertTrue(result["scores"][0]["passed"])

    async def test_identical_profiles_are_scored_once(self) -> None:
        request = self._request()
        api = _Client(_Response(200, _payload(request)))
        result = await self._score(api, profiles={"person-a": self.profile, "person-b": self.profile})
        self.assertEqual([row["id"] for row in result["scores"]], ["person-a", "person-b"])
        self.assertEqual(len(api.calls), 1)

    async def test_concurrency_is_bounded_at_four(self) -> None:
        profiles = {f"person-{index}": {**self.profile, "summary": f"Synthetic {index}"} for index in range(8)}
        responses = [
            _Response(
                200,
                _payload(
                    jev.build_request(jd="Own distributed storage reliability.", profile=profile, as_of="2026-09-19")
                ),
            )
            for profile in profiles.values()
        ]
        api = _Client(*responses, pause=True)
        await self._score(api, profiles=profiles)
        self.assertEqual(api.max_in_flight, 4)
        with self.assertRaisesRegex(ValueError, "between 1 and 4"):
            await self._score(api, concurrency=5)

    async def test_transient_errors_retry_but_invalid_results_never_reject(self) -> None:
        request = self._request()
        api = _Client(_Response(503), _Response(200, _payload(request)))
        result = await self._score(api)
        self.assertEqual(result["requests"], 2)

        invalid = _payload(request)
        invalid["answers"].pop("function_match")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "remains unscored"):
                await self._score(_Client(_Response(200, invalid)), output_dir=Path(directory))
            checkpoints = list(Path(directory).rglob("*.json"))
            self.assertEqual(len(checkpoints), 1)
            with self.assertRaisesRegex(RuntimeError, "remains unscored"):
                await self._score(None, output_dir=Path(directory), api_key=None)

    async def test_malformed_paid_response_records_bounded_unknown_cost(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "malformed JSON"):
            await self._score(_Client(_Response(200, ValueError("bad JSON"), text="not-json")))

        usage_row = json.loads((self.output / "usage.jsonl").read_text())
        self.assertEqual(usage_row["prompt_tokens"], jev.MAX_INPUT_TOKENS)
        self.assertEqual(usage_row["cost_usd"], jev.MAX_UNKNOWN_CALL_COST_USD)
        self.assertEqual(usage_row["cost_basis"], "64000_input_token_upper_bound")
        with self.assertRaisesRegex(RuntimeError, "cached response is invalid"):
            await self._score(None, api_key=None)

    async def test_oversized_request_compacts_without_losing_feature_roles(self) -> None:
        request = self._request()
        api = _Client(
            _Response(503),
            _Response(503),
            _Response(400, text="max_tokens_exceeded"),
            _Response(200, _payload(request)),
        )
        result = await self._score(api)
        self.assertEqual(result["requests"], 4)
        compact = api.calls[3][1]["json"]
        self.assertEqual(compact["state"]["profile"], request["state"]["profile"])
        self.assertEqual(compact["state"]["roles"][0]["original_position_index"], 0)
        self.assertNotIn("original_role", compact["state"]["roles"][0])
        cached = await self._score(None, api_key=None)
        self.assertEqual(cached["requests"], 0)
        self.assertEqual(cached["scores"], result["scores"])

    def test_invalid_company_blocks_fail_before_any_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "companies must contain objects"):
            self._request(profile={**self.profile, "companies": ["not-an-object"]})


if __name__ == "__main__":
    unittest.main()
