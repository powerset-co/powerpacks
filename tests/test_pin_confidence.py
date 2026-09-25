import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from packs.search.primitives.deep_search import pin_confidence as pc, search_harness as harness
from packs.search.primitives.deep_search.results_web import rendering
from packs.search.primitives.deep_search.results_web.model import (
    Candidate, CandidateJudgment, PinJudgment, _pin_confidence, _pin_judgment, _taste_score,
)

VERDICT = {"decision": "introduce", "priority": 88, "reason": "Built the serving control plane at scale.",
           "inference": "none", "unresolved": [], "evidence": []}
CANDIDATES = [
    {"person": "p1", "linkedin_url": "https://linkedin.com/in/Jordan-Bravo/", "candidate_judgment": {"overall_score": 5}},
    {"person": "p2", "linkedin_url": "https://www.linkedin.com/in/casey-c?x=1", "candidate_judgment": {"overall_score": 3}},
    {"person": "p3", "linkedin_url": "https://www.linkedin.com/in/unjudged", "candidate_judgment": None},
]
KEYS = {"POWERSET_API_KEY": "ps", "OPENAI_API_KEY": "oa"}
JUDGED = CandidateJudgment(4, 5, 4, "Strong work", "Fine", "", "gpt-5.6-terra + gpt-5.6-luna", "ok")


def _candidate(**fields) -> Candidate:
    base = dict(person_id="p1", name="Jordan Bravo", linkedin_url="", title="", company="", location="",
                avatar_url="", move_likelihood=None, why="", found_run="r", found_pond=1, found_query="q",
                queries=(), ponds=())
    return Candidate(**{**base, **fields})


def _judge_client(calls: list, *, content: str | None = None, fail: bool = False):
    async def create(**kwargs):
        calls.append(kwargs)
        if fail:
            raise RuntimeError("provider down")
        return SimpleNamespace(model=pc.PIN_JUDGE_MODEL, usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
                               choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                                   content=content if content is not None else "```json\n" + json.dumps(VERDICT) + "\n```"))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _http(*, taste_status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "reporting.powerset.co":
            if taste_status != 200:
                return httpx.Response(taste_status, json={"error": "The request could not be completed."})
            urls = request.url.params.get_list("linkedin_url")
            return httpx.Response(200, json={"scoring_mode": "employee", "results": [
                {"linkedin_url": url, "status": "scored" if url.endswith("/jordan-bravo") else "not_scored",
                 "score": 7.58 if url.endswith("/jordan-bravo") else None, "scored_at": None} for url in urls]})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class PinConfidenceHelperTests(unittest.TestCase):
    def test_canonical_linkedin_matches_reporting_form(self):
        self.assertEqual(pc.canonical_linkedin("https://linkedin.com/in/Jordan-Bravo/"), "https://www.linkedin.com/in/jordan-bravo")
        self.assertEqual(pc.canonical_linkedin("http://www.linkedin.com/in/casey-c?trk=1"), "https://www.linkedin.com/in/casey-c")
        self.assertIsNone(pc.canonical_linkedin(""))
        self.assertIsNone(pc.canonical_linkedin("https://example.com/jordan"))

    def test_parse_taste_keeps_not_scored_as_none_and_rejects_bad_scores(self):
        parsed = pc.parse_taste({"results": [
            {"linkedin_url": "a", "status": "scored", "score": 7.58},
            {"linkedin_url": "b", "status": "not_scored", "score": None}]})
        self.assertEqual(parsed, {"a": 7.58, "b": None})
        with self.assertRaises(ValueError):
            pc.parse_taste({"results": [{"linkedin_url": "a", "status": "scored", "score": "high"}]})

    def test_parse_judgment_accepts_fenced_json_and_rejects_bad_shapes(self):
        self.assertEqual(pc.parse_judgment("```json\n" + json.dumps(VERDICT) + "\n```"),
                         {"decision": "introduce", "priority": 88, "reason": VERDICT["reason"]})
        for bad in ({**VERDICT, "priority": True}, {**VERDICT, "priority": 101}, {**VERDICT, "decision": "pin"},
                    {**VERDICT, "reason": " "}, [VERDICT]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pc.parse_judgment(json.dumps(bad))

    def test_requests_use_sol_low_on_the_measured_prompt_without_labels(self):
        state = pc.candidate_state(jd="Synthetic JD", profile={"headline": "Engineer"},
                                   candidate={"person": "p1", "candidate_judgment": {"overall_score": 5},
                                              "current_company_headcount": 24, "current_company_stage": None},
                                   hiring_company={"name": "Acme", "pull_note": "x", "funding": None}, as_of="2026-09-24")
        self.assertEqual(state["current_company_context"], {"headcount": 24})
        self.assertEqual(state["hiring_company_context"], {"name": "Acme"})
        self.assertNotIn("candidate_judgment", json.dumps(state))
        request = pc.judge_request(state)
        self.assertEqual((request["model"], request["reasoning_effort"], request["response_format"]),
                         ("gpt-6-sol", "low", {"type": "json_object"}))
        self.assertIn("Would you confidently introduce this person", request["messages"][0]["content"])
        self.assertEqual(json.loads(request["messages"][1]["content"]), state)

    def test_viewer_parsers_validate_the_two_fields(self):
        self.assertEqual(_taste_score(7.58), 7.58)
        self.assertIsNone(_taste_score(None))
        self.assertEqual(_pin_confidence(88), 88)
        self.assertIsNone(_pin_confidence(None))
        for bad in (True, "7"):
            with self.assertRaises(ValueError):
                _taste_score(bad)
        for bad in (True, 101, 4.5):
            with self.assertRaises(ValueError):
                _pin_confidence(bad)
        judgment = _pin_judgment({"model": pc.PIN_JUDGE_MODEL, "decision": "review", "reason": "Data work.", "status": "ok"})
        self.assertEqual((judgment.decision, judgment.reason, judgment.status), ("review", "Data work.", "ok"))
        with self.assertRaises(ValueError):
            _pin_judgment({"model": pc.PIN_JUDGE_MODEL, "decision": "pin", "reason": "", "status": "ok"})

    def test_viewer_badges_follow_the_judge_decision_and_taste_presence(self):
        introduce = PinJudgment("introduce", "Built it.", pc.PIN_JUDGE_MODEL, "ok")
        review = PinJudgment("review", "Data work.", pc.PIN_JUDGE_MODEL, "ok")
        suggested = _candidate(candidate_judgment=JUDGED, pin_confidence=88, pin_judgment=introduce, taste_score=7.58)
        self.assertTrue(suggested.suggested_pin)
        self.assertIn("Suggested Pin", rendering._suggested_pin_chip(suggested))
        self.assertIn("Pin confidence 88/100", rendering._suggested_pin_chip(suggested))
        self.assertIn("Taste 7.6", rendering._taste_badge(suggested))
        self.assertEqual(rendering._judge_badges(suggested),
                         rendering._taste_badge(suggested) + rendering._suggested_pin_chip(suggested))
        reviewed = _candidate(candidate_judgment=JUDGED, pin_confidence=60, pin_judgment=review, taste_score=None)
        self.assertFalse(reviewed.suggested_pin)
        self.assertEqual(rendering._suggested_pin_chip(reviewed), "")
        self.assertIn("Taste N/A", rendering._taste_badge(reviewed))
        self.assertEqual(rendering._taste_badge(_candidate()), "")
        self.assertEqual(rendering._suggested_pin_chip(None), "")


class PinConfidenceStageTests(unittest.TestCase):
    def _run(self, run_dir: Path, results: dict, judge, http):
        return harness._annotate_pin_confidence(
            candidates=CANDIDATES, profiles={"p1": {"headline": "Engineer"}, "p2": {}},
            results=results, run_dir=run_dir, pond_n=1, judge_client=judge, http=http)

    def test_taste_for_every_judged_candidate_and_paid_calls_only_for_four_and_five(self):
        judge_calls = []
        judge, http = _judge_client(judge_calls), _http()
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(harness, "_save"), \
                mock.patch.object(harness, "_price_usage_log"), mock.patch.dict(os.environ, KEYS):
            run_dir, results = Path(raw), {"created_at": "2026-09-24"}
            (run_dir / "jd.txt").write_text("Synthetic JD")
            rows = self._run(run_dir, results, judge, http)
            self.assertEqual(rows, self._run(run_dir, results, judge, http))
        self.assertEqual(len(judge_calls), 1)
        self.assertEqual((judge_calls[0]["model"], judge_calls[0]["reasoning_effort"]), ("gpt-6-sol", "low"))
        self.assertNotIn("overall_score", json.dumps(judge_calls[0]["messages"]))
        top, mid, unjudged = rows
        self.assertEqual((top["taste_score"], top["pin_confidence"]), (7.58, 88))
        self.assertEqual(top["pin_judgment"]["decision"], "introduce")
        self.assertEqual(top["pin_judgment"]["reason"], VERDICT["reason"])
        self.assertEqual((top["pin_judgment"]["model"], top["pin_judgment"]["status"]), ("gpt-6-sol", "ok"))
        self.assertNotIn("signals", top["pin_judgment"])
        self.assertEqual((mid["taste_score"], mid["pin_confidence"], mid["pin_judgment"]), (None, None, None))
        self.assertNotIn("taste_score", unjudged)
        records = results["raw_model_responses"][0]
        self.assertEqual(records["kind"], "pin_confidence")
        self.assertEqual({record["kind"] for record in records["checkpoints"]}, {"taste", "judge"})
        self.assertTrue(all(record["cached"] for record in records["checkpoints"] if "cached" in record))

    def test_failures_and_missing_keys_leave_nulls_and_never_raise(self):
        with self.subTest("providers down"):
            judge_calls = []
            judge, http = _judge_client(judge_calls, fail=True), _http(taste_status=500)
            with tempfile.TemporaryDirectory() as raw, mock.patch.object(harness, "_save"), \
                    mock.patch.object(harness, "_price_usage_log"), mock.patch.dict(os.environ, KEYS):
                run_dir, results = Path(raw), {"created_at": "2026-09-24"}
                (run_dir / "jd.txt").write_text("Synthetic JD")
                top = self._run(run_dir, results, judge, http)[0]
            self.assertEqual((top["taste_score"], top["pin_confidence"]), (None, None))
            self.assertEqual(top["pin_judgment"]["status"], "error")
            errors = [record["error"] for record in results["raw_model_responses"][0]["checkpoints"] if record.get("error")]
            self.assertEqual(len(errors), 2)
        with self.subTest("invalid verdict"):
            judge_calls = []
            judge, http = _judge_client(judge_calls, content="not json"), _http()
            with tempfile.TemporaryDirectory() as raw, mock.patch.object(harness, "_save"), \
                    mock.patch.object(harness, "_price_usage_log"), mock.patch.dict(os.environ, KEYS):
                run_dir, results = Path(raw), {"created_at": "2026-09-24"}
                (run_dir / "jd.txt").write_text("Synthetic JD")
                top = self._run(run_dir, results, judge, http)[0]
            self.assertEqual((top["taste_score"], top["pin_confidence"], top["pin_judgment"]["status"]), (7.58, None, "error"))
        with self.subTest("no keys"):
            judge_calls = []
            judge, http = _judge_client(judge_calls), _http()
            with tempfile.TemporaryDirectory() as raw, mock.patch.object(harness, "_save"), \
                    mock.patch.object(harness, "_price_usage_log"), \
                    mock.patch.dict(os.environ, {key: "" for key in KEYS}):
                run_dir, results = Path(raw), {"created_at": "2026-09-24"}
                (run_dir / "jd.txt").write_text("Synthetic JD")
                top = self._run(run_dir, results, None, http)[0]
            self.assertEqual((top["taste_score"], top["pin_confidence"], top["pin_judgment"]), (None, None, None))
            self.assertEqual(len(judge_calls), 0)
            self.assertEqual({record["kind"] for record in results["raw_model_responses"][0]["checkpoints"]},
                             {"taste", "judge"})

    def test_summary_carries_the_fields_from_the_latest_frame(self):
        judgment = {"model": pc.PIN_JUDGE_MODEL, "decision": "introduce", "reason": "Built it.", "status": "ok"}
        results = {"iterations": [{"pond_n": 1, "query": "Engineers", "shortlist_grades": [
            {"person": "p1", "name": "Jordan Bravo", "company": "Acme", "score": 0.9,
             "taste_score": 7.58, "pin_confidence": 88, "pin_judgment": judgment},
            {"person": "p2", "name": "Casey Charlie", "company": "Beta", "score": 0.8}]}]}
        rows = {row["person"]: row for row in harness.build_search_summary(results, 0)["groups"][""]}
        self.assertEqual((rows["p1"]["taste_score"], rows["p1"]["pin_confidence"], rows["p1"]["pin_judgment"]),
                         (7.58, 88, judgment))
        self.assertEqual((rows["p2"]["taste_score"], rows["p2"]["pin_confidence"], rows["p2"]["pin_judgment"]),
                         (None, None, None))

    def test_pin_saved_runs_only_the_pin_stage_on_saved_rows(self):
        judge_calls = []
        judge, http = _judge_client(judge_calls), _http()
        stage = harness._annotate_pin_confidence
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, KEYS), \
                mock.patch.object(harness, "_price_usage_log"), \
                mock.patch.object(harness, "build_saved_search_summary", return_value={"groups": {"": []}}), \
                mock.patch.object(harness, "_manifest", return_value={}), \
                mock.patch.object(harness, "_annotate_candidate_judgments", side_effect=AssertionError("re-judged")), \
                mock.patch.object(harness, "_annotate_pin_confidence",
                                  side_effect=lambda **kwargs: stage(**{**kwargs, "judge_client": judge, "http": http})):
            run_dir = Path(raw)
            (run_dir / "jd.txt").write_text("Synthetic JD")
            harness._write_json(run_dir / "results.json", {
                "created_at": "2026-09-24", "status": "awaiting_diagnosis",
                "iterations": [{"pond_n": 1, "query": "Engineers", "arm": {"artifacts": {}},
                                "shortlist_grades": [dict(row) for row in CANDIDATES]}]})
            harness.pin_saved(run_dir=run_dir, env_file=str(run_dir / "missing.env"))
            saved = harness._read_json(run_dir / "results.json")
        rows = saved["iterations"][0]["shortlist_grades"]
        self.assertEqual((rows[0]["pin_confidence"], rows[0]["taste_score"]), (88, 7.58))
        self.assertEqual(len(judge_calls), 1)


if __name__ == "__main__":
    unittest.main()
