"""A set search goes only through the Powerset API: expand, execute, hydrate (synthetic data)."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.primitives.cloud_search import cloud_search
from packs.search.primitives.hydrate_people import hydrate_people

EXPANDED = {
    "role_semantic_query": "Engineers who build storage engines",
    "role_core_patterns": [{"regex": "storage engineer", "examples": ["Storage Engineer"]}],
    "company_ids": [{"id": "urn:co:1", "type": "company", "display_value": "Acme"}],
    "education_ids": [],
    "cities": [{"id": "San Francisco", "type": "city", "display_value": "San Francisco"}],
    "is_current": True,
    "headcount_min": 50,
}


class CloudSearchTests(unittest.TestCase):
    def test_card_shows_names_and_keywords_under_its_own_keys(self) -> None:
        card = cloud_search.card_filters(EXPANDED)
        self.assertEqual(card["company_names"], ["Acme"])
        self.assertEqual(card["semantic_query"], EXPANDED["role_semantic_query"])
        self.assertEqual(card["bm25_queries"], ["Storage Engineer"])
        self.assertTrue(card["is_current_role"])
        self.assertNotIn("education_names", card)

    def test_card_edits_map_back_to_ids_and_new_names_are_looked_up(self) -> None:
        card = cloud_search.card_filters(EXPANDED) | {"company_names": ["Acme", "Globex"],
                                                       "education_names": ["Stanford"], "set_id": "s1"}
        calls = []

        def api(env_file, path, body=None, **kwargs):
            calls.append(path)
            return {"suggestions": [{"id": "urn:co:2" if "companies" in path else "urn:edu:1", "display_value": "x"}]}

        with mock.patch.object(cloud_search.powerset_api, "call", side_effect=api):
            request = cloud_search.api_request(None, {"role_search_filters": card, "api_filters": EXPANDED,
                                                      "traits": ["builds storage"]},
                                               query="storage engineers", set_id="s1", limit=0)
        self.assertEqual(request["company_ids"], ["urn:co:1", "urn:co:2"])
        self.assertEqual(request["education_ids"], ["urn:edu:1"])
        self.assertEqual(request["cities"], ["San Francisco"])
        self.assertTrue(request["is_current"])
        self.assertEqual(request["role_core_patterns"], EXPANDED["role_core_patterns"])
        self.assertEqual((request["set_id"], request["limit"], request["rerank"], request["skip_llm_filter"]),
                         ("s1", 2000, False, True))
        self.assertNotIn("bm25_queries", request)
        self.assertNotIn("company_names", request)
        self.assertEqual(len(calls), 2)

    def test_run_keeps_at_most_the_limit(self) -> None:
        results = [{"person_id": f"p{i}", "status": "good"} for i in range(5)]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"query": "q", "steps": [{"id": "expand_search_request", "output": {}}]}))
            with mock.patch.object(cloud_search.powerset_api, "call", return_value={"results": results}):
                out = cloud_search.run(SimpleNamespace(state=str(state), env_file=None, set_id="s1", limit=2,
                                                       timeout=60, write_state=False))
        self.assertEqual(out["candidate_count"], 2)

    def test_run_and_hydrate_read_only_the_api(self) -> None:
        responses = {
            "/v2/search/execute": {"total_count": 2, "results": [
                {"person_id": "p1", "final_score": 1.0, "vertical_sources": ["role"], "status": "good"},
                {"person_id": "p2", "status": "llm_filtered"}]},
            "/v2/hydrate": {"profiles": {"p1": {"person_id": "p1", "name": "Avery Stone", "headline": "Storage Engineer",
                                                "positions": [{"position_title": "Storage Engineer", "is_current": True}]}}},
            "/v2/sets/s1/person-attribution": [{"person_id": "p1", "total_interactions": 3,
                                                "sources": [{"channel": "gmail", "total_interactions": 3, "operator_count": 1}],
                                                "operators": [{"operator_id": "o1", "operator_name": "Jordan", "channels": ["gmail"]}]}],
        }
        sent = []

        def api(env_file, path, body=None, **kwargs):
            sent.append((path, body))
            return responses[path]

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"task_id": "t", "query": "storage engineers", "steps": [
                {"id": "expand_search_request", "output": {"role_search_filters": cloud_search.card_filters(EXPANDED),
                                                            "api_filters": EXPANDED, "traits": []}}]}))
            with mock.patch("packs.shared.powerset_api.call", side_effect=api):
                cloud_search.run(SimpleNamespace(state=str(state), env_file=None, set_id="s1", limit=0,
                                                 timeout=60, write_state=True))
                hydrate_people.cmd_hydrate(SimpleNamespace(
                    state=str(state), env_file=None, limit=0, dry_run=False, local_db=None, local_workers=None,
                    local_batch_size=None, no_compress_profiles=False, dump_profiles=False, write_state=True))
            steps = {step["id"]: step["output"] for step in json.loads(state.read_text())["steps"]}
            with gzip.open(steps["hydrate_people"]["profiles_path"], "rt") as handle:
                profile = json.loads(handle.readline())
        self.assertEqual([path for path, _ in sent], ["/v2/search/execute", "/v2/hydrate", "/v2/sets/s1/person-attribution"])
        self.assertEqual(steps["execute_role_search"]["candidate_ids"], ["p1"])
        self.assertEqual(steps["hydrate_people"]["source"]["backend"], "powerset_api")
        self.assertEqual((profile["name"], profile["total_interactions"], profile["source_channels"]),
                         ("Avery Stone", 3, ["gmail"]))


if __name__ == "__main__":
    unittest.main()
