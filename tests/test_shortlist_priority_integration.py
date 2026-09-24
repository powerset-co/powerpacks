"""Saved priorities alter review order without replacing qualification or human feedback."""
from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from packs.search.primitives.deep_search import search_harness
from packs.search.primitives.deep_search.results_web.model import load_searches
from packs.search.primitives.deep_search.results_web.rendering import _cross_encoder_table
from packs.search.primitives.deep_search.results_web.snapshot import export_snapshot, validate_snapshot


class ShortlistPriorityIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name) / "search"
        self.run_dir.mkdir()
        self.rows = []
        grades, profiles = [], []
        for index, (person, overall) in enumerate((("p1", 5), ("p2", 4), ("p3", 3))):
            score = .9 - index / 10
            row = {"person_id": person, "name": f"Person {index}", "final_score": score,
                   "cross_encoder_score": .5, "cross_encoder_score_1_to_5": 4,
                   "cross_encoder_status": "ok", "overall_reasoning": "Original qualification reason"}
            self.rows.append(row)
            grades.append({**row, "person": person, "score": score,
                           "current_company_headcount": 123,
                           "candidate_judgment": {"domain": {"score": overall, "why": "Domain reason"},
                               "opportunity": {"cap": 5, "why": "Opportunity reason"},
                               "overall_score": overall, "model": "original-judge", "status": "ok"}})
            profiles.append({"person_id": person, "summary": f"Original profile summary {index}",
                             "positions": [{"company_name": "Example Company",
                                 "description": "Full original description " + "a" * 1500,
                                 "position_title": f"Role {i}", "start_date": "2020-01-01",
                                 "company_description": "Original company context"} for i in range(8)]})
        self.profiles = profiles
        self.rows_path = self.run_dir / "rows.jsonl"
        self.profiles_path = self.run_dir / "profiles.jsonl"
        self.rows_path.write_text("".join(json.dumps(row) + "\n" for row in self.rows))
        self.profiles_path.write_text("".join(json.dumps(row) + "\n" for row in profiles))
        self.results = {"status": "awaiting_diagnosis", "jd_id": "example", "title": "Example Engineer",
                        "company": "Hiring Example", "created_at": "2026-09-23T00:00:00Z",
                        "hiring_company_context": {"description": "Builds example systems"},
                        "iterations": [{"pond_n": 1, "query": "Engineers", "shortlist_grades": grades,
                                        "arm": {"artifacts": {"jsonl": str(self.rows_path),
                                                              "profiles_path": str(self.profiles_path)}}}]}
        self.results["summary"] = search_harness.build_search_summary(self.results, 0, run_name="search")
        (self.run_dir / "results.json").write_text(json.dumps(self.results))
        (self.run_dir / "jd.txt").write_text("Full original job description")
        self.tags = {"tags": ["Pinned"], "assignments": {"p3": ["Pinned"]}}
        (self.run_dir / "tags.json").write_text(json.dumps(self.tags))
        self.feedback = json.dumps({"person_id": "p3", "human": {"score": 5, "scale": 5,
                                                               "note": "Keep exact human note"}}) + "\n"
        (self.run_dir / "fit-labels.jsonl").write_text(self.feedback)

    def _run(self, output, **options):
        with patch("packs.search.primitives.deep_search.shortlist_priority.ShortlistPriority.run",
                   autospec=True, return_value=output) as run, \
                patch.object(search_harness, "load_env_file"):
            result = search_harness.prioritize_saved(run_dir=self.run_dir, env_file="unused.env",
                                                     max_cost_usd=1, **options)
        return result, {"cases": run.call_args.args[0]._cases}

    def test_completed_run_preserves_all_scores_feedback_and_original_evidence(self):
        annotations = {"p1": {"priority": 10, "reason": "Priority reason <one>"},
                       "p2": {"priority": 90, "reason": "Priority reason two"}}
        originals = {path: path.read_bytes() for path in (
            self.rows_path, self.profiles_path, self.run_dir / "tags.json", self.run_dir / "fit-labels.jsonl")}
        result, inputs = self._run({"status": "completed", "scores": annotations}, approve_spend=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([case.person_id for case in inputs["cases"]], ["p1", "p2"])
        case = inputs["cases"][0]
        self.assertEqual(len(case.state["profile"]["positions"]), 8)
        self.assertEqual(case.state["profile"]["positions"][-1]["description"],
                         self.profiles[0]["positions"][-1]["description"])
        self.assertEqual(case.state["job_description"], "Full original job description")
        self.assertEqual(case.state["current_company_context"]["headcount"], 123)
        self.assertFalse(set(case.state) & {"score", "pinned", "candidate_judgment", "human_score"})
        saved = json.loads((self.run_dir / "results.json").read_text())
        for before, after in zip(self.results["iterations"][0]["shortlist_grades"],
                                 saved["iterations"][0]["shortlist_grades"]):
            self.assertEqual({k: v for k, v in after.items() if k != "shortlist_priority"}, before)
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)
        search = load_searches(self.run_dir.parent, "search")[0]
        self.assertEqual(len(search.candidates), 3)
        self.assertEqual(search.candidate("p3").human_score, 5)
        html = _cross_encoder_table(search)
        self.assertLess(html.index("data-person-id='p2'"), html.index("data-person-id='p1'"))
        self.assertLess(html.index("data-person-id='p1'"), html.index("data-person-id='p3'"))
        self.assertIn("Priority reason &lt;one&gt;", html)
        self.assertIn("data-person-overall='5'", html)
        snapshot = export_snapshot(self.run_dir)
        self.assertEqual(validate_snapshot(snapshot), snapshot)
        self.assertEqual(snapshot["tags"], self.tags)

    def test_estimate_and_provider_error_do_not_mutate_saved_search(self):
        before = (self.run_dir / "results.json").read_bytes()
        result, _ = self._run({"status": "needs_approval", "pending_calls": 4, "estimated_cost_usd": .1})
        self.assertEqual(result["status"], "needs_approval")
        self.assertEqual((self.run_dir / "results.json").read_bytes(), before)
        with patch("packs.search.primitives.deep_search.shortlist_priority.ShortlistPriority.run") as run, \
                patch.object(search_harness, "load_env_file"):
            run.side_effect = RuntimeError("Provider failed")
            with self.assertRaisesRegex(RuntimeError, "Provider failed"):
                search_harness.prioritize_saved(run_dir=self.run_dir, env_file="unused", max_cost_usd=1)
        self.assertEqual((self.run_dir / "results.json").read_bytes(), before)

    def test_limit_clears_old_priorities_without_removing_candidates(self):
        for row in self.results["iterations"][0]["shortlist_grades"]:
            row["shortlist_priority"] = {"priority": 99, "reason": "Stale result"}
        (self.run_dir / "results.json").write_text(json.dumps(self.results))
        _, inputs = self._run({"status": "completed", "scores": {"p1": {"priority": 20, "reason": "New result"}}},
                              limit=1)
        self.assertEqual(len(inputs["cases"]), 1)
        saved = json.loads((self.run_dir / "results.json").read_text())
        rows = saved["summary"]["groups"][""]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["shortlist_priority"]["priority"], 20)
        self.assertTrue(all(row["shortlist_priority"] is None for row in rows[1:]))

    def test_summary_cleared_priority_overrides_related_run_and_baseline_order_stays(self):
        related = deepcopy(self.results)
        related["iterations"][0]["shortlist_grades"][0]["shortlist_priority"] = {"priority": 99, "reason": "Old"}
        self.results["iterations"][0]["shortlist_grades"][0]["shortlist_priority"] = None
        summary = search_harness.build_search_summary(self.results, 0, related_runs=[{"run": "older", "results": related}])
        self.assertIsNone(summary["groups"][""][0]["shortlist_priority"])
        search = load_searches(self.run_dir.parent, "search")[0]
        html = _cross_encoder_table(search)
        self.assertLess(html.index("data-person-id='p1'"), html.index("data-person-id='p2'"))
        self.assertLess(html.index("data-person-id='p2'"), html.index("data-person-id='p3'"))

    def test_new_pond_or_reannotation_invalidates_prior_review(self):
        old = deepcopy(self.results["iterations"][0])
        old["shortlist_grades"][0]["shortlist_priority"] = {"priority": 99, "reason": "Old evidence"}
        new = deepcopy(self.results["iterations"][0])
        new["pond_n"] = 2
        self.results["iterations"] = [old, new]
        summary = search_harness.build_search_summary(self.results, 0)
        self.assertIsNone(summary["groups"][""][0]["shortlist_priority"])


if __name__ == "__main__":
    unittest.main()
