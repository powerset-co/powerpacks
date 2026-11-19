"""A set search expands in the app, then resolves and executes through the API (synthetic data)."""

from __future__ import annotations

import gzip
import json
import re
import subprocess
import sys
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
CARD = {
    "semantic_query": EXPANDED["role_semantic_query"],
    "bm25_queries": ["Storage Engineer"],
    "role_core_patterns": EXPANDED["role_core_patterns"],
    "company_names": ["Acme"], "cities": ["San Francisco"],
    "is_current_role": True, "headcount_min": 50,
}


class CloudSearchTests(unittest.TestCase):
    def test_execute_sends_each_card_filter_under_the_api_name_only(self) -> None:
        lists = {
            "role_ids": ["chief_financial_officer"], "cities": ["Austin"], "states": ["Texas"],
            "countries": ["USA"], "metro_areas": ["Austin Metro"], "macro_regions": ["North America"],
            "company_cities": ["Boston"], "company_states": ["Massachusetts"], "company_countries": ["USA"],
            "company_metro_areas": ["Boston Metro"], "company_macro_regions": ["North America"],
            "yc_batches": ["W24"], "entity_types": ["venture_backed_startup"], "sector_types": ["saas"],
            "seniority_bands": ["senior"], "role_tracks": ["finance"], "degree_levels": ["MBA"],
            "fields_of_study": ["Finance"], "company_semantic_queries": ["banks"],
            "company_adjacency_queries": ["bank CFO"],
        }
        scalars = {
            "funding_stage_min": "seed", "funding_stage_max": "series_b", "headcount_min": 0,
            "headcount_max": 500, "last_funding_after": "2020-01-01", "last_funding_before": "2025-01-01",
            "funding_amount_min": 1000000, "funding_amount_max": 50000000, "valuation_min": 100000000,
            "valuation_max": 1000000000, "founded_year_min": 2000, "founded_year_max": 2025,
            "graduation_year_min": 2000, "graduation_year_max": 2010, "years_experience_min": 5,
            "years_experience_max": 20, "age_min": 25, "age_max": 45, "role_function": "leader",
            "x_followers_min": 0, "x_followers_max": 5000, "li_followers_min": 10, "li_followers_max": 1000,
            "ig_followers_min": 20, "ig_followers_max": 2000, "li_connections_min": 30, "li_connections_max": 3000,
            "operator_interaction_min": 0, "operator_interaction_max": 100, "set_interaction_min": 5,
            "set_interaction_max": 500, "position_after_date": "2020", "position_before_date": "2025",
            "education_op": "and",
        }
        unsupported = {key: ["ignored"] for key in (
            "role_agent_ids", "role_departments", "csuite_shortcut_role_id", "local_title_clusters",
            "local_title_patterns", "is_current_company", "tech_skills", "technology_types", "customer_types",
            "company_size", "notes", "person_sort_strategy", "seniority_intent", "unknown_filter")}
        core = [{"regex": "CFO", "examples": ["CFO"], "source": "local_title_cluster", "count": 7}]
        adjacent = [{"regex": "finance", "examples": ["Finance Leader"], "metadata": {"score": 1}}]
        card = {**lists, **scalars, **unsupported, "semantic_query": "finance leaders", "bm25_queries": ["CFO"],
                "role_core_patterns": core, "role_adjacent_patterns": adjacent, "is_current_role": None}
        traits = ["builds banks", {"value": "CFO", "temporal": "past", "meaning": "role", "score": 1},
                  {"value": "owns finance"}]
        for domain in (False, True):
            with self.subTest(domain=domain), mock.patch.object(cloud_search.powerset_api, "call") as api:
                request = cloud_search.api_request(None, {"role_search_filters": card, "traits": traits,
                                                           "has_domain_intent": domain},
                                                   query="full original query", set_id="s1", limit=1500,
                                                   conversation_id="c1")
                api.assert_not_called()
            self.assertEqual(request, {
                **lists, **scalars, "company_ids": [], "education_ids": [], "investors": [],
                "role_semantic_query": "finance leaders", "role_bm25_queries": ["CFO"],
                "role_core_patterns": [{"regex": "CFO", "examples": ["CFO"]}],
                "role_adjacent_patterns": [{"regex": "finance", "examples": ["Finance Leader"]}],
                "is_current": None, "traits": [{"value": "builds banks", "temporal": "all", "meaning": "general"},
                                                {"value": "CFO", "temporal": "past", "meaning": "role"},
                                                {"value": "owns finance", "temporal": "all", "meaning": "general"}],
                "has_domain_intent": domain, "search_summary": domain, "search_company_signal": domain,
                "query": "full original query", "set_id": "s1", "limit": 1500, "rerank": True,
                "skip_llm_filter": False, "conversation_id": "c1", "save_before_response": True,
            })

    def test_execute_preserves_empty_false_and_null_card_values(self) -> None:
        for current in (True, False, None):
            with self.subTest(current=current):
                request = cloud_search.api_request(None, {"role_search_filters": {
                    "is_current_role": current, "semantic_query": "", "headcount_min": None,
                    "role_function": None, "bm25_queries": []}}, query="q", set_id="s1", limit=3000)
                self.assertIs(request["is_current"], current)
                self.assertEqual(request["role_semantic_query"], "")
                self.assertEqual(request["role_bm25_queries"], [])
                self.assertEqual(request["limit"], 2000)
                self.assertEqual(request["education_op"], "or")
                self.assertNotIn("headcount_min", request)
                self.assertNotIn("role_function", request)
        request = cloud_search.api_request(None, {"role_search_filters": {"semantic_query": "engineer"}},
                                           query="q", set_id="s1", limit=0)
        self.assertNotIn("is_current", request)
        self.assertNotIn("role_semantic_query", request)

    def test_execute_leaves_currentness_to_traits_without_a_card_override(self) -> None:
        for temporal in ("current", "past", "all"):
            with self.subTest(temporal=temporal):
                traits = [{"value": "Engineer", "temporal": temporal, "meaning": "role"}]
                request = cloud_search.api_request(None, {"role_search_filters": {}, "traits": traits},
                                                   query="q", set_id="s1", limit=0)
                self.assertNotIn("is_current", request)
                self.assertEqual(request["traits"], traits)

    def test_pipeline_imports_without_direct_database_dependencies(self) -> None:
        proc = subprocess.run([sys.executable, "-c", """
import sys
for name in ('psycopg', 'psycopg2', 'turbopuffer'):
    sys.modules[name] = None
from packs.search.primitives.search_network_pipeline import search_network_pipeline
"""], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_keyword_edits_rebuild_patterns_and_reach_execute(self) -> None:
        from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline

        payload = {"role_search_filters": {
            "semantic_query": "people building systems", "bm25_queries": ["backend engineer", "platform engineer"],
            "role_core_patterns": [{"regex": "backend engineer|platform engineer",
                                    "examples": ["backend engineer", "platform engineer"]}]}}
        for keywords in (["backend engineer"], ["backend engineer", "storage engineer"], [], None):
            with self.subTest(keywords=keywords), tempfile.TemporaryDirectory() as directory:
                edited = pipeline.apply_card_edits(payload, {"filters": {"bm25_queries": keywords}}, Path(directory))
                request = cloud_search.api_request(None, edited, query="q", set_id="s1", limit=0)
                self.assertEqual(request["role_bm25_queries"], keywords or [])
                patterns = request["role_core_patterns"]
                for title in ("backend engineer", "platform engineer", "storage engineer"):
                    self.assertEqual(any(re.search(p["regex"], title) for p in patterns), title in (keywords or []))
                if keywords:
                    self.assertEqual(request["role_semantic_query"], "people building systems")
                else:
                    self.assertNotIn("role_semantic_query", request)

    def test_resolve_notes_only_names_without_an_equal_or_prefixed_label(self) -> None:
        payload = {"role_search_filters": {"education_names": ["Example University", "Missing College"]}}
        with mock.patch.object(cloud_search.powerset_api, "call", return_value={
                "education_ids": [{"id": "e1", "display_value": "EXAMPLE University (Main)"}]}):
            out, _ = cloud_search.resolve(None, payload, "s1")
        self.assertEqual(out["notes"], ["resolve did not return education_names: Missing College"])

    def test_card_edits_resolve_all_added_names_in_one_request(self) -> None:
        card = CARD | {"company_names": ["Acme", "Globex"], "education_names": ["Example University"],
                       "investor_names": ["Example Ventures"], "set_id": "s1"}
        calls = []

        def api(env_file, path, body=None, **kwargs):
            calls.append((path, body))
            return {"company_ids": [{"id": "urn:co:2", "display_value": "Globex"},
                                    {"id": "urn:co:3", "display_value": "Globex Labs"}],
                    "education_ids": [{"id": "urn:edu:1", "display_value": "Example University (Main)"}],
                    "investors": [{"id": "urn:vc:1", "display_value": "Example Ventures"}]}

        with mock.patch.object(cloud_search.powerset_api, "call", side_effect=api):
            request = cloud_search.api_request(None, {"role_search_filters": card, "api_filters": EXPANDED,
                                                      "traits": ["builds storage"]},
                                               query="storage engineers", set_id="s1", limit=0)
        self.assertEqual(request["company_ids"], ["urn:co:1", "urn:co:2", "urn:co:3"])
        self.assertEqual(request["education_ids"], ["urn:edu:1"])
        self.assertEqual(request["investors"], ["urn:vc:1"])
        self.assertEqual(request["cities"], ["San Francisco"])
        self.assertTrue(request["is_current"])
        self.assertEqual(request["role_core_patterns"], EXPANDED["role_core_patterns"])
        self.assertEqual((request["set_id"], request["limit"], request["rerank"], request["skip_llm_filter"]),
                         ("s1", 2000, False, True))
        self.assertNotIn("bm25_queries", request)
        self.assertEqual(request["role_bm25_queries"], card["bm25_queries"])
        self.assertNotIn("company_names", request)
        self.assertEqual(calls, [("/v2/search/resolve", {"company_names": ["Globex"],
                               "education_names": ["Example University"], "investor_names": ["Example Ventures"],
                               "set_id": "s1"})])

    def test_run_keeps_at_most_the_limit(self) -> None:
        results = [{"person_id": f"p{i}", "status": "good"} for i in range(5)]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"query": "q", "steps": [{"id": "expand_search_request", "output": {}}]}))
            with mock.patch.object(cloud_search.powerset_api, "call", return_value={"results": results}):
                out = cloud_search.run(SimpleNamespace(state=str(state), env_file=None, set_id="s1", limit=2,
                                                       timeout=60, write_state=False, rerank=False, rerank_rest=False))
        self.assertEqual(out["candidate_count"], 2)

    def test_entity_names_keep_all_matching_ids_in_resolve_order(self) -> None:
        for key, card_key in (("company_ids", "company_names"), ("education_ids", "education_names"),
                              ("investors", "investor_names")):
            expanded = {key: [{"id": id_, "display_value": name} for id_, name in
                              [("A", "Google"), ("D", "Other"), ("B", "Google"),
                               ("C", "Google"), ("A", "Google")]]}
            for names, expected, lookup in (
                    (["Google", "Google", "Google"], ["A", "B", "C"], False),
                    (["google"], ["A", "B", "C"], False),
                    (["Other", "Google"], ["A", "D", "B", "C"], False),
                    (["Other"], ["D"], False),
                    ([], [], False),
                    (["Google", "Added"], ["A", "B", "C", "E"], True)):
                with self.subTest(key=key, names=names), mock.patch.object(
                        cloud_search.powerset_api, "call", return_value={key: [{"id": "E", "display_value": "Added"}]}) as api:
                    request = cloud_search.api_request(
                        None, {"api_filters": expanded, "role_search_filters": {card_key: names}},
                        query="q", set_id="s1", limit=0)
                    self.assertEqual(request.get(key), expected)
                    if lookup:
                        api.assert_called_once_with(None, "/v2/search/resolve", {card_key: ["Added"], "set_id": "s1"})
                    else:
                        api.assert_not_called()

    def test_person_places_send_resolved_ids_and_keep_added_card_values(self) -> None:
        places = ("cities", "states", "countries", "metro_areas", "macro_regions")
        resolved = {key: [{"id": f"{key}-id", "display_value": f"{key} label"}] for key in places}
        card = {key: [f"{key} label", f"{key} added"] for key in places}
        with mock.patch.object(cloud_search.powerset_api, "call") as api:
            request = cloud_search.api_request(None, {"role_search_filters": card, "api_filters": resolved},
                                               query="q", set_id="s1", limit=0)
        api.assert_not_called()
        for key in places:
            self.assertEqual(request[key], [f"{key}-id", f"{key} added"])

    def test_set_prepare_expands_in_the_app_then_resolves_names_and_titles_once(self) -> None:
        from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline

        filters = {"company_names": ["Acme", "Missing"], "education_names": ["Example University"],
                   "investor_names": ["Example Ventures"], "role_agent_ids": ["software_engineer"],
                   "cities": ["Austin"], "states": ["Texas"], "countries": ["USA"],
                   "metro_areas": ["Austin Metro"], "macro_regions": ["North America"],
                   "bm25_queries": ["software engineers"], "set_interaction_min": 2}
        expansion = {"status": "completed", "role_search_filters": filters, "traits": ["builds storage"],
                     "has_domain_intent": False, "notes": ["from app"]}
        resolved = {key: [{"id": name, "display_value": name}] for key, name in
                    [("investors", "Example Ventures"), ("education_ids", "Example University"),
                     *[(key, filters[key][0]) for key in ("cities", "states", "countries", "metro_areas", "macro_regions")]]}
        resolved["company_ids"] = [{"id": id_, "display_value": name} for id_, name in
                                   [("co1", "Acme"), ("co2", "Acme"), ("co3", "Acme Labs")]]
        resolved["title_clusters"] = [
            {"stem": "senior software engin", "titles": ["Senior Software Engineer", "Software Engineer II"], "count": 10},
            {"stem": "sales engin", "titles": ["Sales Engineer"], "count": 5}]
        with tempfile.TemporaryDirectory() as directory:
            args = pipeline.build_parser().parse_args([
                "prepare", "--query", "software engineers", "--set-id", "s1", "--output-dir", directory,
                "--expand-model", "synthetic-model", "--expand-reasoning-effort", "high"])
            with mock.patch.object(pipeline, "run", return_value={"returncode": 0, "json": expansion}) as run, \
                    mock.patch.object(cloud_search.powerset_api, "call", return_value=resolved) as api, \
                    mock.patch.object(pipeline, "emit") as emit, mock.patch.dict("os.environ"):
                self.assertEqual(pipeline.cmd_prepare(args), 0)
            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual(Path(command[1]).stem, "expand_search_request")
            for flag, value in (("--query", args.query), ("--model", "synthetic-model"),
                                ("--reasoning-effort", "high"),
                                ("--snapshot-prompts-dir", str(Path(directory) / "expand-prompts"))):
                self.assertEqual(command[command.index(flag) + 1], value)
            api.assert_called_once_with(Path(".env"), "/v2/search/resolve", {
                **{key: filters[key] for key in ("company_names", "education_names", "investor_names", "cities",
                                                 "states", "countries", "metro_areas", "macro_regions")},
                "role_ids": ["software_engineer"], "set_id": "s1"})
            payload = json.loads((Path(directory) / "expand_search_request.json").read_text())
            card = payload["role_search_filters"]
            self.assertEqual(payload["api_filters"], {k: v for k, v in resolved.items() if k != "title_clusters"})
            self.assertEqual(card["company_names"], ["Acme", "Acme Labs"])
            self.assertEqual(card["investor_names"], ["Example Ventures"])
            self.assertEqual((card["set_id"], card["set_interaction_min"]), ("s1", 2))
            self.assertEqual(payload["traits"], expansion["traits"])
            self.assertFalse(payload["has_domain_intent"])
            self.assertTrue(any("Missing" in note for note in payload["notes"]))
            expected = pipeline._apply_title_clusters(
                pipeline.normalize_query_expansion_payload(pipeline.payload_from_expand_output(expansion)),
                [{**group, "display_title": group["titles"][0]} for group in resolved["title_clusters"]])
            for key in ("bm25_queries", "role_core_patterns"):
                self.assertEqual(card[key], expected["role_search_filters"][key])
            self.assertEqual(card["bm25_queries"], ["software engineers", "Senior Software Engineer"])
            self.assertEqual(emit.call_args.args[0]["status"], "preview_ready")
            with mock.patch.object(cloud_search.powerset_api, "call", side_effect=AssertionError("no lookup")):
                request = cloud_search.api_request(None, payload, query=args.query, set_id="s1", limit=0)
            self.assertEqual(request["company_ids"], ["co1", "co2", "co3"])
            self.assertEqual(request["cities"], ["Austin"])

    def test_long_query_reaches_the_app_expander_and_execute_unchanged(self) -> None:
        from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline

        query = "job description " * 250
        self.assertEqual(len(query), 4000)
        with tempfile.TemporaryDirectory() as directory:
            args = pipeline.build_parser().parse_args(
                ["prepare", "--query", query, "--set-id", "s1", "--output-dir", directory])
            with mock.patch.object(cloud_search.powerset_api, "call", return_value={}) as api, \
                    mock.patch.object(pipeline, "run", return_value={"returncode": 0, "json": {}}) as expand, \
                    mock.patch.object(pipeline, "emit"), mock.patch.dict("os.environ"):
                self.assertEqual(pipeline.cmd_prepare(args), 0)
                command = expand.call_args.args[0]
                self.assertEqual(command[command.index("--query") + 1], query)
                self.assertEqual(api.call_args.args[1], "/v2/search/resolve")
                payload = json.loads((Path(directory) / "expand_search_request.json").read_text())
                state = Path(directory) / "state.json"
                state.write_text(json.dumps({"query": query, "steps": [
                    {"id": "expand_search_request", "output": payload}]}))
                cloud_search.run(SimpleNamespace(state=str(state), env_file=None, set_id="s1", limit=0,
                                                timeout=60, write_state=False, rerank=False, rerank_rest=False))
                self.assertEqual(api.call_args.args[1], "/v2/search/execute")
                self.assertEqual(api.call_args.args[2]["query"], query)

    def test_signed_out_set_prepare_emits_failed_status(self) -> None:
        from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline

        with tempfile.TemporaryDirectory() as directory:
            args = pipeline.build_parser().parse_args(["prepare", "--query", "q", "--output-dir", directory])
            with mock.patch.object(pipeline, "run", return_value={"returncode": 0, "json": {}}), \
                    mock.patch.object(cloud_search.powerset_api, "call", side_effect=SystemExit("signed out")), \
                    mock.patch.object(pipeline, "emit") as emit, mock.patch.dict("os.environ"):
                self.assertEqual(pipeline.cmd_prepare(args), 1)
            self.assertEqual(emit.call_args.args[0]["status"], "failed")
            self.assertEqual(emit.call_args.args[0]["error"], "signed out")

    def test_resolve_drops_empty_results_and_uses_shortcut_role_ids(self) -> None:
        payload = {"role_search_filters": {key: ["Missing"] for key in cloud_search.RESOLVE_KEYS}}
        payload["role_search_filters"]["role_ids"] = ["founder"]
        with mock.patch.object(cloud_search.powerset_api, "call", return_value={}) as api:
            out, clusters = cloud_search.resolve(None, payload, "s1")
        self.assertEqual(api.call_args.args[2]["role_ids"], ["founder"])
        self.assertEqual(out["role_search_filters"], {"role_ids": ["founder"]})
        self.assertEqual(len(out["notes"]), len(cloud_search.RESOLVE_KEYS))
        self.assertTrue(all("Missing" in note for note in out["notes"]))
        self.assertEqual(clusters, [])

    def test_set_prepare_preserves_pins_and_applies_edits_last(self) -> None:
        from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline

        expansion = {"role_search_filters": {"semantic_query": "expanded", "seniority_bands": ["junior"],
                                             "is_current_role": False, "set_id": "wrong"}}
        with tempfile.TemporaryDirectory() as directory:
            edits = Path(directory) / "edits.json"
            edits.write_text(json.dumps({"filters": {"cities": ["Edited"], "is_current_role": False}}))
            args = pipeline.build_parser().parse_args([
                "prepare", "--query", "raw query", "--output-dir", directory, "--set-id", "s1",
                "--preserve-query-semantic", "--seniority-bands", "staff", "--current-role",
                "--edits-json", str(edits)])
            with mock.patch.object(pipeline, "run", return_value={"returncode": 0, "json": expansion}), \
                    mock.patch.object(cloud_search.powerset_api, "call", return_value={}), \
                    mock.patch.object(pipeline, "emit") as emit, mock.patch.dict("os.environ"):
                self.assertEqual(pipeline.cmd_prepare(args), 0)
            filters = json.loads((Path(directory) / "expand_search_request.json").read_text())["role_search_filters"]
            self.assertEqual(filters["semantic_query"], "raw query")
            self.assertEqual(filters["seniority_bands"], ["staff"])
            self.assertTrue(filters["seniority_bands_pinned"])
            self.assertTrue(filters["is_current_role_pinned"])
            self.assertFalse(filters["is_current_role"])
            self.assertEqual((filters["set_id"], filters["cities"]), ("s1", ["Edited"]))
            command = emit.call_args.args[0]["execute_command"]
            for flag in ("--set-id s1", "--current-role", "--seniority-bands staff", "--execute-approved"):
                self.assertIn(flag, command)

    def _api(self, good, bad, sent, *, answer=None):
        """The API of one ranked search, which answers only once its results are saved."""
        def api(env_file, path, body=None, **kwargs):
            sent.append((path, body))
            if path == "/v2/conversations":
                return {"id": "c1"}
            if path == "/v2/search/execute":
                # Its own first 50 come in another shape; nothing reads them.
                return {"total_count": len(good) + len(bad), "good_count": len(good), "bad_count": len(bad), "results_saved": True,
                        "results": answer or [{"person_id": "from-the-answer", "status": "good", "final_score": 1.0}]}
            if path == "/v2/hydrate":
                raise AssertionError("a ranked search's people come from the downloaded file")
            if path.endswith("/person-attribution"):
                return []
            query = dict(pair.split("=") for pair in path.split("?")[1].split("&"))
            if "filter" not in query:
                raise AssertionError("nothing polls the saved count")
            rows, offset = (good if query["filter"] == "good" else bad), int(query.get("offset", 0))
            return {"results": rows[offset:offset + int(query["limit"])], "has_more": offset + int(query["limit"]) < len(rows),
                    "good_count": len(good), "bad_count": len(bad)}
        return api

    def _person(self, person_id, score, reason, title):
        """A result row as GET /v2/conversations/{id}/results returns it: the score and the whole profile."""
        return {"person_id": person_id, "final_score": score, "overall_reasoning": reason, "name": f"Person {person_id}",
                "headline": title, "location": "Austin", "linkedin_url": f"https://linkedin.com/in/{person_id}",
                "trait_scores": {"storage": {"score": 0.9 if score >= 0.3 else 0.0, "reason": reason}},
                "matched_position_indexes": [0], "vertical_sources": ["role"],
                "positions": [{"position_title": title, "company_name": "Example Labs", "is_current": True}],
                "education": [{"school_name": "Example University"}]}

    def test_a_ranked_search_reads_only_the_downloaded_files_good_first_then_bad(self) -> None:
        """With --rerank the API filters, ranks and saves to a conversation. The best 500 good results
        are downloaded first and are the run's people at once; --rerank-rest replaces them with all
        of them, and the bad ones are another process's download. Good and bad become the same
        artifacts through the same functions, so the page reads one shape."""
        from packs.search.primitives.deep_search.results_web.model import _pond_candidates

        good = [self._person(f"g{i}", round(0.9 - i / 1000, 3), "Runs storage", "Storage Engineer") for i in range(501)]
        bad = [self._person("b1", 0.1, "Sells storage", "Account Executive")]
        sent = []
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"task_id": "t", "query": "storage engineers", "steps": [
                {"id": "expand_search_request", "output": {"traits": ["storage"]}}]}))
            args = dict(state=str(state), env_file=None, set_id="s1", limit=0, timeout=60, write_state=True, rerank=True)
            hydrate = SimpleNamespace(state=str(state), env_file=None, limit=0, dry_run=False, local_db=None, local_workers=None,
                                      local_batch_size=None, no_compress_profiles=False, dump_profiles=False, write_state=True)
            with mock.patch("packs.shared.powerset_api.call", side_effect=self._api(good, bad, sent)), \
                    mock.patch.object(cloud_search.time, "sleep"), \
                    mock.patch.object(cloud_search.subprocess, "Popen") as background:
                background.return_value.pid = 4242
                first = cloud_search.run(SimpleNamespace(**args, rerank_rest=False))
                hydrate_people.cmd_hydrate(hydrate)
                first_rows = results_io.result_rows(json.loads(state.read_text()))
                self.assertEqual(background.call_count, 0)
                out = cloud_search.run(SimpleNamespace(**args, rerank_rest=True))
                hydrate_people.cmd_hydrate(hydrate)
                rows = results_io.result_rows(json.loads(state.read_text()))
                profiles = json.loads(state.read_text())["steps"][-1]["output"]["profiles_path"]
                jsonl = Path(directory) / "good.jsonl"
                results_io.write_jsonl(jsonl, rows)
                # The bad ones are another process's job; here it runs in line.
                command = background.call_args.args[0]
                self.assertEqual(command[2:6], ["--bad-for", "c1", "--out", str(Path(out["bad_results"]["jsonl"]).parent)])
                self.assertFalse(Path(out["bad_results"]["jsonl"]).exists())
                self.assertEqual(cloud_search.main(command[2:]), 0)
            root = Path(directory) / "x" / "y"
            shown_good = _pond_candidates(root, {"arm": {"artifacts": {"jsonl": str(jsonl), "profiles_path": profiles}}})
            shown_bad = _pond_candidates(root, {"arm": {"artifacts": out["bad_results"]}})
            self.assertEqual(cloud_search.read_results(Path(out["results_path"])), good)
        results = "/v2/conversations/c1/results?"
        self.assertEqual([path for path, _ in sent if "attribution" not in path], [
            "/v2/conversations", "/v2/search/execute",
            results + "filter=good&limit=500&offset=0",
            results + "filter=good&limit=500&offset=0", results + "filter=good&limit=500&offset=500",
            results + "filter=bad&limit=500&offset=0"])
        request = sent[1][1]
        self.assertEqual((request["rerank"], request["skip_llm_filter"], request["conversation_id"], request["save_before_response"]),
                         (True, False, "c1", True))
        self.assertEqual((first["candidate_count"], first["partial"], [row["person_id"] for row in first_rows]),
                         (500, True, [row["person_id"] for row in good[:500]]))
        self.assertEqual((out["candidate_count"], out["partial"], out["bad_results"]["count"]), (501, False, 1))
        self.assertEqual([row["person_id"] for row in rows], [row["person_id"] for row in good])
        self.assertEqual((rows[0]["final_score"], rows[0]["current_titles"], rows[0]["name"]), ("0.9", "Storage Engineer", "Person g0"))
        # One shape for both: the same fields, filled the same way.
        best, worst = shown_good[0], shown_bad[0]
        self.assertEqual(len(shown_bad), 1)
        self.assertEqual((worst.person_id, worst.name, worst.title, worst.company, worst.final_score, worst.reasoning),
                         ("b1", "Person b1", "Account Executive", "Example Labs", 0.1, "Sells storage"))
        self.assertEqual((best.title, best.company, best.location, best.linkedin_url, best.matched_positions, best.vertical_sources),
                         ("Storage Engineer", "Example Labs", "Austin", "https://linkedin.com/in/g0", (0,), ("role",)))
        for name in ("positions", "education", "traits", "headline", "profile_location", "summary", "avatar_url"):
            self.assertEqual(type(getattr(best, name)), type(getattr(worst, name)), name)
        self.assertEqual((worst.positions[0].title, worst.education[0].school, worst.traits[0].score),
                         ("Account Executive", best.education[0].school, 0.0))

    def test_a_download_is_whole_or_it_is_not_there(self) -> None:
        """A page that fails is asked for again; a download that comes up short of the API's count is
        done again; one that stays short leaves no file. A person on two pages is written once.
        Nothing sleeps between tries of a whole download."""
        import urllib.error

        rows = [{"person_id": f"p{i}"} for i in range(501)]
        calls = {"n": 0}

        def flaky(env_file, path, body=None, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("reset")
            offset = int(path.split("offset=")[1])
            if calls["n"] == 2:  # a page cut short while the API was still saving
                return {"results": rows[:10], "has_more": False, "good_count": 501}
            page = rows[offset:offset + 500] + (rows[:1] if offset else [])
            return {"results": page, "has_more": offset + 500 < len(rows), "good_count": 501}

        def short(env_file, path, body=None, **kwargs):
            return {"results": rows[:10], "has_more": False, "good_count": 501}

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "good_results.jsonl.gz"
            with mock.patch("packs.shared.powerset_api.call", side_effect=flaky), mock.patch.object(cloud_search.time, "sleep"):
                self.assertEqual(cloud_search.download(None, "c1", "good", out), 501)
            self.assertEqual(cloud_search.read_results(out), rows)
            missing = Path(directory) / "bad_results.jsonl.gz"
            with mock.patch("packs.shared.powerset_api.call", side_effect=short), mock.patch.object(cloud_search.time, "sleep"), \
                    self.assertRaisesRegex(RuntimeError, "downloaded 10 of the 501 good results"):
                cloud_search.download(None, "c1", "good", missing)
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["good_results.jsonl.gz"])
            # The background download leaves its failure where the page finds it.
            with mock.patch("packs.shared.powerset_api.call", side_effect=short), mock.patch.object(cloud_search.time, "sleep"):
                self.assertEqual(cloud_search.main(["--bad-for", "c1", "--out", str(Path(directory) / "bad"), "--env-file", ""]), 1)
            self.assertIn("RuntimeError", (Path(directory) / "bad" / "error.txt").read_text())

    def test_a_ranked_search_needs_an_api_that_saves_before_it_answers(self) -> None:
        def api(env_file, path, body=None, **kwargs):
            return {"id": "c1"} if path == "/v2/conversations" else {"good_count": 3, "bad_count": 0, "results": []}

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"query": "q", "steps": [{"id": "expand_search_request", "output": {}}]}))
            with mock.patch("packs.shared.powerset_api.call", side_effect=api), \
                    self.assertRaisesRegex(RuntimeError, "answered before saving the results"):
                cloud_search.run(SimpleNamespace(state=str(state), env_file=None, set_id="s1", limit=0, timeout=60,
                                                 write_state=True, rerank=True, rerank_rest=False))

    def test_a_set_search_is_not_screened_or_ranked_on_this_mac(self) -> None:
        """The pipeline asks the API to rank a set search and runs no LLM step: the best 500 are
        listed, then all of them. A run stopped part way picks up at the step it was on and never
        searches again. A job-description run and --search-only keep the unranked search."""
        from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline

        first = [("cloud_search", "--rerank"), ("hydrate_people", ""), ("results_io", "")]
        rest = [("cloud_search", "--rerank-rest"), ("hydrate_people", ""), ("results_io", "")]
        for extra, fail_at, expected in (
                ([], None, [first + rest]),
                (["--search-only"], None, [[("cloud_search", ""), ("hydrate_people", ""), ("results_io", "")]]),
                ([], 3, [first + rest[:1], [("results_io", "")] + rest]),      # the full download failed
                ([], 4, [first + rest[:2], rest[1:]])):  # stopped before the full hydrate
            runs = []

            def child(command, **kwargs):
                stage = (Path(command[1]).stem, next((flag for flag in ("--rerank-rest", "--rerank") if flag in command), ""))
                runs[-1].append(stage)
                if len(runs) == 1 and len(runs[0]) - 1 == fail_at:
                    return {"returncode": 1, "json": {}, "stderr": "boom", "stdout": ""}
                return {"returncode": 0, "json": {"row_count": 1}}

            with self.subTest(extra=extra, fail_at=fail_at), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "state.json"
                state.write_text(json.dumps({"task_id": "t", "query": "q", "steps": [
                    {"id": "expand_search_request", "output": {"traits": []}}]}))
                args = pipeline.build_parser().parse_args(
                    ["run", "--state", str(state), "--ledger", str(Path(directory) / "pipeline.json"), *extra])
                with mock.patch.object(pipeline, "run", side_effect=child):
                    for _ in expected:
                        runs.append([])
                        try:
                            pipeline.run_pipeline(args)
                        except Exception:
                            self.assertIsNotNone(fail_at)
                self.assertEqual(runs, expected)

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
                {"id": "expand_search_request", "output": {"role_search_filters": CARD,
                                                            "api_filters": EXPANDED, "traits": []}}]}))
            with mock.patch("packs.shared.powerset_api.call", side_effect=api):
                cloud_search.run(SimpleNamespace(state=str(state), env_file=None, set_id="s1", limit=0,
                                                 timeout=60, write_state=True, rerank=False, rerank_rest=False))
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
