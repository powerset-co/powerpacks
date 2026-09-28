"""Golden text and ranking contract shared with the employee API."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.primitives.deep_search import search_harness, team_similarity
from packs.search.primitives.deep_search.team_similarity import embedding_input, rank, work_text
from packs.search.primitives.deep_search.team_department import Department
from packs.search.primitives.deep_search.results_web import team as team_api
from packs.search.primitives.deep_search.results_web.team import fetch_embedded_employees


class TeamSimilarityTest(unittest.TestCase):
    def test_original_work_text_order_and_aliases(self):
        positions = [
            {"position_title": "Finance Lead", "company_name": "Example Bank",
             "description": "Built forecasts.", "start_date": "2021-02"},
            {"title": "Analyst", "company": "Sample Co", "description": "Owned planning.",
             "start_date": "2024-03", "is_current": True},
            {"title": "Analyst", "company": "Sample Co", "description": "Duplicate.",
             "start_date": "2024-03", "is_current": True},
            {"title": "Deleted role", "company": "Old Co", "description": "Ignore.",
             "start_date": "2025-01", "deleted": True},
        ]
        expected = "Analyst\nSample Co\nOwned planning.\n\nFinance Lead\nExample Bank\nBuilt forecasts."
        self.assertEqual(work_text(positions), expected)
        self.assertEqual(embedding_input(positions)[1], expected)

    def test_five_recent_iso_dated_jobs_only(self):
        positions = [{"title": f"Role {year}", "company": "Example Co",
                      "start_date": f"{year}-01-01T00:00:00Z"}
                     for year in (2018, 2025, 2023, 2021, 2024, 2022)]
        self.assertEqual([part.splitlines()[0] for part in work_text(positions).split("\n\n")],
                         ["Role 2025", "Role 2024", "Role 2023", "Role 2022", "Role 2021"])

    def test_self_employee_excluded_without_dropping_candidate(self):
        people = [
            {"person_id": "candidate-1", "linkedin_url": "https://linkedin.com/in/example/",
             "positions": [{"title": "Analyst", "company": "Sample Co"}]},
            {"person_id": "candidate-2", "linkedin_url": "https://linkedin.com/in/other",
             "positions": [{"title": "Manager", "company": "Example Co"}]},
        ]
        employees = [
            {"provider_employee_id": "employee-1", "linkedin_url": "https://www.linkedin.com/in/example",
             "full_name": "Same Person", "embedding": [1.0, 0.0]},
            {"provider_employee_id": "employee-2", "linkedin_url": "https://linkedin.com/in/colleague",
             "full_name": "Colleague", "embedding": [0.0, 1.0]},
        ]
        vectors = {embedding_input(people[0]["positions"])[0]: [1.0, 0.0],
                   embedding_input(people[1]["positions"])[0]: [0.0, 1.0]}
        scores = rank(people, employees, vectors)
        self.assertEqual(set(scores), {"candidate-1", "candidate-2"})
        self.assertEqual(scores["candidate-1"]["closest_names"], ["Colleague"])
        self.assertEqual(scores["candidate-2"]["rank"], 1)
        self.assertEqual(scores["candidate-1"]["rank"], 2)

    def test_embedded_employee_request_uses_verified_domain(self):
        response = mock.Mock()
        response.json.return_value = {"company_id_source": "coresignal_company",
            "company_id": "core-123", "embedding_model": team_similarity.MODEL,
            "embedding_usage_tokens": 24, "embedding_cache_misses": 1,
            "employees": [{"full_name": "Jordan Bravo", "embedding_status": "ready",
                           "embedding": [1.0, 0.0]}]}
        with mock.patch("packs.search.primitives.deep_search.results_web.team.requests.post",
                        return_value=response) as post, \
             mock.patch("packs.search.primitives.deep_search.results_web.team.bearer_token",
                        return_value="test"), \
             mock.patch("packs.search.primitives.deep_search.results_web.team.api_base",
                        return_value="https://example.test"):
            employees, metadata = fetch_embedded_employees(
                "example.test", Path("test.env"), department="technical")
        self.assertEqual(len(employees), 1)
        self.assertEqual(metadata["company_id"], "core-123")
        self.assertEqual(metadata["embedding_usage_tokens"], 24)
        self.assertEqual(post.call_args.kwargs["json"]["domain"], "example.test")
        self.assertNotIn("company_id", post.call_args.kwargs["json"])
        self.assertTrue(post.call_args.kwargs["json"]["embed"])
        self.assertEqual(post.call_args.kwargs["json"]["departments"], ["technical"])

    def test_every_embedding_page_filters_the_selected_department(self):
        pages = [
            {"company_id": "core-123", "employees": [{"full_name": "Jordan Bravo"},
                                                       {"full_name": "Casey Delta"}]},
            {"company_id": "core-123", "employees": [{"full_name": "Morgan Echo"}]},
        ]
        responses = [mock.Mock(**{"json.return_value": page}) for page in pages]
        with mock.patch.object(team_api, "PAGE_SIZE", 2), \
             mock.patch.object(team_api.requests, "post", side_effect=responses) as post, \
             mock.patch.object(team_api, "bearer_token", return_value="test"), \
             mock.patch.object(team_api, "api_base", return_value="https://example.test"):
            employees, _ = fetch_embedded_employees("example.test", Path("test.env"),
                                                    department="technical")
        self.assertEqual(len(employees), 3)
        bodies = [call.kwargs["json"] for call in post.call_args_list]
        self.assertEqual([body["offset"] for body in bodies], [0, 2])
        self.assertEqual(bodies[0]["domain"], "example.test")
        self.assertEqual(bodies[1]["company_id"], "core-123")
        for body in bodies:
            self.assertEqual(body["departments"], ["technical"])
            self.assertTrue(body["embed"])

    def test_unfiltered_employee_fetch_keeps_its_existing_scope(self):
        response = mock.Mock(**{"json.return_value": {"employees": []}})
        with mock.patch.object(team_api.requests, "post", return_value=response) as post, \
             mock.patch.object(team_api, "bearer_token", return_value="test"), \
             mock.patch.object(team_api, "api_base", return_value="https://example.test"):
            self.assertEqual(team_api.fetch_employees("core-123", Path("test.env")), [])
        self.assertFalse(post.call_args.kwargs["json"]["embed"])
        self.assertNotIn("departments", post.call_args.kwargs["json"])

    def test_empty_department_does_not_broaden_to_all_employees(self):
        metadata = {"company_id_source": "coresignal_company", "company_id": "core-123",
                    "embedding_model": team_similarity.MODEL}
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch.object(Department, "from_jd", return_value=Department("technical")), \
             mock.patch.object(team_similarity, "fetch_embedded_employees",
                               return_value=([], metadata)) as fetch:
            run_dir = Path(raw)
            snapshot = team_similarity.prepare_team("example.test", Path("test.env"), run_dir,
                                                    jd="Backend engineer", as_of="2026-09-28")
            self.assertEqual(snapshot["employees"], [])
            self.assertEqual(snapshot["department"], "technical")
            self.assertEqual(json.loads((run_dir / "team.json").read_text())["members"], [])
        fetch.assert_called_once_with("example.test", Path("test.env"), department="technical")

    def test_department_failure_prevents_employee_request(self):
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch.object(Department, "from_jd", side_effect=RuntimeError("invalid response")), \
             mock.patch.object(team_similarity, "fetch_embedded_employees") as fetch:
            with self.assertRaisesRegex(RuntimeError, "invalid response"):
                team_similarity.prepare_team("example.test", Path("test.env"), Path(raw),
                                             jd="Backend engineer", as_of="2026-09-28")
        fetch.assert_not_called()

    def test_old_or_differently_scoped_snapshot_is_backed_up_and_team_replaced(self):
        for scope in ({"domain": "example.test"},
                      {"domain": "example.test", "department": "sales"},
                      {"domain": "different.test", "department": "technical"}):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as raw:
                run_dir = Path(raw)
                original = json.dumps({**scope, "employees": [{"full_name": "Old Employee"}],
                                       "fetched_at": "2026-09-27"})
                path = run_dir / "team-embeddings.json"
                path.write_text(original)
                (run_dir / "team.json").write_text(json.dumps({"members": [{"name": "Old Employee"}]}))
                metadata = {"company_id_source": "coresignal_company", "company_id": "core-123",
                            "embedding_model": team_similarity.MODEL}
                employees = [{"full_name": "Jordan Bravo", "department": "technical",
                              "embedding_status": "ready", "embedding": [1.0, 0.0]}]
                with mock.patch.object(Department, "from_jd", return_value=Department("technical")), \
                     mock.patch.object(team_similarity, "fetch_embedded_employees",
                                       return_value=(employees, metadata)) as fetch:
                    snapshot = team_similarity.prepare_team("example.test", Path("test.env"), run_dir,
                                                            jd="Backend engineer", as_of="2026-09-28")
                fetch.assert_called_once_with("example.test", Path("test.env"), department="technical")
                backups = list(run_dir.glob("team-embeddings.json.*.bkup"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(), original)
                self.assertEqual(snapshot["domain"], "example.test")
                self.assertEqual(snapshot["department"], "technical")
                self.assertEqual(json.loads(path.read_text()), snapshot)
                team = json.loads((run_dir / "team.json").read_text())
                self.assertEqual([member["name"] for member in team["members"]], ["Jordan Bravo"])

    def test_only_jev_pass_candidates_are_embedded_and_saved(self):
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            profile_path = run_dir / "profiles.jsonl"
            profile_path.write_text("\n".join(json.dumps(row) for row in [
                {"person_id": "p1", "positions": [{"title": "Analyst", "company": "Sample Co"}]},
                {"person_id": "p2", "positions": [{"title": "Manager", "company": "Example Co"}]},
                {"person_id": "p3", "positions": [{"title": "Designer", "company": "Sketch Co"}]},
            ]) + "\n")
            def grade(person, name, passed, score_type="qualification_score"):
                return {"person": person, "name": name, "company": "Current Co",
                        "linkedin_url": f"https://linkedin.com/in/{person}",
                        "cross_encoder_score_type": score_type,
                        "cross_encoder_status": "ok", "cross_encoder_passed": passed}
            results = {"summary": {"groups": {"": [
                {"person": person, "name": name, "company": "Current Co"}
                for person, name in (("p1", "Jordan Bravo"), ("p2", "Casey Delta"),
                                     ("p3", "Morgan Echo"))]}},
                "iterations": [{"pond_n": 1, "arm": {"artifacts": {"profiles_path": str(profile_path)}},
                                "shortlist_grades": [grade("p1", "Jordan Bravo", True),
                                    grade("p2", "Casey Delta", False),
                                    grade("p3", "Morgan Echo", True, "ordinal_rating_1_to_5")]}]}
            people = search_harness._team_candidates(results)
            self.assertEqual([row["person_id"] for row in people], ["p1"])
            team = {"employees": [{"provider_employee_id": "employee-1",
                "full_name": "Teammate", "linkedin_url": "https://linkedin.com/in/teammate",
                "embedding_status": "ready", "embedding": [1.0, 0.0]},
                {"provider_employee_id": "employee-2", "full_name": "Unavailable",
                 "embedding_status": "error", "embedding": None}]}
            digest, _ = embedding_input(people[0]["positions"])
            with mock.patch.object(team_similarity, "embed_candidates", return_value={digest: [1.0, 0.0]}), \
                 mock.patch.object(search_harness, "_save"):
                search_harness._finish_team(run_dir, results, SimpleNamespace(result=lambda: team))
            scores = json.loads((run_dir / "team-similarity.json").read_text())
            self.assertEqual(list(scores), ["p1"])
            self.assertEqual(scores["p1"]["rank"], 1)
            self.assertEqual(json.loads((run_dir / "team-status.json").read_text())["status"], "ready")
            self.assertIn("1 embedding errors", (run_dir / "team-status.json").read_text())

    def test_employee_snapshot_is_reused_without_provider_request(self):
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            metadata = {"company_id_source": "coresignal_company", "company_id": "core-123",
                        "embedding_model": team_similarity.MODEL, "embedding_usage_tokens": 24,
                        "embedding_cache_misses": 1}
            employees = [{"full_name": "Teammate", "embedding_status": "ready",
                          "embedding": [1.0, 0.0]}]
            with mock.patch.object(team_similarity, "fetch_embedded_employees",
                                   return_value=(employees, metadata)) as fetch, \
                 mock.patch.object(Department, "from_jd", return_value=Department("technical")) as classify:
                first = team_similarity.prepare_team("example.test", Path("test.env"), run_dir,
                                                     jd="Backend engineer", as_of="2026-09-28")
                second = team_similarity.prepare_team("example.test", Path("test.env"), run_dir,
                                                      jd="Backend engineer", as_of="2026-09-28")
            fetch.assert_called_once_with("example.test", Path("test.env"), department="technical")
            classify.assert_awaited_with("Backend engineer", as_of="2026-09-28", run_dir=run_dir)
            self.assertEqual(first["department"], "technical")
            self.assertEqual(first, second)

    def test_employee_usage_is_logged_once_and_priced_after_future(self):
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            team = {"employees": [], "embedding_usage_tokens": 2400}
            results = {"iterations": [{"pond_n": 2}]}
            with mock.patch.object(search_harness, "_save"):
                search_harness._finish_team(run_dir, results, SimpleNamespace(result=lambda: team))
                search_harness._finish_team(run_dir, results, SimpleNamespace(result=lambda: team))
            usage = [json.loads(line) for line in (run_dir / "usage.jsonl").read_text().splitlines()]
            self.assertEqual(len(usage), 1)
            self.assertEqual(usage[0]["prompt_tokens"], 2400)
            self.assertEqual(usage[0]["stage"], "search_harness.pond_02.team_similarity.employees")
            self.assertGreater(results["iterations"][-1]["cost_usd"], 0)

    def test_exact_candidate_embedding_cache_prevents_rebilling(self):
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            people = [{"person_id": "p1", "positions": [{"title": "Analyst", "company": "Sample Co"}]}]
            response = SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0, 0.0])])
            client = mock.Mock()
            client.embeddings.create.return_value = response
            with mock.patch.object(team_similarity, "make_openai_client", return_value=client) as make:
                first = team_similarity.embed_candidates(people, run_dir)
                second = team_similarity.embed_candidates(people, run_dir)
            self.assertEqual(first, second)
            make.assert_called_once()
            client.embeddings.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
