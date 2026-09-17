import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from packs.search.primitives.llm_rerank_candidates import terra


def response(*, rating=3, content=None, finish_reason="stop", refusal=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(
            refusal=refusal, content=content if content is not None else json.dumps({
                "rating": rating, "evidence": "Built relevant systems.", "basis": "direct"})))],
        model="gpt-5.6-terra", service_tier="flex",
        usage=SimpleNamespace(model_dump=lambda: {
            "prompt_tokens": 2000, "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 1500, "cache_write_tokens": 100},
            "completion_tokens_details": {"reasoning_tokens": 30}}))


def client(*responses):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(side_effect=list(responses)))))


class TerraCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.profile = {"headline": "Engineer", "summary": "Original personal text.",
                        "positions": [{"title": "Engineer", "company": "Example Tools",
                                       "start": "2018", "end": "2020",
                                       "description": "Built original systems."}]}

    async def score(self, api, **overrides):
        args = dict(jd="Job: Engineer at Example\nBuild systems.", profiles={"person-a": self.profile},
                    output_dir=self.output, as_of="2026-09-17", client=api)
        args.update(overrides)
        return await terra.score_candidates(**args)

    def test_prompt_matches_recommended_gist_version_exactly(self):
        self.assertEqual(hashlib.sha256(terra.system_prompt("2026-09-16").encode()).hexdigest(),
                         "2c43b8fa6ef45083125591884aa2a0a9ff8d6833e033debac9a151635d8dee98")

    async def test_one_candidate_per_call_and_exact_configuration(self):
        api = client(response(rating=4), response(rating=2))
        result = await self.score(api, profiles={"person-a": self.profile,
                                                "person-b": {**self.profile, "summary": "Other evidence."}})
        self.assertEqual([row["score"] for row in result["scores"]], [4, 2])
        self.assertEqual(result["score_type"], "ordinal_rating_1_to_5")
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["usage"]["cached_tokens"], 3000)
        self.assertEqual(result["usage"]["cache_write_tokens"], 200)
        self.assertEqual(result["usage"]["reasoning_tokens"], 60)
        calls = [call.kwargs for call in api.chat.completions.create.call_args_list]
        for request in calls:
            self.assertEqual(request["model"], "gpt-5.6-terra")
            self.assertEqual(request["reasoning_effort"], "high")
            self.assertEqual(request["service_tier"], "flex")
            self.assertEqual(request["max_completion_tokens"], 8192)
            self.assertIs(request["store"], False)
            self.assertTrue(request["response_format"]["json_schema"]["strict"])
            self.assertNotIn("temperature", request)
            self.assertEqual(request["extra_body"], {"prompt_cache_options": {"mode": "explicit"}})
            parts = request["messages"][1]["content"]
            self.assertEqual(parts[0]["prompt_cache_breakpoint"], {"mode": "explicit"})
            self.assertNotIn("person-a", json.dumps(request))
            self.assertNotIn("person-b", json.dumps(request))
        self.assertEqual(calls[0]["messages"][0], calls[1]["messages"][0])
        self.assertEqual(calls[0]["messages"][1]["content"][0], calls[1]["messages"][1]["content"][0])

    async def test_exact_request_cache_reuses_and_invalidates(self):
        api = client(*[response() for _ in range(6)])
        first = await self.score(api)
        second = await self.score(api)
        self.assertEqual(first["scores"], second["scores"])
        self.assertEqual(second["requests"], 0)
        self.assertEqual(second["cached_candidates"], 1)
        await self.score(api, as_of="2026-09-18")
        await self.score(api, jd="Different job")
        await self.score(api, profiles={"person-a": {**self.profile, "summary": "New evidence"}})
        with patch.object(terra, "MODEL", "other-model"):
            await self.score(api)
        with patch.object(terra, "system_prompt", return_value="Changed rubric"):
            await self.score(api)
        self.assertEqual(api.chat.completions.create.await_count, 6)
        artifact = json.loads(Path(first["artifacts"][0]).read_text())
        self.assertEqual(artifact["assessment_date"], "2026-09-17")
        self.assertEqual(artifact["service_tier"], "flex")
        self.assertEqual(artifact["prompt_sha256"], hashlib.sha256(
            terra.system_prompt("2026-09-17").encode()).hexdigest())

    async def test_identical_evidence_is_scored_once_without_cache_write_race(self):
        api = client(response(rating=4))
        result = await self.score(api, profiles={"person-a": self.profile, "person-b": self.profile})
        self.assertEqual([row["id"] for row in result["scores"]], ["person-a", "person-b"])
        self.assertEqual([row["score"] for row in result["scores"]], [4, 4])
        self.assertEqual(api.chat.completions.create.await_count, 1)

    async def test_errors_never_become_negative_scores_or_cache_entries(self):
        cases = [response(content="bad json"), response(content="[]"), response(rating=True),
                 response(rating=2.5), response(rating=6), response(finish_reason="length"),
                 response(refusal="Refused"), response(content='{"rating":3}'),
                 response(content='{"rating":3,"basis":"invented","evidence":"Reason"}'),
                 response(content='{"rating":3,"basis":"direct","evidence":" "}'),
                 SimpleNamespace(choices=[]), TimeoutError("timeout")]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises((RuntimeError, TimeoutError)):
                    await self.score(client(case))
                self.assertEqual(list(self.output.rglob("*.json")), [])

    async def test_successes_survive_another_candidates_failure(self):
        api = client(response(rating=4), response(content="broken"))
        profiles = {"person-a": self.profile, "person-b": {**self.profile, "summary": "Different"}}
        with self.assertRaises(RuntimeError):
            await self.score(api, profiles=profiles, concurrency=1)
        self.assertEqual(len(list(self.output.rglob("*.json"))), 1)
        retry = client(response(rating=3))
        result = await self.score(retry, profiles=profiles)
        self.assertEqual([row["score"] for row in result["scores"]], [4, 3])
        self.assertEqual(retry.chat.completions.create.await_count, 1)

    async def test_empty_input_does_not_create_client(self):
        with patch.object(terra, "make_async_openai_client") as factory:
            result = await self.score(None, profiles={})
        factory.assert_not_called()
        self.assertEqual(result["scores"], [])

    async def test_completed_cache_needs_no_api_credentials(self):
        await self.score(client(response()))
        with patch.object(terra, "make_async_openai_client") as factory:
            result = await self.score(None)
        factory.assert_not_called()
        self.assertEqual(result["cached_candidates"], 1)

    async def test_owned_client_uses_usage_capture_without_automatic_retries(self):
        api = client(response())
        api.close = AsyncMock()
        with patch.object(terra, "make_async_openai_client", return_value=api) as factory:
            await self.score(None, api_key="synthetic-test-key")
        factory.assert_called_once_with(api_key="synthetic-test-key", timeout=600, max_retries=0)
        api.close.assert_awaited_once()

    def test_profile_preserves_all_personal_text_without_ids_or_investors(self):
        original = "Original work description " * 1000
        position = {**self.profile["positions"][0], "description": original,
                    "company_description": "First sentence. Second sentence! Third sentence.",
                    "investor_names": ["Private Investor"], "company_funding_total": 10000,
                    "company_funding_date": "2023-02-01"}
        profile = {**self.profile, "title": "redundant", "company": "redundant",
                   "id": "private-id", "positions": [position] * 12}
        request = terra.build_request(jd="Cleaned JD", profile=profile, as_of="2026-09-17")
        text = request["messages"][1]["content"][1]["text"]
        evidence = json.loads(text.split("<Profile>\n", 1)[1].split("\n</Profile>", 1)[0])
        self.assertEqual(set(evidence), {"headline", "summary", "positions", "companies"})
        self.assertEqual(evidence["summary"], self.profile["summary"])
        self.assertEqual(len(evidence["positions"]), 12)
        self.assertTrue(all(row["description"] == original for row in evidence["positions"]))
        self.assertEqual(len(evidence["companies"]), 1)
        self.assertEqual(evidence["companies"][0]["company_description"], "First sentence. Second sentence!")
        self.assertEqual(evidence["companies"][0]["funding_date"], "2023-02-01")
        self.assertNotIn("Private Investor", text)
        self.assertNotIn("private-id", text)

    def test_company_description_is_exact_prefix_at_word_boundary(self):
        description = "abcdefghij " * 100
        profile = {"positions": [{"company": "Example", "company_description": description}]}
        evidence = terra._profile_evidence(profile)
        excerpt = evidence["companies"][0]["company_description"]
        self.assertLessEqual(len(excerpt), 800)
        self.assertTrue(description.startswith(excerpt))
        self.assertEqual(excerpt[-10:], "abcdefghij")


if __name__ == "__main__":
    unittest.main()
