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
                                                       timeout=60, write_state=False, rerank=False, rerank_rest=False))
        self.assertEqual(out["candidate_count"], 2)

    def test_company_and_school_names_keep_all_matching_ids_in_expansion_order(self) -> None:
        for key, card_key, kind in (("company_ids", "company_names", "companies"),
                                    ("education_ids", "education_names", "schools")):
            expanded = {key: [{"id": id_, "display_value": name} for id_, name in
                              [("A", "Google"), ("D", "Other"), ("B", "Google"),
                               ("C", "Google"), ("A", "Google")]]}
            for names, expected, lookup in (
                    (["Google", "Google", "Google"], ["A", "B", "C"], False),
                    (["google"], ["A", "B", "C"], False),
                    (["Other", "Google"], ["A", "D", "B", "C"], False),
                    (["Other"], ["D"], False),
                    ([], None, False),
                    (["Google", "Added"], ["A", "B", "C", "E"], True)):
                with self.subTest(key=key, names=names), mock.patch.object(
                        cloud_search.powerset_api, "call", return_value={"suggestions": [{"id": "E"}]}) as api:
                    request = cloud_search.api_request(
                        None, {"api_filters": expanded, "role_search_filters": {card_key: names}},
                        query="q", set_id="s1", limit=0)
                    self.assertEqual(request.get(key), expected)
                    if lookup:
                        api.assert_called_once_with(None, f"/v2/autocomplete/{kind}?limit=1&q=Added")
                    else:
                        api.assert_not_called()

    def test_long_query_is_shortened_only_for_expand(self) -> None:
        from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline

        query = "job description " * 250
        self.assertEqual(len(query), 4000)
        with tempfile.TemporaryDirectory() as directory:
            args = pipeline.build_parser().parse_args(
                ["prepare", "--query", query, "--set-id", "s1", "--output-dir", directory])
            with mock.patch.object(cloud_search.powerset_api, "call", return_value={}) as api, \
                    mock.patch.object(pipeline, "emit"), mock.patch.dict("os.environ"):
                self.assertEqual(pipeline.cmd_prepare(args), 0)
                self.assertEqual(api.call_args.args[2]["query"], query[:2048])
                payload = json.loads((Path(directory) / "expand_search_request.json").read_text())
                state = Path(directory) / "state.json"
                state.write_text(json.dumps({"query": query, "steps": [
                    {"id": "expand_search_request", "output": payload}]}))
                cloud_search.run(SimpleNamespace(state=str(state), env_file=None, set_id="s1", limit=0,
                                                timeout=60, write_state=False, rerank=False, rerank_rest=False))
                self.assertEqual(api.call_args.args[1], "/v2/search/execute")
                self.assertEqual(api.call_args.args[2]["query"], query)

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
                {"id": "expand_search_request", "output": {"role_search_filters": cloud_search.card_filters(EXPANDED),
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
