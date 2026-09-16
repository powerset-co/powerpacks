"""Cross-encoder boundary checks use a local mock transport; no paid calls."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from packs.search.primitives.llm_rerank_candidates import cross_encoder as ce


def response_for(body):
    pairs = body["pairs"]
    return {
        "model": "Qwen/Qwen3-Reranker-8B", "revision": "test-revision",
        "adapter": None, "score_type": "raw_yes_minus_no_logit",
        "scores": [{"id": pair["id"], "score": index / 2} for index, pair in enumerate(pairs)],
        "usage": {"pairs": len(pairs), "input_tokens": len(pairs) * 100,
                  "output_tokens": 0, "max_tokens": 100, "truncated": 0},
    }


class CrossEncoderTests(unittest.TestCase):
    def test_native_expected_ratings_keep_their_scale(self):
        def respond(request, body):
            payload = response_for(body)
            payload.update(model="google/gemma-4-12B-it", score_type="expected_rating_1_to_5")
            payload["scores"][0].update(score=2.25, score_1_to_5=2.25)
            return httpx.Response(200, json=payload)
        self.respond = respond
        result = self.score()
        self.assertEqual(result["score_type"], "expected_rating_1_to_5")
        self.assertEqual(result["scores"], [{"id": "person-1", "score": 2.25}])
        self.assertEqual(ce.score_1_to_5(2.25, score_type=result["score_type"]), 2.25)

    def test_five_point_scale_preserves_order_and_handles_extreme_logits(self):
        raw = [-1000.0, -2.5, 0.0, 2.5, 1000.0]
        expected = [1.0, 1.30343272, 3.0, 4.69656728, 5.0]
        for score, normalized in zip(raw, expected):
            self.assertAlmostEqual(ce.score_1_to_5(score), normalized)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        sleep_patch = patch("time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        self.requests = []
        self.respond = lambda request, body: httpx.Response(200, json=response_for(body))

        def handle(request):
            self.requests.append(request)
            return self.respond(request, json.loads(request.content))

        client = httpx.Client(transport=httpx.MockTransport(handle))
        self.client_patch = patch.object(httpx, "Client", return_value=client)
        self.client_constructor = self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def score(self, profiles=None, query="Backend engineer", **kwargs):
        return ce.score_candidates(
            query=query, profiles={"person-1": {"positions": []}} if profiles is None else profiles,
            output_dir=self.output, api_key="test-powerset-key", **kwargs,
        )

    def test_gateway_passage_matches_epoch_two_training_evidence(self):
        profile = {
            "person_id": "person-1", "headline": "Builds storage", "title": "Engineer",
            "company": "Example Systems", "summary": "Original résumé evidence",
            "name": "Jordan Bravo", "inferred_age": 40, "inferred_birth_year": 1986,
            "gender": "example", "birth_date": "1986-01-01", "years_of_experience": 15,
            "city": "Example City", "location": "Example City", "trait_scores": {"Engineer": 1},
            "positions": [{"position_title": f"Role {index}", "start_date": "2010-01-01",
                           "end_date": "null", "is_current": False, "dense_text": "Generated duties",
                           "description": "Backend systems", "company_description": "Data infrastructure",
                           "company_funding_total": 12345, "company_headcount": 100,
                           "company_stage": "SEED", "investor_names": ["Example Ventures"],
                           "company_name": "Example Systems"}
                          for index in range(12)],
            "education": [{"school_name": "Example University", "degree": "BS",
                           "start_year": 2004, "end_year": 2008}],
        }
        result = self.score({"person-1": profile})
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://proxy.powerset.dev/vendor/cross-encoder/rerank")
        self.assertEqual(request.headers["x-powerset-key"], "test-powerset-key")
        self.assertEqual(request.headers["x-ce-score-type"], "expected_rating_1_to_5")
        self.assertNotIn("authorization", request.headers)
        self.assertEqual(self.client_constructor.call_args.kwargs["timeout"], 600)
        # qlora-20260913-epoch2-v1 reference/lab/{ce_experiment,ce_expanded_data}.py.
        expected = {
            "headline": "Builds storage", "title": "Engineer", "company": "Example Systems",
            "summary": "Original résumé evidence",
            "education": [{"school_name": "Example University", "degree": "BS", "field_of_study": None}],
            "positions": [{"title": f"Role {index}", "company": "Example Systems", "start": "2010-01-01",
                           "is_current": False, "description": "Backend systems"} for index in range(12)],
            "companies": [{"company": "Example Systems", "company_description": "Data infrastructure",
                           "headcount": 100, "stage": "SEED", "funding_total": 12345,
                           "investors": ["Example Ventures"]}],
        }
        self.assertEqual(json.loads(request.content)["pairs"][0]["passage"],
                         json.dumps(expected, ensure_ascii=False, separators=(",", ":")))
        self.assertIn("inferred_age", profile)
        self.assertEqual(result["usage"]["input_tokens"], 100)

    def test_company_dedup_keeps_distinct_facts_and_position_alias_precedence(self):
        self.score({"p": {"positions": [
            {"title": "Engineer", "position_title": "Unused alias", "company": "Example",
             "start": " none ", "start_date": "2020", "headcount": 0, "company_headcount": 12},
            {"position_title": "Engineer", "company_name": "Example", "company_headcount": 0},
            {"title": "Engineer", "company": "Example", "headcount": 30, "investors": []},
            {},
        ]}})
        passage = json.loads(json.loads(self.requests[0].content)["pairs"][0]["passage"])
        self.assertEqual(passage, {
            "positions": [{"title": "Engineer", "company": "Example", "start": "2020"},
                          {"title": "Engineer", "company": "Example"},
                          {"title": "Engineer", "company": "Example"}, {}],
            "companies": [{"company": "Example", "headcount": 0},
                          {"company": "Example", "headcount": 30, "investors": []}, {}],
        })

    def test_environment_key(self):
        with patch.dict(os.environ, {"POWERSET_API_KEY": "env-test-key"}):
            ce.score_candidates(query="Engineer", profiles={"p": {}}, output_dir=self.output)
        self.assertEqual(self.requests[0].headers["x-powerset-key"], "env-test-key")

    def test_empty_profiles_need_no_key_or_client(self):
        with patch.dict(os.environ, {}, clear=True):
            result = ce.score_candidates(query="", profiles={}, output_dir=self.output)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["scores"], [])
        self.assertEqual(result["usage"]["pairs"], 0)
        self.client_constructor.assert_not_called()

    def test_missing_key_prevents_request(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(RuntimeError, "POWERSET_API_KEY"):
            ce.score_candidates(query="Engineer", profiles={"p": {}}, output_dir=self.output)
        self.assertEqual(self.requests, [])

    def test_batches_at_thousand_and_restores_input_order(self):
        def respond(request, body):
            payload = response_for(body)
            payload["scores"].reverse()
            return httpx.Response(200, json=payload)
        self.respond = respond
        profiles = {f"person-{index}": {} for index in range(1003)}
        result = self.score(profiles)
        self.assertEqual([len(json.loads(request.content)["pairs"]) for request in self.requests], [1000, 3])
        self.assertEqual([row["id"] for row in result["scores"]], list(profiles))
        self.assertEqual(result["usage"]["pairs"], 1003)
        self.assertEqual(result["usage"]["input_tokens"], 100300)
        self.assertEqual(result["requests"], 2)

    def test_exact_byte_limit_splits_without_truncating_unicode(self):
        profile = {"summary": "界" * 100}
        with patch.object(ce, "MAX_REQUEST_BYTES", 700):
            self.score({"a": profile, "b": profile})
        self.assertEqual(len(self.requests), 2)
        for request in self.requests:
            self.assertLessEqual(len(request.content), 700)
            self.assertEqual(json.loads(json.loads(request.content)["pairs"][0]["passage"]),
                             {**profile, "positions": [], "companies": []})

    def test_oversized_later_profile_fails_before_any_paid_calls(self):
        with self.assertRaisesRegex(ValueError, "131072"):
            self.score({"a": {}, "b": {"summary": "x" * 131073}})
        self.assertEqual(self.requests, [])

    def test_query_and_id_limits(self):
        for query in ("", "q" * 131073):
            with self.subTest(query_length=len(query)), self.assertRaises(ValueError):
                self.score(query=query)
        for person_id in ("", "x" * 257, 123):
            with self.subTest(person_id=str(person_id)[:10]), self.assertRaises(ValueError):
                self.score({person_id: {}})
        self.assertEqual(self.requests, [])

    def test_reuses_exact_cache_without_key_or_client(self):
        first = self.score()
        self.client_constructor.reset_mock()
        with patch.dict(os.environ, {}, clear=True):
            second = ce.score_candidates(query="Backend engineer", profiles={"person-1": {"positions": []}},
                                         output_dir=self.output)
        self.assertEqual(second["scores"], first["scores"])
        self.assertEqual(second["cached_batches"], 1)
        self.assertEqual(second["requests"], 0)
        self.client_constructor.assert_not_called()
        self.assertEqual(len(self.requests), 1)

    def test_query_changes_leave_paid_cache_intact(self):
        first = self.score()
        artifact = Path(first["artifacts"][0])
        original = artifact.read_bytes()
        # Each invocation creates and closes its own HTTP client.
        self.client_patch.stop()
        with patch.object(httpx.Client, "send", side_effect=lambda request, **kwargs:
                          httpx.Response(200, json=response_for(json.loads(request.content)), request=request)):
            changed = self.score(query="Growth engineer")
        self.assertEqual(artifact.read_bytes(), original)
        self.assertNotEqual(first["artifacts"], changed["artifacts"])

    def test_old_whole_profile_cache_is_preserved_and_not_reused(self):
        profile = {"person_id": "p", "headline": "Engineer", "positions": []}
        old_body = json.dumps({"pairs": [{"id": "p", "query": "Backend engineer", "passage":
            json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}]},
            ensure_ascii=False, separators=(",", ":")).encode()
        digest = hashlib.sha256(ce.ENDPOINT.encode() + b"\n" + old_body).hexdigest()
        cache = self.output / "cross_encoder" / f"{digest}.json"
        cache.parent.mkdir()
        cached = json.dumps(response_for(json.loads(old_body)))
        cache.write_text(cached)
        result = self.score({"p": profile})
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["cached_batches"], 0)
        self.assertNotIn(str(cache), result["artifacts"])
        self.assertEqual(cache.read_text(), cached)

    def test_new_score_contract_keeps_old_qwen_cache_separate(self):
        _, body = ce._batches("Backend engineer", {"person-1": {"positions": []}})[0]
        digest = hashlib.sha256(ce.ENDPOINT.encode() + b"\n" + body).hexdigest()
        cache = self.output / "cross_encoder" / f"{digest}.json"
        cache.parent.mkdir()
        cached = json.dumps(response_for(json.loads(body)))
        cache.write_text(cached)
        result = self.score()
        self.assertEqual(result["requests"], 1)
        self.assertNotIn(str(cache), result["artifacts"])
        self.assertEqual(cache.read_text(), cached)

    def test_invalid_response_is_redacted_and_not_cached(self):
        mutations = {
            "missing": lambda payload: payload["scores"].clear(),
            "wrong_id": lambda payload: payload["scores"][0].update(id="other"),
            "duplicate": lambda payload: payload["scores"].append(payload["scores"][0]),
            "nan": lambda payload: payload["scores"][0].update(score="NaN"),
            "boolean": lambda payload: payload["scores"][0].update(score=True),
            "usage_count": lambda payload: payload["usage"].update(pairs=2),
            "tokens": lambda payload: payload["usage"].update(input_tokens=-1),
            "truncated": lambda payload: payload["usage"].update(truncated=1),
            "unknown_score_type": lambda payload: payload.update(score_type="unknown"),
            "rating_below_one": lambda payload: payload.update(score_type="expected_rating_1_to_5"),
            "rating_above_five": lambda payload: (
                payload.update(score_type="expected_rating_1_to_5"), payload["scores"][0].update(score=5.1)),
        }
        self.client_patch.stop()
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                def send(request, **kwargs):
                    payload = response_for(json.loads(request.content))
                    mutate(payload)
                    return httpx.Response(200, json=payload, request=request)
                with patch.object(httpx.Client, "send", side_effect=send), self.assertRaisesRegex(
                    RuntimeError, "invalid response"
                ):
                    self.score()
        self.assertEqual(list(self.output.rglob("*.json")), [])

    def test_gateway_errors_retry_three_times_without_echoing_body(self):
        self.respond = lambda request, body: httpx.Response(502, text="secret-key private-person-data")
        with self.assertRaisesRegex(RuntimeError, "HTTP 502") as error:
            self.score()
        self.assertNotIn("private-person-data", str(error.exception))
        self.assertEqual(len(self.requests), 4)
        self.assertEqual([call.args for call in self.sleep.call_args_list], [(30,)] * 3)

    def test_gateway_retry_recovers_and_caches_success(self):
        self.respond = lambda request, body: (
            httpx.Response(503) if len(self.requests) < 4
            else httpx.Response(200, json=response_for(body)))
        first = self.score()
        second = self.score()
        self.assertEqual(first["requests"], 4)
        self.assertEqual(second["requests"], 0)
        self.assertEqual(second["cached_batches"], 1)
        self.assertEqual(len(self.requests), 4)

    def test_other_http_errors_do_not_retry(self):
        self.respond = lambda request, body: httpx.Response(401)
        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            self.score()
        self.assertEqual(len(self.requests), 1)
        self.sleep.assert_not_called()

    def test_timeout_is_redacted(self):
        def fail(request, body):
            raise httpx.ReadTimeout("secret-key private-person-data")
        self.respond = fail
        with self.assertRaisesRegex(RuntimeError, "request failed") as error:
            self.score()
        self.assertNotIn("private-person-data", str(error.exception))
        self.assertEqual(len(self.requests), 1)

    def test_rerun_reuses_completed_batch_after_later_failure(self):
        self.client_patch.stop()
        calls = []

        def send(request, **kwargs):
            body = json.loads(request.content)
            calls.append(body["pairs"][0]["id"])
            if 2 <= len(calls) <= 5:
                return httpx.Response(502, request=request)
            return httpx.Response(200, json=response_for(body), request=request)

        with patch.object(httpx.Client, "send", side_effect=send), patch.object(ce, "MAX_PAIRS", 1):
            with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                self.score({"a": {}, "b": {}})
            result = self.score({"a": {}, "b": {}})
        self.assertEqual(calls, ["a", "b", "b", "b", "b", "b"])
        self.assertEqual(result["cached_batches"], 1)
        self.assertEqual(result["requests"], 1)

    def test_unreadable_paid_cache_stops_without_resubmitting(self):
        result = self.score()
        artifact = Path(result["artifacts"][0])
        artifact.write_text("interrupted JSON", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "cache is unreadable"):
            self.score()
        self.assertEqual(artifact.read_text(), "interrupted JSON")
        self.assertEqual(len(self.requests), 1)

    def test_different_model_revisions_are_not_mixed(self):
        def respond(request, body):
            payload = response_for(body)
            payload["revision"] = f"revision-{len(self.requests)}"
            return httpx.Response(200, json=payload)
        self.respond = respond
        with patch.object(ce, "MAX_PAIRS", 1), self.assertRaisesRegex(RuntimeError, "model changed"):
            self.score({"a": {}, "b": {}})
        self.assertEqual(len(list(self.output.rglob("*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
