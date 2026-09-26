"""The team is a saved, paginated view; similarity never changes the score/order."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from packs.search.primitives.deep_search.results_web.model import load_searches
from packs.search.primitives.deep_search.results_web.rendering import render_page, render_search_body
from packs.search.primitives.deep_search.results_web.snapshot import export_snapshot, render_snapshot
from packs.search.primitives.deep_search.results_web.team import fetch_employees
from tests.test_deep_search_results_web import ResultsWebTest


class TeamTest(unittest.TestCase):
    def test_snapshot_team_and_badge_preserve_scores_and_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = ResultsWebTest()._fixture(directory, cross_encoder=True)
            run = root / "jordan-role"
            original = load_searches(root, run.name)[0]
            team = {"members": [{"name": f"Example Person {i}", "title": "Engineer",
                "linkedin_url": "https://linkedin.com/in/example", "location": "Example City",
                "started_on": "2024-01-01"} for i in range(12)], "fetched_at": "2026-09-25"}
            (run / "team.json").write_text(json.dumps(team))
            (run / "team-similarity.json").write_text(json.dumps({ResultsWebTest.PERSON: {
                "rank": 2, "candidate_count": 3, "score": .78,
                "method": "Titles + descriptions + company names",
                "closest_names": ["Example One", "Example Two", "Example Three"]}}))
            search = load_searches(root, run.name)[0]
            self.assertEqual([c.person_id for c in original.candidates], [c.person_id for c in search.candidates])
            for before, after in zip(original.candidates, search.candidates):
                self.assertEqual(before, replace(after, team_similarity=None))
            page = render_page([search], readonly=True)
            self.assertIn("Team · 12", page)
            self.assertIn("1–10 of 12", page)
            self.assertEqual(page.count("data-team-row hidden"), 2)
            self.assertNotIn("data-team-table open", page)
            self.assertIn("Team Similarity Rank #2", render_search_body(search))
            snapshot = export_snapshot(run)
            exported = render_snapshot(snapshot, asset_base_url="https://example.com/assets")
            self.assertIn("Team · 12", exported)
            self.assertIn("Team Similarity Rank #2", exported)
            self.assertIn("connect-src 'none'", exported)

    def test_employee_fetch_paginates_without_enrichment(self):
        responses = [Mock(), Mock()]
        responses[0].json.return_value = {"employees": [{"full_name": "Example"}] * 500}
        responses[1].json.return_value = {"employees": [{"full_name": "Last Example"}]}
        with patch("packs.search.primitives.deep_search.results_web.team.requests.post", side_effect=responses) as post, \
             patch("packs.search.primitives.deep_search.results_web.team.bearer_token", return_value="test"), \
             patch("packs.search.primitives.deep_search.results_web.team.api_base", return_value="https://example.com"):
            rows = fetch_employees("example", Path("test.env"))
        self.assertEqual(len(rows), 501)
        self.assertEqual([call.kwargs["json"]["offset"] for call in post.call_args_list], [0, 500])
        self.assertTrue(all(call.args[0].endswith("/v2/company/history/employees") for call in post.call_args_list))


if __name__ == "__main__":
    unittest.main()
