from __future__ import annotations

import sys
import unittest
from pathlib import Path


SHARED_DIR = Path(__file__).resolve().parents[1] / "packs/search/primitives/shared"
sys.path.insert(0, str(SHARED_DIR))

from search_result_merge import fuse_ranked_position_rows  # noqa: E402


class FuseRankedPositionRowsTests(unittest.TestCase):
    def test_fuses_channel_ranks_with_caller_weights(self) -> None:
        role_rows = [
            {"id": "position-a", "person_id": "person-a", "retrieval_mode": "role"},
            {"id": "position-b", "person_id": "person-b", "retrieval_mode": "role"},
        ]
        jd_rows = [
            {"position_id": "position-b", "person_id": "person-b", "retrieval_mode": "job_description"},
        ]

        fused = fuse_ranked_position_rows([role_rows, jd_rows], [1.0, 0.5])

        self.assertEqual([row["position_id"] for row in fused], ["position-b", "position-a"])
        self.assertGreater(fused[0]["score"], fused[1]["score"])

    def test_keeps_strongest_row_and_merges_provenance(self) -> None:
        role_rows = [{
            "id": "position-a",
            "person_id": "person-a",
            "position_title": "Role channel title",
            "retrieval_mode": "hybrid",
            "vertical_sources": ["role"],
        }]
        jd_rows = [{
            "position_id": "position-a",
            "person_id": "person-a",
            "position_title": "JD channel title",
            "retrieval_mode": "job_description",
            "vertical_sources": ["jd"],
        }]

        fused = fuse_ranked_position_rows([role_rows, jd_rows], [1.0, 2.0])

        self.assertEqual(fused[0]["id"], "position-a")
        self.assertEqual(fused[0]["position_id"], "position-a")
        self.assertEqual(fused[0]["position_title"], "JD channel title")
        self.assertEqual(fused[0]["retrieval_mode"], "job_description")
        self.assertEqual(fused[0]["vertical_sources"], ["role", "hybrid", "jd", "job_description"])

    def test_rejects_missing_channel_weight(self) -> None:
        with self.assertRaisesRegex(ValueError, "one weight per ranked channel"):
            fuse_ranked_position_rows([[{"id": "position-a"}]], [])


if __name__ == "__main__":
    unittest.main()
