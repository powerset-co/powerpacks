from __future__ import annotations

import sys
import unittest
from pathlib import Path


SHARED_DIR = Path(__file__).resolve().parents[1] / "packs/search/primitives/shared"
sys.path.insert(0, str(SHARED_DIR))

from search_result_merge import dedupe_people, fuse_ranked_people  # noqa: E402


class FuseRankedPeopleTests(unittest.TestCase):
    def test_fuses_channel_ranks_with_caller_weights(self) -> None:
        role_rows = [
            {"id": "position-a", "person_id": "person-a", "retrieval_mode": "role"},
            {"id": "position-b", "person_id": "person-b", "retrieval_mode": "role"},
        ]
        jd_rows = [
            {"position_id": "position-b", "person_id": "person-b", "retrieval_mode": "job_description"},
        ]

        fused = fuse_ranked_people([role_rows, jd_rows], [1.0, 0.5])

        self.assertEqual([row["position_id"] for row in fused], ["position-b", "position-a"])
        self.assertGreater(fused[0]["score"], fused[1]["score"])

    def test_combines_different_positions_for_one_person_and_keeps_jd_evidence(self) -> None:
        role_rows = [{
            "id": "position-a",
            "person_id": "person-a",
            "position_title": "Role channel title",
            "retrieval_mode": "hybrid",
            "vertical_sources": ["role"],
        }]
        jd_rows = [{
            "position_id": "position-a-old",
            "person_id": "person-a",
            "position_title": "JD channel title",
            "retrieval_mode": "job_description",
            "vertical_sources": ["jd"],
            "job_description_id": "jd-a",
            "job_description_position_id": "position-a-old",
            "job_description_match_score": 0.8,
        }]

        fused = fuse_ranked_people([role_rows, jd_rows], [1.0, 0.7])
        candidates = dedupe_people(fused, limit=10)

        self.assertEqual(len(fused), 1)
        self.assertEqual(fused[0]["position_id"], "position-a")
        self.assertEqual(fused[0]["vertical_sources"], ["role", "hybrid", "jd", "job_description"])
        self.assertEqual(candidates[0]["matched_position_ids"], ["position-a", "position-a-old"])
        self.assertEqual(candidates[0]["job_description_id"], "jd-a")
        self.assertEqual(candidates[0]["job_description_position_id"], "position-a-old")
        self.assertEqual(candidates[0]["job_description_match_score"], 0.8)
        self.assertAlmostEqual(fused[0]["score"], 1.7 / 61)

    def test_one_vote_per_person_per_channel(self) -> None:
        rows = [
            {"id": "a-current", "person_id": "a", "retrieval_mode": "hybrid"},
            {"id": "a-old", "person_id": "a", "retrieval_mode": "hybrid"},
            {"id": "b-current", "person_id": "b", "retrieval_mode": "hybrid"},
        ]
        fused = fuse_ranked_people([rows], [1.0])

        self.assertEqual([row["person_id"] for row in fused], ["a", "b"])
        self.assertAlmostEqual(fused[0]["score"], 1 / 61)
        self.assertAlmostEqual(fused[1]["score"], 1 / 62)
        self.assertEqual(fused[0]["matched_position_ids"], ["a-current", "a-old"])

    def test_retains_summary_and_company_sources_when_fusing_jd(self) -> None:
        main = [
            {"id": "a-role", "person_id": "a", "retrieval_mode": "hybrid"},
            {"id": "a", "person_id": "a", "retrieval_mode": "summary"},
            {"id": "a-old", "person_id": "a", "retrieval_mode": "company_signal"},
        ]
        jobs = [{"id": "a-jd", "person_id": "a", "retrieval_mode": "job_description"}]
        fused = fuse_ranked_people([main, jobs], [1.0, 0.7])

        self.assertEqual(fused[0]["vertical_sources"], ["hybrid", "summary", "company_signal", "job_description"])
        self.assertEqual(fused[0]["matched_position_ids"], ["a-role", "a-old", "a-jd"])

    def test_does_not_turn_person_only_results_into_position_evidence(self) -> None:
        rows = [{"id": "person-a", "person_id": "person-a", "retrieval_mode": "filter_only"}]

        fused = fuse_ranked_people([rows, []], [1.0, 0.7])

        self.assertEqual(dedupe_people(fused, limit=10)[0]["matched_position_ids"], [])

    def test_rejects_missing_channel_weight(self) -> None:
        with self.assertRaisesRegex(ValueError, "one weight per ranked channel"):
            fuse_ranked_people([[{"id": "position-a"}]], [])


if __name__ == "__main__":
    unittest.main()
