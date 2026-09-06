from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.indexing.lib.artifact_io import write_parquet_rows
from packs.indexing.lib.io import read_jsonl
from packs.indexing.primitives.match_job_description_positions import match_job_description_positions as matcher


class MatchJobDescriptionPositionsTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.jobs_path = self.root / "jobs.parquet"
        self.positions_path = self.root / "positions.parquet"
        self.output = self.root / "output"
        self.position = {
            "id": "position-1", "person_id": "person-1", "company_domain": "example.com",
            "company_headcount": 500, "position_title": "Member of Technical Staff",
            "description": "Built distributed storage systems for database replication.",
            "headline": "Other job: mobile applications", "summary": "Previously worked in sales.",
            "start_date_epoch": 1_672_531_200, "end_date_epoch": 0,
        }
        self.job = {
            "id": "jd-1", "company_domain": "example.com", "title": "Storage Engineer",
            "posted_date": "2024-06-01", "retrieval_text": "Build distributed storage engines.",
            "vector": [1.0, 0.0],
        }
        write_parquet_rows(self.jobs_path, [self.job])
        write_parquet_rows(self.positions_path, [self.position])
        self.verdict = {
            "job_description_id": "jd-1", "supported": True,
            "position_evidence": "Built distributed storage systems",
            "jd_evidence": "Build distributed storage engines.",
            "rationale": "Both descriptions specify distributed storage work.",
        }
        self.client = mock.Mock()
        self.client.embeddings.create.side_effect = lambda **kwargs: SimpleNamespace(
            usage=SimpleNamespace(total_tokens=20),
            data=[SimpleNamespace(index=index, embedding=[1.0, 0.0]) for index, _ in enumerate(kwargs["input"])],
        )
        self.client.chat.completions.create.return_value = self.response([self.verdict])
        self.openai = self.enterContext(mock.patch.object(matcher, "OpenAI", return_value=self.client))

    def response(self, verdicts: list[dict], *, finish_reason: str = "stop") -> SimpleNamespace:
        return SimpleNamespace(
            id="response-synthetic", usage=SimpleNamespace(prompt_tokens=100, completion_tokens=100),
            choices=[SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=json.dumps({"matches": verdicts})),
            )],
        )

    def run_paid(self, **kwargs) -> dict:
        return matcher.run(self.jobs_path, self.positions_path, self.output,
                           allow_paid=True, max_cost_usd=1, concurrency=1, **kwargs)

    def test_dry_run_and_budget_refusal_never_construct_paid_client(self) -> None:
        preview = matcher.run(self.jobs_path, self.positions_path, self.output)
        self.assertEqual(preview["status"], "dry-run")
        self.assertEqual(preview["reviewable_positions"], 1)
        self.assertGreater(preview["additional_cost_ceiling_usd"], 0)
        with self.assertRaisesRegex(ValueError, "cost ceiling exceeds"):
            matcher.run(self.jobs_path, self.positions_path, self.output, allow_paid=True, max_cost_usd=0)
        self.openai.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_original_position_text_excludes_other_profile_context(self) -> None:
        self.assertEqual(matcher._position_text(self.position),
                         self.position["position_title"] + "\n" + self.position["description"])
        prompt = matcher._prompt(self.position, [self.job])
        self.assertNotIn(self.position["headline"], prompt)
        self.assertNotIn(self.position["summary"], prompt)

    def test_first_run_caches_paid_outputs_and_second_run_reuses_them(self) -> None:
        first = self.run_paid()
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["matches"], 1)
        self.assertEqual(first["matched_people"], 1)
        self.assertEqual(len(read_jsonl(self.output / "embeddings.jsonl")), 2)
        self.assertEqual(len(read_jsonl(self.output / "reviews.jsonl")), 1)
        self.assertEqual(read_jsonl(self.output / "matches.jsonl")[0]["match_type"], "work_semantic")
        self.client.embeddings.create.assert_called_once()
        self.client.chat.completions.create.assert_called_once()

        second = matcher.run(self.jobs_path, self.positions_path, self.output,
                             allow_paid=True, max_cost_usd=first["spent_usd"], concurrency=1)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["spent_usd"], first["spent_usd"])
        self.assertEqual(second["missing_position_or_control_embeddings"], 0)
        self.openai.assert_called_once()
        self.client.embeddings.create.assert_called_once()
        self.client.chat.completions.create.assert_called_once()

    def test_missing_verdict_is_recorded_as_partial_and_not_accepted(self) -> None:
        self.client.chat.completions.create.return_value = self.response([])
        result = self.run_paid()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["matches"], 0)
        review = read_jsonl(self.output / "reviews.jsonl")[0]
        self.assertIn("error", review)
        self.assertNotIn("matches", review)
        self.assertEqual(read_jsonl(self.output / "work-matches.jsonl"), [])

    def test_truncated_review_is_not_accepted(self) -> None:
        self.client.chat.completions.create.return_value = self.response([self.verdict], finish_reason="length")
        result = self.run_paid()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["matches"], 0)
        self.assertEqual(read_jsonl(self.output / "reviews.jsonl")[0]["error"], "incomplete review")

    def test_failed_request_is_recorded_and_charged_conservatively(self) -> None:
        self.client.chat.completions.create.side_effect = TimeoutError("synthetic timeout")
        result = self.run_paid()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["matches"], 0)
        review = read_jsonl(self.output / "reviews.jsonl")[0]
        self.assertEqual(review["error"], "TimeoutError")
        self.assertGreater(review["cost_usd"], 0)

    def test_fabricated_evidence_is_not_a_position_link(self) -> None:
        self.client.chat.completions.create.return_value = self.response([
            {**self.verdict, "position_evidence": "Built GPU training infrastructure"},
        ])
        result = self.run_paid()
        self.assertEqual(result["matches"], 0)
        self.assertEqual(read_jsonl(self.output / "matches.jsonl"), [])

    def test_duplicate_verdict_ids_are_rejected(self) -> None:
        self.client.chat.completions.create.return_value = self.response([
            self.verdict, {**self.verdict, "supported": False},
        ])
        result = self.run_paid()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["matches"], 0)
        self.assertIn("error", read_jsonl(self.output / "reviews.jsonl")[0])

    def test_missing_supported_verdict_is_recorded_as_partial(self) -> None:
        verdict = {key: value for key, value in self.verdict.items() if key != "supported"}
        self.client.chat.completions.create.return_value = self.response([verdict])
        result = self.run_paid()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["matches"], 0)
        self.assertIn("error", read_jsonl(self.output / "reviews.jsonl")[0])


if __name__ == "__main__":
    unittest.main()
