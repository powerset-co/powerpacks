"""Tests for merge_candidate_frontier primitive."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path


class TestMergeCandidateFrontier(unittest.TestCase):
    """Test the merge / dedupe logic."""

    def _write_probe_csv(self, tmp: Path, probe_id: str, rows: list[dict]) -> Path:
        csv_path = tmp / f"{probe_id}.csv"
        fields = [
            "rank", "person_id", "result_index", "final_score",
            "trait_scores", "overall_reasoning", "matched_position_indexes",
            "pre_rerank_score", "tags", "vertical_sources", "name",
            "headline", "location", "current_titles", "current_companies",
            "linkedin_url", "hydrated", "source_run", "source_query",
        ]
        with csv_path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for r in rows:
                row = {f: "" for f in fields}
                row.update(r)
                writer.writerow(row)
        return csv_path

    def _write_probe_summaries(self, tmp: Path, probes: list[dict]) -> Path:
        path = tmp / "probe_summaries.json"
        path.write_text(json.dumps(probes, indent=2))
        return path

    def _write_plan(self, tmp: Path) -> Path:
        plan_path = tmp / "plan.json"
        plan_path.write_text(json.dumps({"job_title": "Test"}, indent=2))
        return plan_path

    def test_dedup_by_person_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv1 = self._write_probe_csv(tmp, "p1", [
                {"rank": "1", "person_id": "abc", "name": "Alice", "final_score": "0.9",
                 "linkedin_url": "https://linkedin.com/in/alice", "hydrated": "True",
                 "source_run": "run-1", "location": "NYC", "current_titles": "Eng",
                 "current_companies": "Acme"},
            ])
            csv2 = self._write_probe_csv(tmp, "p2", [
                {"rank": "1", "person_id": "abc", "name": "Alice", "final_score": "0.7",
                 "linkedin_url": "https://linkedin.com/in/alice", "hydrated": "True",
                 "source_run": "run-2", "location": "NYC"},
                {"rank": "2", "person_id": "def", "name": "Bob", "final_score": "0.6",
                 "linkedin_url": "https://linkedin.com/in/bob", "hydrated": "True",
                 "source_run": "run-2"},
            ])
            summaries = self._write_probe_summaries(tmp, [
                {"id": "p1", "status": "completed", "csv": str(csv1)},
                {"id": "p2", "status": "completed", "csv": str(csv2)},
            ])
            plan = self._write_plan(tmp)

            from packs.search.primitives.merge_candidate_frontier.merge_candidate_frontier import (
                merge_candidates, read_probe_csv,
            )

            rows = read_probe_csv(csv1, "p1") + read_probe_csv(csv2, "p2")
            candidates = merge_candidates(rows)

            self.assertEqual(len(candidates), 2)
            # Alice appears in both probes
            alice = [c for c in candidates if c["name"] == "Alice"][0]
            self.assertEqual(sorted(alice["matched_probe_ids"]), ["p1", "p2"])
            self.assertEqual(alice["duplicate_signal"]["matched_probe_count"], 2)
            self.assertEqual(len(alice["source_rows"]), 2)

    def test_dedup_by_linkedin_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv1 = self._write_probe_csv(tmp, "p1", [
                {"rank": "1", "person_id": "", "name": "Carol",
                 "linkedin_url": "https://www.linkedin.com/in/Carol123/",
                 "final_score": "0.8"},
            ])
            csv2 = self._write_probe_csv(tmp, "p2", [
                {"rank": "1", "person_id": "", "name": "Carol",
                 "linkedin_url": "https://linkedin.com/in/carol123",
                 "final_score": "0.5"},
            ])

            from packs.search.primitives.merge_candidate_frontier.merge_candidate_frontier import (
                merge_candidates, read_probe_csv,
            )
            rows = read_probe_csv(csv1, "p1") + read_probe_csv(csv2, "p2")
            candidates = merge_candidates(rows)

            self.assertEqual(len(candidates), 1)
            carol = candidates[0]
            self.assertEqual(len(carol["matched_probe_ids"]), 2)

    def test_skipped_probes_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            csv1 = self._write_probe_csv(tmp, "p1", [
                {"rank": "1", "person_id": "x", "name": "X", "final_score": "0.5"},
            ])
            summaries = self._write_probe_summaries(tmp, [
                {"id": "p1", "status": "completed", "csv": str(csv1)},
                {"id": "p2", "status": "failed", "csv": None},
            ])

            from packs.search.primitives.merge_candidate_frontier.merge_candidate_frontier import (
                merge_candidates, read_probe_csv,
            )
            rows = read_probe_csv(csv1, "p1")
            candidates = merge_candidates(rows)
            self.assertEqual(len(candidates), 1)


class TestCaptureJdEvaluations(unittest.TestCase):
    """Test evaluation validation logic."""

    def test_validate_evaluation_good(self) -> None:
        from packs.search.primitives.capture_jd_evaluations.capture_jd_evaluations import (
            validate_evaluation,
        )
        ev = {
            "candidate_id": "abc",
            "rank": 1,
            "jd_score": 0.85,
            "verdict": "strong",
            "seniority_fit": "ideal",
            "must_have": [{"trait": "Python", "status": "strong", "evidence": "5yr exp"}],
            "nice_to_have": [],
            "duplicate_signal": {
                "matched_probe_count": 2,
                "matched_probe_ids": ["p1", "p2"],
                "interpretation": "Found in both probes",
            },
            "rationale": "Strong match overall",
            "caveats": [],
        }
        errors = validate_evaluation(ev, 0)
        self.assertEqual(errors, [])

    def test_validate_evaluation_bad_verdict(self) -> None:
        from packs.search.primitives.capture_jd_evaluations.capture_jd_evaluations import (
            validate_evaluation,
        )
        ev = {
            "candidate_id": "abc",
            "rank": 1,
            "jd_score": 0.5,
            "verdict": "excellent",
            "seniority_fit": "ideal",
            "must_have": [],
            "nice_to_have": [],
            "duplicate_signal": {
                "matched_probe_count": 1,
                "matched_probe_ids": ["p1"],
                "interpretation": "single",
            },
            "rationale": "ok",
            "caveats": [],
        }
        errors = validate_evaluation(ev, 0)
        self.assertTrue(any("verdict" in e for e in errors))

    def test_validate_evaluation_rejects_strong_or_maybe_out_of_band_seniority(self) -> None:
        from packs.search.primitives.capture_jd_evaluations.capture_jd_evaluations import (
            validate_evaluation,
        )
        base = {
            "candidate_id": "abc",
            "rank": 1,
            "jd_score": 0.5,
            "verdict": "maybe",
            "seniority_fit": "too_senior",
            "must_have": [],
            "nice_to_have": [],
            "duplicate_signal": {
                "matched_probe_count": 1,
                "matched_probe_ids": ["p1"],
                "interpretation": "single",
            },
            "rationale": "CFO with stale analyst experience",
            "caveats": [],
        }
        errors = validate_evaluation(base, 0)
        self.assertTrue(any("cannot be used with seniority_fit" in e for e in errors))

        weak = {**base, "verdict": "weak"}
        self.assertEqual(validate_evaluation(weak, 0), [])

    def test_validate_evaluation_missing_fields(self) -> None:
        from packs.search.primitives.capture_jd_evaluations.capture_jd_evaluations import (
            validate_evaluation,
        )
        ev: dict = {"candidate_id": "abc"}
        errors = validate_evaluation(ev, 0)
        self.assertTrue(len(errors) > 0)


if __name__ == "__main__":
    unittest.main()
