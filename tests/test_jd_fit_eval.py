"""Metrics for human-reviewed JD-fit labels."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.search.evals.evaluate_jd_fit import evaluate_label_files


class JdFitEvalTests(unittest.TestCase):
    def test_compares_rankings_within_each_jd_and_scores_trait_agreement(self) -> None:
        rows = [
            self._row("role-a", "a-review", "review", .9, .8, "capable", "capable"),
            self._row("role-a", "a-pass", "pass", .8, .2, "foundational", "missing"),
            self._row("role-b", "b-review", "review", .4, .9, "experienced", "experienced"),
            self._row("role-b", "b-pass", "pass", .7, .3, "thin", "missing"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fit-labels.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            report = evaluate_label_files([path])

        self.assertEqual(report["counts"], {
            "runs": 2, "labels": 4, "review": 2, "pass": 2, "trait_labels": 4,
        })
        self.assertEqual(report["ranking"]["rerank"]["pairwise_accuracy"], .5)
        self.assertEqual(report["ranking"]["jd_fit"]["pairwise_accuracy"], 1.0)
        self.assertEqual(report["ranking"]["rerank"]["precision_at_20"], .5)
        self.assertEqual(report["ranking"]["jd_fit"]["precision_at_20"], .5)
        self.assertEqual(report["traits"], {
            "exact_agreement": .5, "within_one_rung": .75,
        })
        self.assertEqual(set(report["runs"]), {"role-a", "role-b"})

    @staticmethod
    def _row(run_id: str, person_id: str, overall: str,
             rerank: float, coverage: float, model_status: str,
             human_status: str) -> dict[str, object]:
        trait = "Builds reliable systems"
        return {
            "at": "2026-09-03T00:00:00Z",
            "run_id": run_id,
            "person_id": person_id,
            "human": {"overall": overall, "traits": [{"trait": trait, "status": human_status}]},
            "model": {
                "group": "chat_worthy",
                "rerank_score": rerank,
                "jd_fit": {"coverage": coverage, "traits": [
                    {"trait": trait, "status": model_status, "evidence": "Synthetic evidence."},
                ]},
            },
            "comment": "Synthetic review.",
        }


if __name__ == "__main__":
    unittest.main()
