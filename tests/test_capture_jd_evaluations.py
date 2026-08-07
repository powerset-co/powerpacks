"""Tests for canonical candidate evaluation capture."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from packs.search.primitives.capture_jd_evaluations.capture_jd_evaluations import (
    enrich_evaluations,
    run,
    validate_evaluation,
)
from packs.search.primitives.validate_artifact.validate_artifact import validate_file


def evaluation(**overrides: object) -> dict:
    value = {
        "candidate_id": "abc",
        "rank": 1,
        "jd_score": 0.85,
        "verdict": "top_tier",
        "seniority_fit": "ideal",
        "must_have": [{"trait": "Python", "status": "experienced", "evidence": "5yr exp"}],
        "nice_to_have": [],
        "rationale": "Strong match overall",
        "caveats": [],
    }
    value.update(overrides)
    return value


def candidate(person_id: str = "abc") -> dict:
    return {
        "person_id": person_id,
        "retrieval_score": 0.9,
        "rank_components": {},
        "matched_position_ids": [],
        "matched_position_indexes": [],
        "source_lanes": ["role"],
        "found_by": [
            {"lane": "role", "rank": 1, "probe_id": "p2", "probe_family": "title", "score": 0.9},
            {"lane": "summary", "rank": 2, "probe_id": "p1", "probe_family": "systems", "score": 0.8},
            {"lane": "role", "rank": 3, "probe_id": "p2", "probe_family": "title", "score": 0.7},
        ],
        "backend": "local",
        "hard_filter_evidence": {},
        "structured": {"position_title": "Staff Engineer", "company_name": "Acme"},
        "tech_skills": [],
        "hydrated_profile": {"name": "Ada", "location": "SF"},
        "hydration_disposition": "hydrated",
        "deterministic_score": 0.9,
        "semantic_score": None,
        "triage": None,
        "judge": None,
        "deterministic_gates": {},
    }


class TestCaptureJdEvaluations(unittest.TestCase):
    def test_validate_evaluation_accepts_only_canonical_values(self) -> None:
        self.assertEqual(validate_evaluation(evaluation(), 0), [])
        for verdict in ("strong", "maybe", "weak", "excellent"):
            with self.subTest(verdict=verdict):
                self.assertTrue(any("verdict" in error for error in validate_evaluation(evaluation(verdict=verdict), 0)))
        for status in ("strong", "partial", "weak"):
            with self.subTest(status=status):
                value = evaluation(must_have=[{"trait": "Python", "status": status, "evidence": "e"}])
                self.assertTrue(any("invalid status" in error for error in validate_evaluation(value, 0)))

    def test_validate_evaluation_rejects_shortlist_verdict_with_out_of_band_seniority(self) -> None:
        value = evaluation(verdict="high_potential", seniority_fit="too_senior")
        self.assertTrue(any("cannot be used with seniority_fit" in error for error in validate_evaluation(value, 0)))
        self.assertEqual(validate_evaluation({**value, "verdict": "out"}, 0), [])

    def test_validate_evaluation_missing_fields(self) -> None:
        self.assertTrue(validate_evaluation({"candidate_id": "abc"}, 0))

    def test_duplicate_signal_is_derived_from_canonical_found_by(self) -> None:
        raw = evaluation(duplicate_signal={"matched_probe_count": 99, "matched_probe_ids": ["fake"]})
        enriched = enrich_evaluations([raw], {"candidates": [candidate()]})[0]
        self.assertEqual(enriched["duplicate_signal"], {
            "matched_probe_count": 2,
            "matched_probe_ids": ["p1", "p2"],
            "interpretation": "matched multiple search probes",
        })

    def test_frontier_candidates_reject_legacy_fields(self) -> None:
        legacy = {**candidate(), "current_role": "Staff Engineer"}
        with self.assertRaisesRegex(ValueError, "unknown CandidateRecord fields: current_role"):
            enrich_evaluations([evaluation()], {"candidates": [legacy]})

    def test_run_uses_canonical_paths_and_emits_schema_valid_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            run_dir = Path(tmp_str) / ".powerpacks" / "search-runs" / "run-1"
            (run_dir / "review").mkdir(parents=True)
            (run_dir / "review" / "plan.json").write_text(json.dumps({"job_title": "Test"}))
            (run_dir / "candidate-frontier.json").write_text(json.dumps({"candidates": [candidate()]}))
            (run_dir / "candidate_evaluations.raw.jsonl").write_text(json.dumps(evaluation()) + "\n")
            args = argparse.Namespace(
                run_dir=str(run_dir), raw_evaluations=None, frontier_json=None, plan_json=None,
                out_dir=None, evaluator_mode="harness_single_agent", evaluator_model=None,
                evaluator_reasoning=None, force=False,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                run(args)

            document = json.loads((run_dir / "candidate_evaluations.json").read_text())
            self.assertEqual(
                validate_file("candidate-evaluations", run_dir / "candidate_evaluations.json"),
                document,
            )
            self.assertTrue(document["candidate_frontier_json"].endswith("candidate-frontier.json"))
            self.assertTrue(document["plan_json"].endswith("review/plan.json"))

    def test_run_does_not_fall_back_to_legacy_artifact_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            run_dir = Path(tmp_str)
            (run_dir / "candidate_frontier.json").write_text(json.dumps({"candidates": [candidate()]}))
            (run_dir / "plan.json").write_text("{}")
            (run_dir / "candidate_evaluations.raw.jsonl").write_text(json.dumps(evaluation()) + "\n")
            args = argparse.Namespace(
                run_dir=str(run_dir), raw_evaluations=None, frontier_json=None, plan_json=None,
                out_dir=None, evaluator_mode="harness_single_agent", evaluator_model=None,
                evaluator_reasoning=None, force=False,
            )
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                run(args)


if __name__ == "__main__":
    unittest.main()
