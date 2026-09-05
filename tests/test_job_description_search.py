from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packs/search/primitives/shared"))

from job_description_search import rank_job_description_people
from packs.search.primitives.execute_role_search import execute_role_search


class JobDescriptionPeopleRankingTests(unittest.TestCase):
    def test_ranks_best_mapping_weighted_evidence_per_person(self) -> None:
        positions = [
            {"id": "position-a", "base_id": "person-a"},
            {"id": "position-b", "base_id": "person-b"},
            {"id": "position-b-old", "base_id": "person-b"},
        ]
        matches = [
            {"job_description_id": "jd-first", "position_id": "position-a", "match_score": 0.2},
            {"job_description_id": "jd-first", "position_id": "position-b-old", "match_score": 0.3},
            {"job_description_id": "jd-second", "position_id": "position-b", "match_score": 0.9,
             "match_type": "title_overlap", "posting_position_gap_days": 42},
        ]

        rows = rank_job_description_people(
            {"jd-first": 0.95, "jd-second": 0.8}, matches, positions, top_k=2,
        )

        self.assertEqual([row["person_id"] for row in rows], ["person-b", "person-a"])
        self.assertEqual(rows[0]["position_id"], "position-b")
        self.assertEqual(rows[0]["job_description_id"], "jd-second")
        self.assertEqual(rows[0]["job_description_position_id"], "position-b")
        self.assertAlmostEqual(rows[0]["score"], 0.72)
        self.assertEqual(rows[0]["job_description_match_type"], "title_overlap")
        self.assertEqual(rows[0]["job_description_position_gap_days"], 42)
        self.assertEqual(len(rank_job_description_people(
            {"jd-first": 0.95, "jd-second": 0.8}, matches, positions, top_k=1,
        )), 1)

    def test_ignores_ineligible_unretrieved_and_nonpositive_evidence(self) -> None:
        matches = [
            {"job_description_id": jd, "position_id": position, "match_score": score}
            for jd, position, score in [
                ("positive", "ineligible", 1.0),
                ("unretrieved", "eligible", 1.0),
                ("negative", "eligible", 1.0),
                ("zero", "eligible", 1.0),
                ("positive", "eligible", 0.0),
            ]
        ]
        rows = rank_job_description_people(
            {"positive": 0.8, "negative": -0.3, "zero": 0.0},
            matches, [{"id": "eligible", "base_id": "person-a"}], top_k=10,
        )
        self.assertEqual(rows, [])

    def test_equal_scores_choose_same_evidence_regardless_of_backend_row_order(self) -> None:
        positions = [
            {"id": "position-b", "base_id": "person-a"},
            {"id": "position-a", "base_id": "person-a"},
        ]
        matches = [
            {"job_description_id": jd, "position_id": position, "match_score": 1.0}
            for jd, position in [("jd-a", "position-b"), ("jd-b", "position-a"), ("jd-a", "position-a")]
        ]
        scores = {"jd-a": 0.8, "jd-b": 0.8}

        forward = rank_job_description_people(scores, matches, positions, top_k=10)
        reverse = rank_job_description_people(scores, matches[::-1], positions[::-1], top_k=10)

        self.assertEqual(forward, reverse)
        self.assertEqual(forward[0]["position_id"], "position-a")
        self.assertEqual(forward[0]["job_description_id"], "jd-a")


class JobDescriptionExecutionTests(unittest.TestCase):
    def test_jd_fuses_at_person_level_and_no_jd_keeps_native_role_scores(self) -> None:
        backend = SimpleNamespace(
            hybrid_role_rows=AsyncMock(return_value=[
                {"id": "a-role", "person_id": "a", "score": 0.9, "retrieval_mode": "hybrid"},
                {"id": "b-role", "person_id": "b", "score": 0.8, "retrieval_mode": "hybrid"},
            ]),
            job_description_rows=AsyncMock(return_value=[{
                "id": "b-old", "person_id": "b", "score": 0.7,
                "retrieval_mode": "job_description", "job_description_id": "jd-b",
            }]),
            namespace_name=lambda name: name,
        )
        args = SimpleNamespace(
            env_file=None, state=None, payload_json=json.dumps({"semantic_query": "software engineer"}),
            top_k=10, limit=10,
        )
        with (
            patch.object(execute_role_search, "load_env_file"),
            patch.object(execute_role_search, "search_backend", return_value=backend),
            patch.object(execute_role_search, "filters_from_role_payload", return_value=None),
            patch.object(execute_role_search.search_backend_mode, "is_local_backend_configured", return_value=False),
        ):
            normal = asyncio.run(execute_role_search.run(args))
            backend.job_description_rows.assert_not_awaited()
            args.payload_json = json.dumps({"semantic_query": "software engineer", "job_description": "Build APIs"})
            with_jd = asyncio.run(execute_role_search.run(args))

        self.assertEqual(normal["candidate_ids"], ["a", "b"])
        self.assertEqual([row["score"] for row in normal["candidates"]], [0.9, 0.8])
        self.assertEqual(normal["verticals"]["job_description"]["status"], "skipped_no_job_description")
        self.assertEqual(with_jd["candidate_ids"], ["b", "a"])
        self.assertEqual(with_jd["candidates"][0]["matched_position_ids"], ["b-role", "b-old"])
        self.assertEqual(with_jd["candidates"][0]["job_description_id"], "jd-b")
        self.assertAlmostEqual(with_jd["candidates"][0]["score"], 1 / 62 + 0.7 / 61)


if __name__ == "__main__":
    unittest.main()
