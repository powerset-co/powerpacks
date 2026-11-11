"""Quick-run refine contract through the CLI and a local OpenAI HTTP server."""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from packs.search.primitives.deep_search.results_web.model import load_catalog, load_search, record_quick_search
from packs.search.primitives.lib.usage_pricing import load_prices, row_cost_usd
from packs.search.primitives.search_network_pipeline.refine import _has_zero_trait_score, rerank
from packs.search.primitives.search_network_pipeline.refine_prompt import candidate_xml, cohort_summary

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "packs/search/primitives/search_network_pipeline/search_network_pipeline.py"


class RefineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.run = self.project / ".powerpacks/search/fixture"
        self.run.mkdir(parents=True)
        self.root = self.project / ".powerpacks/deep-search"
        self.ids = [f"00000000-0000-0000-0000-{n:012d}" for n in range(8)]
        self.rows = [dict(person_id=pid, name=f"Jordan Example {n}", final_score=score,
                          trait_scores={"engineer": {"score": 0.8}}, overall_reasoning="original",
                          matched_position_indexes=[0], vertical_sources=["role"], extra="carried over")
                     for n, (pid, score) in enumerate(zip(self.ids, [.9, .6, .35, .7, .8, .7, .29, .9]))]
        self.rows[7]["trait_scores"] = json.dumps({"trait_scores": {"engineer": 0}})
        self.jsonl = self.run / "query_results.jsonl"
        self.jsonl.write_text("".join(json.dumps(row) + "\n" for row in self.rows))
        self.profiles = self.run / "profiles.jsonl"
        self.profiles.write_text("".join(json.dumps(dict(person_id=pid, name=f"Jordan Example {n}", inferred_age=30 + n,
            years_of_experience=n * 2.5, summary="Builds search" if n < 4 else None,
            positions=[dict(position_title="Engineer", company_name="Example Labs", is_current=True, seniority_band="Senior",
                            company_stage="SERIES_B", company_sector_types=["AI_ML"]),
                       dict(position_title="Founder" if n == 7 else "Intern", company_name="Side Co", is_current=n == 7)],
            education=[dict(school_name="Stanford University" if n % 2 else "Example College")],
            trait_scores={"engineer": {"score": 0.8}})) + "\n"
            for n, pid in enumerate(self.ids)))
        self.state = self.run / "task.json"
        self.state.write_text(json.dumps({"steps": [{"id": "hydrate_people", "output": {"profiles_path": str(self.profiles)}}]}))
        (self.run / "pipeline.ledger.json").write_text(json.dumps({"state": str(self.state)}))
        record_quick_search(self.root, "fixture", query="engineers", filters={}, traits=[], created_at="2026-11-09",
                            jsonl=str(self.jsonl.relative_to(self.project)), profiles_path=str(self.profiles), status="completed")
        self.result_path = self.root / "fixture/results.json"
        self.catalog_path = self.root / "catalog.json"
        self.requests = []
        self.fail_id = None
        self.malformed = False
        self.bare = False
        self.fenced = False
        self.usage = True
        self.types = {}
        self.scores = dict(zip(self.ids, [.8, .95, .6, .4, .5, .1, 1, 1]))
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                ids = re.findall(r"<person id='([^']+)'>", body["messages"][0]["content"])
                fixture.requests.append((ids, body, json.loads(fixture.catalog_path.read_text())["fixture"]["status"]))
                if fixture.fail_id in ids:
                    self.send_response(500)
                    self.end_headers()
                    return
                results = [dict(person_id=pid, score=fixture.scores[pid], reasoning="refined") for pid in ids]
                results.append(dict(person_id="unknown", score=1, reasoning="skip"))
                content = json.dumps(results if fixture.bare else {"query_type": fixture.types.get(ids[0], "rerank"), "results": results})
                if fixture.fenced:
                    content = "```json\n" + content + "\n```"
                if fixture.malformed:
                    content = "not JSON"
                response = dict(id="mock", object="chat.completion", created=0, model=body["model"],
                                choices=[dict(index=0, finish_reason="stop", message=dict(role="assistant", content=content))])
                if fixture.usage:
                    response["usage"] = dict(prompt_tokens=100, completion_tokens=10, total_tokens=110)
                data = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env_file = self.run / "test.env"
        self.env_file.write_text(f"OPENAI_API_KEY=test-key\nOPENAI_API_BASE=http://127.0.0.1:{self.server.server_port}\n")
        self.env = dict(os.environ)
        for key in ("OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL", "POWERPACKS_USAGE_LOG", "OPENAI_SERVICE_TIER"):
            self.env.pop(key, None)

    def cli(self, *args, run_dir=None, expected=0):
        proc = subprocess.run([sys.executable, str(PIPELINE), "rerank", "--run-dir", str(run_dir or self.run),
                               "--env-file", str(self.env_file), *args], cwd=ROOT, env=self.env,
                              text=True, capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, expected, proc.stdout + proc.stderr)
        output = json.loads(proc.stdout)
        self.assertEqual(output["status"], "completed" if expected == 0 else "failed")
        return output, proc

    def payload(self):
        return json.loads(self.result_path.read_text())

    def step_rows(self):
        path = self.payload()["iterations"][0]["arm"]["artifacts"]["jsonl"]
        return [json.loads(line) for line in (self.project / path).read_text().splitlines()]

    def snapshot(self):
        return {str(path): path.read_bytes() for path in self.project.rglob("*") if path.is_file()}

    def test_refine_pool_readers_order_cost_and_apostrophe(self):
        criteria = "who has founder's experience"
        command = "uv run --env-file .env --project . python " + str(PIPELINE.relative_to(ROOT)) + " rerank --run-dir " + shlex.quote(str(self.run)) + " --criteria " + shlex.quote(criteria)
        self.assertFalse(any(char in command for char in ";&|$`<>\\"))
        self.assertEqual(shlex.split(command)[-1], criteria)
        output, proc = self.cli("--criteria", criteria)
        payload = self.payload()
        self.assertEqual([s["id"] for s in payload["steps"]], ["s1", "s2"])
        step = payload["steps"][1]
        self.assertEqual((step["parent"], step["criteria"], step["scored"], step["shown"]), ("s1", criteria, 6, 4))
        self.assertEqual(payload["current_step"], "s2")
        self.assertEqual(payload["summary"]["pond_chain"][0]["result_count"], 4)
        self.assertEqual(payload["iterations"][0]["arm"]["artifacts"]["jsonl"], step["jsonl"])
        self.assertEqual(payload["steps"][0]["jsonl"], str(self.jsonl.relative_to(self.project)))
        self.assertFalse(Path(step["jsonl"]).is_absolute())
        self.assertEqual(step["jsonl"], ".powerpacks/search/fixture/query_results.s2.jsonl")
        self.assertEqual([row["final_score"] for row in self.step_rows()], [.95, .8, .6, .5])
        for row in self.step_rows():
            original = next(r for r in self.rows if r["person_id"] == row["person_id"])
            self.assertEqual(row, dict(original, final_score=self.scores[row["person_id"]], overall_reasoning="refined"))
        self.assertEqual(set(pid for ids, _, _ in self.requests for pid in ids), set(self.ids[:6]))
        self.assertTrue(all(len(ids) == 2 and status == "running" for ids, _, status in self.requests))
        self.assertTrue(all(body["model"] == "gpt-5.1" and body["max_completion_tokens"] == 4096 and body["reasoning_effort"] == "low" for _, body, _ in self.requests))
        expected = 3 * row_cost_usd(dict(model="gpt-5.1", prompt_tokens=100, completion_tokens=10), load_prices())
        self.assertAlmostEqual(step["cost_usd"], expected)
        self.assertEqual(output["step"]["cost_usd"], step["cost_usd"])
        self.assertIn(f'4 kept of 6 for "{criteria}"', proc.stderr)
        self.assertEqual(load_catalog(self.root)[0].candidates, 4)
        search = load_search(self.root, "fixture")
        self.assertEqual(len(search.ponds[0].candidates), 4)
        kept = {row["person_id"] for row in self.step_rows()}
        self.requests.clear()
        self.cli("--criteria", "again")
        self.assertEqual(set(pid for ids, _, _ in self.requests for pid in ids), kept)
        self.assertEqual(self.payload()["steps"][-1]["parent"], "s2")

    def test_filter_majority_and_filter_and_rank(self):
        self.types = {self.ids[0]: "filter", self.ids[2]: "rerank", self.ids[4]: "filter"}
        self.cli("--criteria", "only engineers")
        self.assertEqual(self.payload()["steps"][-1]["query_type"], "filter")
        self.assertEqual([row["final_score"] for row in self.step_rows()], [.9, .8, .6])
        self.cli("--use", "s1")
        self.types = {self.ids[0]: "filter_and_rank", self.ids[2]: "filter_and_rank", self.ids[4]: "filter"}
        self.cli("--criteria", "who has experience")
        self.assertEqual(self.payload()["steps"][-1]["query_type"], "filter_and_rank")
        self.assertEqual([row["final_score"] for row in self.step_rows()], [.95, .8, .6, .5])

    def test_batch_concurrency_is_capped(self):
        lock = threading.Lock()
        active = peak = 0
        handler = self.server.RequestHandlerClass
        original = handler.do_POST

        def counted_post(request):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.2)
                original(request)
            finally:
                with lock:
                    active -= 1

        env = {"OPENAI_API_KEY": "test-key", "OPENAI_API_BASE": f"http://127.0.0.1:{self.server.server_port}"}
        with patch.object(handler, "do_POST", counted_post), patch(
            "packs.search.primitives.search_network_pipeline.refine.MAX_CONCURRENT_BATCHES", 2
        ):
            output = rerank(self.run, criteria="capped", use=None, model="gpt-5.1", env=env)
        self.assertLessEqual(peak, 2)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(sorted(pid for ids, _, _ in self.requests for pid in ids), self.ids[:6])
        self.assertEqual(output["step"]["scored"], 6)

    def test_use_branch_and_unknown(self):
        self.cli("--criteria", "first")
        s2 = self.payload()["steps"][1]
        s2_bytes = (self.project / s2["jsonl"]).read_bytes()
        count = len(self.requests)
        output, _ = self.cli("--use", "s1")
        self.assertEqual(output["step"]["cost_usd"], 0)
        self.assertEqual(self.payload()["current_step"], "s1")
        self.assertEqual(load_catalog(self.root)[0].candidates, 8)
        self.assertEqual(len(self.requests), count)
        self.cli("--criteria", "branch")
        self.assertEqual(self.payload()["steps"][-1]["parent"], "s1")
        self.assertEqual(self.payload()["steps"][-1]["id"], "s3")
        self.assertEqual((self.project / s2["jsonl"]).read_bytes(), s2_bytes)
        count = len(self.requests)
        self.cli("--use", "s2")
        self.assertEqual(self.payload()["current_step"], "s2")
        self.assertEqual(len(self.requests), count)
        before = self.snapshot()
        self.cli("--use", "missing", expected=1)
        self.cli("--use", "", expected=1)
        self.assertEqual(self.snapshot(), before)

    def test_failed_batch_and_unparseable_leave_previous_step(self):
        self.cli("--criteria", "first")
        before = self.result_path.read_bytes()
        self.fail_id = self.ids[2]
        self.cli("--criteria", "fail", expected=1)
        self.assertEqual(self.result_path.read_bytes(), before)
        self.assertEqual(load_catalog(self.root)[0].status, "failed")
        self.assertFalse((self.run / "query_results.s3.jsonl").exists())
        self.fail_id = None
        self.malformed = True
        self.cli("--criteria", "malformed", expected=1)
        self.assertEqual(self.result_path.read_bytes(), before)
        self.assertEqual(load_catalog(self.root)[0].status, "failed")

    def test_interrupted_scoring_marks_failed_and_allows_use(self):
        before = self.result_path.read_bytes()
        env = {"OPENAI_API_KEY": "test-key", "OPENAI_API_BASE": f"http://127.0.0.1:{self.server.server_port}"}
        for interruption in (KeyboardInterrupt, SystemExit):
            with self.subTest(interruption=interruption.__name__):
                with patch("packs.search.primitives.search_network_pipeline.refine._score_batch", side_effect=interruption):
                    with self.assertRaises(interruption):
                        rerank(self.run, criteria="interrupted", use=None, model="gpt-5.1", env=env)
                self.assertEqual(self.result_path.read_bytes(), before)
                self.assertEqual(load_catalog(self.root)[0].status, "failed")
                self.assertFalse((self.run / "query_results.s2.jsonl").exists())
                self.cli("--use", "s1")
                self.assertEqual(load_catalog(self.root)[0].status, "completed")
        self.assertEqual(self.requests, [])

    def test_refusals_leave_files_unchanged(self):
        before = self.snapshot()
        self.cli("--criteria", "test", run_dir=self.root / "fixture", expected=1)
        self.assertEqual(self.snapshot(), before)
        catalog = json.loads(self.catalog_path.read_text())
        catalog["fixture"]["status"] = "running"
        self.catalog_path.write_text(json.dumps(catalog))
        before = self.snapshot()
        self.cli("--criteria", "test", expected=1)
        self.assertEqual(self.snapshot(), before)
        catalog["fixture"]["status"] = "completed"
        self.catalog_path.write_text(json.dumps(catalog))
        self.profiles.write_text("")
        before = self.snapshot()
        self.cli("--criteria", "test", expected=1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.requests, [])

    def test_old_run_fences_bare_array_override_and_no_usage(self):
        payload = self.payload()
        del payload["steps"], payload["current_step"]
        self.result_path.write_text(json.dumps(payload))
        self.assertEqual(len(load_search(self.root, "fixture").ponds[0].candidates), 8)
        self.fenced = self.bare = True
        self.usage = False
        self.cli("--criteria", "test", "--model", "unpriced-mock")
        self.assertEqual(self.payload()["steps"][0]["jsonl"], str(self.jsonl.relative_to(self.project)))
        self.assertEqual(self.payload()["steps"][1]["query_type"], "rerank")
        self.assertEqual(self.payload()["steps"][1]["cost_usd"], 0)
        self.assertTrue(all(body["max_tokens"] == 4096 and body["temperature"] == .1 for _, body, _ in self.requests))

    def test_empty_pool_and_shared_usage_log(self):
        usage = self.run / "shared-usage.jsonl"
        usage.write_text(json.dumps(dict(model="gpt-5.1", prompt_tokens=1000000)) + "\n")
        self.env["POWERPACKS_USAGE_LOG"] = str(usage)
        self.cli("--criteria", "first")
        expected = 3 * row_cost_usd(dict(model="gpt-5.1", prompt_tokens=100, completion_tokens=10), load_prices())
        self.assertAlmostEqual(self.payload()["steps"][-1]["cost_usd"], expected)
        self.scores = {pid: 0 for pid in self.ids}
        self.cli("--criteria", "nothing")
        self.assertEqual(self.step_rows(), [])
        count = len(self.requests)
        self.cli("--criteria", "empty")
        step = self.payload()["steps"][-1]
        self.assertEqual((step["query_type"], step["scored"], step["shown"], step["cost_usd"]), ("rerank", 0, 0, 0))
        self.assertEqual(len(self.requests), count)

    def test_zero_traits_shapes_and_matched_xml(self):
        for traits in ({"a": 0}, {"a": {"score": 0}}, {"trait_scores": {"a": 0}}, json.dumps({"a": 0})):
            self.assertTrue(_has_zero_trait_score(traits))
        for traits in (None, [], {"a": .3}, {"a": {"score": None}}, {"overall_reasoning": 0}, {"trait_scores": []}):
            self.assertFalse(_has_zero_trait_score(traits))
        profile = dict(person_id="synthetic", name="Jordan Example", base_score=.7, matched_position_indexes=[8],
                       positions=[dict(position_title=f"Engineer {n}", company_name="Example Labs") for n in range(10)])
        xml = candidate_xml(profile)
        self.assertEqual(xml.count("<work>"), 5)
        self.assertIn("Engineer 8 (past, matched)", xml)
        self.assertNotIn("Engineer 4 (past)", xml)
        self.assertEqual(cohort_summary([profile]), 'Total candidates: 1, unique matched titles: 1\n- "Engineer 8" at Example Labs (1 person)')

    def test_sql_filters_sorts_and_fails_without_the_judge(self):
        output, _ = self.cli("--criteria", "under 35", "--sql", "SELECT person_id FROM candidates WHERE inferred_age < 35")
        self.assertEqual(output["step"], {"run_id": "fixture", "id": "s2", "parent": "s1", "criteria": "under 35",
                                          "query_type": "filter", "shown": 5, "scored": 8, "cost_usd": 0})
        self.assertEqual([row["person_id"] for row in self.step_rows()], self.ids[:5])
        self.assertEqual([row["final_score"] for row in self.step_rows()], [.9, .6, .35, .7, .8])
        self.assertEqual(self.payload()["steps"][-1]["sql"], "SELECT person_id FROM candidates WHERE inferred_age < 35")
        self.assertEqual(load_search(self.root, "fixture").steps[-1].sql, "SELECT person_id FROM candidates WHERE inferred_age < 35")
        self.assertEqual(len(load_search(self.root, "fixture", step="s1").candidates), 8)
        self.assertEqual(self.requests, [])
        self.cli("--criteria", "stanford first", "--sql",
                 "SELECT c.person_id FROM candidates c LEFT JOIN education e USING (person_id) "
                 "ORDER BY e.school_name ILIKE '%stanford%' DESC, c.base_score DESC")
        self.assertEqual(self.payload()["steps"][-1]["query_type"], "sort")
        self.assertEqual(self.payload()["steps"][-1]["parent"], "s2")
        self.assertEqual([row["person_id"] for row in self.step_rows()],
                         [self.ids[3], self.ids[1], self.ids[0], self.ids[4], self.ids[2]])
        self.cli("--criteria", "senior engineers", "--sql",
                 "SELECT person_id FROM positions WHERE is_current AND seniority_band = 'senior' "
                 "AND person_id IN (SELECT person_id FROM trait_scores WHERE trait_name = 'engineer' AND score >= .5)")
        self.assertEqual(self.payload()["steps"][-1]["shown"], 5)
        self.cli("--use", "s1")
        # The cloud prompt's spellings match: enums are lower-cased, all_current_titles joins every current
        # position, total_years_experience and summary are there, and a `reasoning` column rides on the row.
        self.cli("--criteria", "series B, no founders, 5+ years", "--sql",
                 "SELECT DISTINCT c.person_id, ROUND(c.total_years_experience, 1) || ' years exp' AS reasoning "
                 "FROM candidates c JOIN positions p USING (person_id) WHERE p.company_stage = 'series_b' "
                 "AND list_contains(p.company_sector_types, 'ai_ml') AND c.all_current_titles NOT ILIKE '%founder%' "
                 "AND c.total_years_experience >= 5 AND c.summary IS NOT NULL ORDER BY c.person_id")
        self.assertEqual([(row["person_id"], row["overall_reasoning"]) for row in self.step_rows()],
                         [(self.ids[2], "5.0 years exp"), (self.ids[3], "7.5 years exp")])
        self.assertEqual(self.payload()["steps"][-1]["query_type"], "filter")
        before = self.snapshot()
        for sql in ("SELECT nope FROM candidates", "SELECT count(*) FROM candidates"):
            _, proc = self.cli("--criteria", "broken", "--sql", sql, expected=1)
            self.assertIn("nope" if "nope" in sql else "person_id", json.loads(proc.stdout)["error"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
