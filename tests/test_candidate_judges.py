import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.primitives.deep_search import candidate_judges as judges, search_harness as harness

DOMAIN = {"score": 4, "why": "Strong relevant work", "evidence": ["Built systems"], "concerns": []}
OPPORTUNITY = {"cap": 3, "why": "Scope tradeoff", "current_scope": "Manager",
               "target_scope": "IC", "company_context": "Smaller team", "missing_facts": []}


class CandidateJudgeTests(unittest.TestCase):
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
            profiles["p1"]["positions"][-1]["description"] = "Changed old role"
            run()
            self.assertEqual(len(calls), 4)
            with mock.patch.dict(judges.JUDGE_CONFIG, {"max_completion_tokens": 2501}):
                run()
            self.assertEqual(len(calls), 6)
        self.assertEqual(first[0]["score"], .8)
        self.assertEqual(first[0]["candidate_judgment"]["overall_score"], 3)
        self.assertIsNone(first[1]["candidate_judgment"])
        payload = json.loads(calls[0]["messages"][1]["content"])
        self.assertEqual(len(payload["candidate"]["positions"]), 12)
        self.assertEqual(payload["current_company"]["funding_date"], "2020-01-01")
        self.assertEqual(payload["hiring_company"]["headcount"], 10)
        self.assertEqual(calls[0]["model"], "gpt-5.6-terra")
        self.assertEqual(calls[0]["service_tier"], "flex")

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
