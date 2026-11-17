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
from packs.search.primitives.persist_search_results import results_io

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
                                                       timeout=60, write_state=False, rerank=False))
        self.assertEqual(out["candidate_count"], 2)

    def test_rerank_downloads_the_api_ranking_in_pages_good_and_bad_apart(self) -> None:
        """With --rerank the API filters, ranks and saves to a conversation; the run pages the good
        results back as its own ranking (the exporter reads it like a local rerank's) and streams the
        bad ones, as the API returns them, into their own gzip."""
        good = [{"person_id": f"g{i}", "final_score": round(0.9 - i / 1000, 3), "overall_reasoning": "Runs storage",
                 "trait_scores": {"storage": {"score": 0.9, "reason": "Runs storage"}}, "name": f"Good {i}"} for i in range(501)]
        bad = [{"person_id": "b1", "final_score": 0.1, "overall_reasoning": "Sells storage", "name": "Bad One",
                "positions": [{"title": "Account Executive"}]}]
        saved = iter([0, 300, 502])  # the API saves after it answers
        sent = []

        def api(env_file, path, body=None, **kwargs):
            sent.append((path, body))
            if path == "/v2/conversations":
                return {"id": "c1"}
            if path == "/v2/search/execute":
                return {"total_count": 502, "good_count": 501, "bad_count": 1, "results": good[:50]}
            query = dict(pair.split("=") for pair in path.split("?")[1].split("&"))
            if "filter" not in query:
                count = next(saved)
                return {"good_count": count, "bad_count": 0, "results": []}
            rows, offset = (good if query["filter"] == "good" else bad), int(query.get("offset", 0))
            return {"results": rows[offset:offset + int(query["limit"])], "has_more": offset + int(query["limit"]) < len(rows)}

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"task_id": "t", "query": "storage engineers", "steps": [
                {"id": "expand_search_request", "output": {"traits": ["storage"]}}]}))
            with mock.patch("packs.shared.powerset_api.call", side_effect=api), mock.patch.object(cloud_search.time, "sleep"):
                out = cloud_search.run(SimpleNamespace(state=str(state), env_file=None, set_id="s1", limit=0,
                                                       timeout=60, write_state=True, rerank=True))
            steps = json.loads(state.read_text())["steps"]
            rows = results_io.result_rows(json.loads(state.read_text()))
            with gzip.open(out["bad_results_path"], "rt") as handle:
                saved_bad = [json.loads(line) for line in handle]
        results = "/v2/conversations/c1/results?"
        self.assertEqual([path for path, _ in sent], [
            "/v2/conversations", "/v2/search/execute", results + "limit=1", results + "limit=1", results + "limit=1",
            results + "filter=good&limit=500&offset=0", results + "filter=good&limit=500&offset=500",
            results + "filter=bad&limit=500&offset=0"])
        request = sent[1][1]
        self.assertEqual((request["rerank"], request["skip_llm_filter"], request["conversation_id"]), (True, False, "c1"))
        self.assertEqual([step["id"] for step in steps][1:], ["execute_role_search", "llm_rerank_candidates"])
        self.assertEqual((out["candidate_count"], out["bad_count"], out["conversation_id"]), (501, 1, "c1"))
        self.assertEqual([row["person_id"] for row in rows], [row["person_id"] for row in good])
        self.assertEqual((rows[0]["final_score"], json.loads(rows[0]["trait_scores"])["storage"]["score"]), ("0.9", 0.9))
        self.assertEqual(saved_bad, bad)

    def test_rerank_fails_when_the_api_never_saves_the_results(self) -> None:
        def api(env_file, path, body=None, **kwargs):
            if path == "/v2/conversations":
                return {"id": "c1"}
            if path == "/v2/search/execute":
                return {"good_count": 3, "bad_count": 0, "results": []}
            return {"good_count": 0, "bad_count": 0, "results": []}

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"query": "q", "steps": [{"id": "expand_search_request", "output": {}}]}))
            with mock.patch("packs.shared.powerset_api.call", side_effect=api), \
                    mock.patch.object(cloud_search.time, "sleep"), \
                    mock.patch.object(cloud_search.time, "time", side_effect=[0, 0, 200, 200]), \
                    self.assertRaisesRegex(RuntimeError, "saved 0 of 3 results"):
                cloud_search.run(SimpleNamespace(state=str(state), env_file=None, set_id="s1", limit=0,
                                                 timeout=60, write_state=True, rerank=True))

    def test_a_set_search_is_not_screened_or_ranked_on_this_mac(self) -> None:
        """The pipeline asks the API to rank a set search and runs no LLM step; a job-description
        run and --search-only keep the unranked search."""
        from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline

        for extra, ranked in (([], True), (["--search-only"], False)):
            stages = []

            def child(command, **kwargs):
                stages.append((Path(command[1]).stem, "--rerank" in command))
                return {"returncode": 0, "json": {}}

            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "state.json"
                state.write_text(json.dumps({"task_id": "t", "query": "q", "steps": [
                    {"id": "expand_search_request", "output": {"traits": []}}]}))
                args = pipeline.build_parser().parse_args(
                    ["run", "--state", str(state), "--ledger", str(Path(directory) / "pipeline.json"), *extra])
                with mock.patch.object(pipeline, "run", side_effect=child):
                    pipeline.run_pipeline(args)
            self.assertEqual(stages, [("cloud_search", ranked), ("hydrate_people", False), ("results_io", False)])

    def test_api_calls_ask_for_gzip_and_read_it(self) -> None:
        from packs.shared import powerset_api

        class Reply:
            headers = {"Content-Encoding": "gzip"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return gzip.compress(json.dumps({"profiles": {"p1": {}}}).encode())

        with mock.patch.object(powerset_api.pull_runtime_keys, "api_base", return_value="https://api.example"), \
                mock.patch.object(powerset_api.pull_runtime_keys, "bearer_token", return_value="token"), \
                mock.patch("urllib.request.urlopen", return_value=Reply()) as urlopen:
            out = powerset_api.call(None, "/v2/hydrate", {"person_ids": ["p1"]})
        self.assertEqual(out, {"profiles": {"p1": {}}})
        self.assertEqual(urlopen.call_args.args[0].get_header("Accept-encoding"), "gzip")

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
                                                 timeout=60, write_state=True, rerank=False))
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
