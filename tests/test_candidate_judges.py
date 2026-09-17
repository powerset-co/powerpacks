import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from openai import OpenAI

from packs.search.primitives.deep_search import candidate_judges as judges, search_harness as harness
from packs.search.primitives.deep_search.results_web.model import _candidate_judgment

DOMAIN = {"score": 4, "why": "Strong relevant work", "evidence": ["Built systems"], "concerns": []}
OPPORTUNITY = {"cap": 3, "why": "Scope tradeoff", "current_scope": "Manager",
               "target_scope": "IC", "company_context": "Smaller team", "missing_facts": []}


class CandidateJudgeTests(unittest.TestCase):
    def test_mixed_requests_preserve_original_terra_and_guide_only_luna(self):
        inputs = dict(jd="Synthetic JD", candidate={"headline": "Jordan Bravo"},
                      hiring_company={}, pond_query="Engineers", as_of="2026-09-16")
        domain = judges.candidate_judge_request(dimension="domain", **inputs)
        luna = judges.candidate_judge_request(dimension="opportunity", **inputs)
        review = judges.candidate_judge_request(dimension="opportunity", opportunity_review=True, **inputs)
        original = judges.candidate_judge_messages(dimension="opportunity", **inputs)
        self.assertEqual(domain["model"], "gpt-5.6-terra")
        self.assertEqual(domain["reasoning_effort"], "medium")
        self.assertEqual(domain["messages"], judges.candidate_judge_messages(dimension="domain", **inputs))
        self.assertEqual(review, {**judges.JUDGE_CONFIG, "messages": original})
        self.assertEqual(luna["model"], "gpt-5.6-luna")
        self.assertEqual(luna["reasoning_effort"], "low")
        self.assertEqual(luna["messages"][0]["content"], original[0]["content"]
                         + "\n\n" + judges.JUDGE_GUIDANCE + "\n\n" + judges.OPPORTUNITY_GUIDANCE)
        self.assertEqual(luna["messages"][1:], original[1:])
        schema = luna["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertEqual(schema["schema"]["properties"]["cap"]["enum"], [2, 3, 5])
        self.assertFalse(schema["schema"]["additionalProperties"])

    def test_only_luna_cap_two_gets_independent_terra_review_and_resumes(self):
        for cap, reviewed_cap in ((2, 2), (2, 3), (2, 5), (3, None), (5, None)):
            with self.subTest(cap=cap, reviewed_cap=reviewed_cap):
                calls = []
                async def create(**kwargs):
                    calls.append(kwargs)
                    if "qualifications for" in kwargs["messages"][0]["content"]:
                        answer = DOMAIN
                    elif kwargs["model"] == "gpt-5.6-luna":
                        answer = {**OPPORTUNITY, "cap": cap, "why": "Initial Luna reason"}
                    else:
                        answer = {**OPPORTUNITY, "cap": reviewed_cap, "why": "Independent Terra reason"}
                    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
                        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(answer)))])
                client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                with tempfile.TemporaryDirectory() as raw, mock.patch.object(harness, "_save"), mock.patch.object(harness, "_price_usage_log"):
                    run_dir, results = Path(raw), {"created_at": "2026-09-16"}
                    (run_dir / "jd.txt").write_text("Synthetic JD")
                    def run():
                        return harness._annotate_candidate_judgments(candidates=[{
                            "person": "p1", "cross_encoder_status": "ok", "cross_encoder_score_1_to_5": 3,
                            "candidate_judgment": {"overall_score": 2, "opportunity": {"why": "Prior vote"}}}],
                            profiles={"p1": {"headline": "Engineer"}}, results=results, run_dir=run_dir,
                            pond_n=1, context={}, pond_query="Engineers", client=client)
                    judgment = run()[0]["candidate_judgment"]
                    self.assertEqual(judgment, run()[0]["candidate_judgment"])
                    self.assertEqual(len(calls), 3 if cap == 2 else 2)
                    self.assertEqual(judgment["opportunity_initial"]["cap"], cap)
                    self.assertEqual(judgment["overall_score"], min(4, reviewed_cap if cap == 2 else cap))
                    self.assertEqual(judgment["models"]["opportunity"], "gpt-5.6-terra" if cap == 2 else "gpt-5.6-luna")
                    viewer = _candidate_judgment(judgment)
                    self.assertEqual(viewer.model, "gpt-5.6-terra + gpt-5.6-luna")
                    self.assertEqual(viewer.overall_score, judgment["overall_score"])
                    self.assertEqual(viewer.opportunity_reason, judgment["opportunity"]["why"])
                    records = results["raw_model_responses"][0]["checkpoints"]
                    self.assertTrue(all(record["cached"] for record in records))
                    self.assertTrue(all(record["usage"]["input_tokens"] == 100 for record in records))
                    if cap == 2:
                        self.assertEqual(judgment["opportunity"]["why"], "Independent Terra reason")
                        self.assertEqual(judgment["opportunity_review"], judgment["opportunity"])
                        self.assertEqual(calls[-1]["messages"], judges.candidate_judge_messages(
                            dimension="opportunity", jd="Synthetic JD", candidate={"headline": "Engineer"},
                            hiring_company={}, pond_query="Engineers", as_of="2026-09-16"))
                        self.assertNotIn("Prior vote", json.dumps(calls[-1]))

    def test_failed_or_truncated_review_leaves_unscored_not_luna_two(self):
        for failure in ("invalid", "length", "exception"):
            with self.subTest(failure=failure):
                async def create(**kwargs):
                    domain = "qualifications for" in kwargs["messages"][0]["content"]
                    review = not domain and kwargs["model"] == "gpt-5.6-terra"
                    if review and failure == "exception":
                        raise TimeoutError("Synthetic review failure")
                    answer = DOMAIN if domain else {**OPPORTUNITY, "cap": 2}
                    return SimpleNamespace(usage=SimpleNamespace(), choices=[SimpleNamespace(
                        finish_reason="length" if review and failure == "length" else "stop",
                        message=SimpleNamespace(content="{}" if review and failure == "invalid" else json.dumps(answer)))])
                client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                with tempfile.TemporaryDirectory() as raw, mock.patch.object(harness, "_save"), mock.patch.object(harness, "_price_usage_log"):
                    run_dir = Path(raw)
                    (run_dir / "jd.txt").write_text("Synthetic JD")
                    judgment = harness._annotate_candidate_judgments(candidates=[{
                        "person": "p1", "cross_encoder_status": "ok", "cross_encoder_score_1_to_5": 3}],
                        profiles={}, results={"created_at": "2026-09-16"}, run_dir=run_dir, pond_n=1,
                        context={}, pond_query="Engineers", client=client)[0]["candidate_judgment"]
                self.assertEqual(judgment["status"], "error")
                self.assertIsNone(judgment["overall_score"])
                self.assertIsNone(judgment["opportunity"])
                self.assertEqual(judgment["opportunity_initial"]["cap"], 2)

    def test_sdk_sends_explicit_cache_options_and_breakpoint(self):
        sent = []

        def respond(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion",
                "created": 0, "model": "gpt-5.6-terra", "choices": [{"index": 0,
                    "finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}]})

        messages = judges.candidate_judge_messages(dimension="domain", jd="Synthetic JD",
            candidate={"headline": "Jordan Bravo"}, hiring_company={}, pond_query="Engineers")
        with OpenAI(api_key="synthetic-test-key", http_client=httpx.Client(
                transport=httpx.MockTransport(respond))) as client:
            client.chat.completions.create(**judges.JUDGE_CONFIG, messages=messages)
        self.assertEqual(sent[0]["prompt_cache_options"], {"mode": "explicit"})
        self.assertEqual(sent[0]["messages"], messages)

    def test_shared_prefix_excludes_candidate_and_current_company(self):
        shared = {"jd": "Synthetic JD", "pond_query": "Engineers", "target_level": "IC",
                  "comp_band": {"min": 150000}, "as_of": "2026-09-16",
                  "hiring_company": {"headcount": 10, "funding": 500000, "pull_note": "Internal"}}
        for dimension in ("domain", "opportunity"):
            with self.subTest(dimension=dimension):
                first = judges.candidate_judge_messages(dimension=dimension, **shared, candidate={
                    "headline": "Jordan Bravo", "current_company_headcount": 200})
                second = judges.candidate_judge_messages(dimension=dimension, **shared, candidate={
                    "headline": "Casey Bravo", "current_company_headcount": 300})
                self.assertEqual(first[0], second[0])
                prefix, suffix = first[1]["content"]
                self.assertEqual(json.dumps(prefix).encode(), json.dumps(second[1]["content"][0]).encode())
                self.assertEqual(prefix["prompt_cache_breakpoint"], {"mode": "explicit"})
                self.assertNotIn("prompt_cache_breakpoint", suffix)
                self.assertNotEqual(suffix, second[1]["content"][1])
                self.assertEqual(json.loads(prefix["text"]), {
                    "as_of": shared["as_of"], "job_description": shared["jd"],
                    "pond_query": shared["pond_query"], "target_level": shared["target_level"],
                    "comp_band": shared["comp_band"],
                    "hiring_company": {"headcount": 10, "funding": 500000}})
                self.assertEqual(json.loads(suffix["text"])["current_company"], {"headcount": 200})

    def test_single_candidate_runs_both_dimensions_concurrently(self):
        started = []
        both_started = asyncio.Event()

        async def create(**kwargs):
            started.append(kwargs)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=.5)
            answer = DOMAIN if "qualifications for" in kwargs["messages"][0]["content"] else OPPORTUNITY
            return SimpleNamespace(usage=SimpleNamespace(), choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(answer)))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with (tempfile.TemporaryDirectory() as raw, mock.patch.object(harness, "_save"),
              mock.patch.object(harness, "_price_usage_log"),
              mock.patch.object(harness, "CANDIDATE_JUDGE_CONCURRENCY", 2)):
            run_dir = Path(raw)
            (run_dir / "jd.txt").write_text("Synthetic JD")
            rows = harness._annotate_candidate_judgments(candidates=[{
                "person": "p1", "cross_encoder_status": "ok", "cross_encoder_score_1_to_5": 3}],
                profiles={}, results={"created_at": "2026-09-15"}, run_dir=run_dir,
                pond_n=1, context={}, pond_query="Engineers", client=client)
        self.assertEqual(rows[0]["candidate_judgment"]["status"], "ok")
        self.assertEqual(rows[0]["candidate_judgment"]["overall_score"], 3)
        self.assertEqual({call["model"] for call in started}, {"gpt-5.6-terra", "gpt-5.6-luna"})
        self.assertEqual(started[0]["messages"][1:], started[1]["messages"][1:])

    def test_partial_failure_retains_domain_and_never_invents_overall(self):
        async def create(**kwargs):
            if "qualifications for" not in kwargs["messages"][0]["content"]:
                raise TimeoutError("Synthetic failure")
            return SimpleNamespace(usage=SimpleNamespace(), choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(DOMAIN)))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(harness, "_save"), mock.patch.object(harness, "_price_usage_log"):
            run_dir = Path(raw)
            (run_dir / "jd.txt").write_text("Synthetic JD")
            rows = harness._annotate_candidate_judgments(candidates=[{
                "person": "p1", "cross_encoder_status": "ok", "cross_encoder_score_1_to_5": 3}],
                profiles={}, results={"created_at": "2026-09-15"}, run_dir=run_dir,
                pond_n=1, context={}, pond_query="Engineers", client=client)
        judgment = rows[0]["candidate_judgment"]
        self.assertEqual(judgment["domain"], DOMAIN)
        self.assertIsNone(judgment["opportunity"])
        self.assertIsNone(judgment["overall_score"])
        self.assertEqual(judgment["status"], "error")

    def test_native_ce_gate_and_strict_opportunity_caps(self):
        for score, eligible in ((1, False), (2.99, False), (3, True), (4.5, True), (float("nan"), False)):
            self.assertEqual(harness._candidate_judgment_eligible({
                "cross_encoder_status": "ok", "cross_encoder_score_1_to_5": score}), eligible)
        for cap in (True, 3.0, "3", 1, 4, None):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                judges.parse_candidate_judge(json.dumps({**OPPORTUNITY, "cap": cap}), "opportunity")

    def test_two_calls_resume_and_preserve_full_evidence_and_scores(self):
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            answer = DOMAIN if "qualifications for" in kwargs["messages"][0]["content"] else OPPORTUNITY
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
                                   choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer)))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        candidates = [{"person": "p1", "score": .8, "cross_encoder_status": "ok",
                       "cross_encoder_score_1_to_5": 3, "current_company_funding_date": "2020-01-01"},
                      {"person": "p2", "cross_encoder_status": "ok", "cross_encoder_score_1_to_5": 2.9}]
        profiles = {"p1": {"positions": [{"title": "Engineer", "description": f"Original role {i}"}
                                        for i in range(12)]}}
        results = {"created_at": "2026-09-15", "hiring_company_context": {"headcount": 10, "funding": 500000}}
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(harness, "_save"), mock.patch.object(harness, "_price_usage_log"):
            run_dir = Path(raw)
            (run_dir / "jd.txt").write_text("Full synthetic job description")
            def run():
                return harness._annotate_candidate_judgments(candidates=candidates, profiles=profiles,
                    results=results, run_dir=run_dir, pond_n=1, context={}, pond_query="Engineers", client=client)
            first = run()
            self.assertEqual(first, run())
            self.assertEqual(len(calls), 2)
            candidates.reverse()
            self.assertEqual(run(), list(reversed(first)))
            self.assertEqual(len(calls), 2)
            candidates.reverse()
            profiles["p1"]["positions"][-1]["description"] = "Changed old role"
            run()
            self.assertEqual(len(calls), 4)
            with mock.patch.dict(judges.JUDGE_CONFIG, {"max_completion_tokens": 2501}):
                run()
            self.assertEqual(len(calls), 6)
        self.assertEqual(first[0]["score"], .8)
        self.assertEqual(first[0]["candidate_judgment"]["overall_score"], 3)
        self.assertIsNone(first[1]["candidate_judgment"])
        prefix, suffix = calls[0]["messages"][1]["content"]
        payload = {**json.loads(prefix["text"]), **json.loads(suffix["text"])}
        self.assertEqual(len(payload["candidate"]["positions"]), 12)
        self.assertEqual(payload["current_company"]["funding_date"], "2020-01-01")
        self.assertEqual(payload["hiring_company"]["headcount"], 10)
        self.assertEqual(calls[0]["model"], "gpt-5.6-terra")
        self.assertEqual(calls[0]["service_tier"], "flex")
        self.assertEqual(calls[0]["extra_body"], {"prompt_cache_options": {"mode": "explicit"}})

    def test_bad_response_cached_without_fake_score_or_rebilling(self):
        completion = mock.AsyncMock(return_value=SimpleNamespace(usage=SimpleNamespace(),
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"score":4.5}'))]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
        candidate = {"person": "p1", "score": .9, "cross_encoder_status": "ok", "cross_encoder_score_1_to_5": 3}
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(harness, "_save"), mock.patch.object(harness, "_price_usage_log"):
            run_dir = Path(raw)
            (run_dir / "jd.txt").write_text("Synthetic JD")
            for _ in range(2):
                rows = harness._annotate_candidate_judgments(candidates=[candidate], profiles={},
                    results={"created_at": "2026-09-15"}, run_dir=run_dir, pond_n=1,
                    context={}, pond_query="Engineers", client=client)
            self.assertEqual(completion.await_count, 2)
        self.assertEqual(rows[0]["candidate_judgment"]["status"], "error")
        self.assertIsNone(rows[0]["candidate_judgment"]["overall_score"])
        self.assertEqual(rows[0]["score"], .9)

    def test_skips_ce_failure_or_missing_without_client(self):
        candidates = [{"person": "p1", "cross_encoder_score_1_to_5": 5, "cross_encoder_status": "failed"}]
        with mock.patch.object(harness, "make_async_openai_client") as client:
            rows = harness._annotate_candidate_judgments(candidates=candidates, profiles={}, results={},
                run_dir=Path("unused"), pond_n=1, context={}, pond_query="Engineer")
        client.assert_not_called()
        self.assertIsNone(rows[0]["candidate_judgment"])
