from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline
from packs.shared import powerset_api


def lookup_payload(name="Jordan Bravo"):
    return {"intent_type": "person_lookup", "person_name": name,
            "normalized_query": f"who is {name}", "role_search_filters": {}, "traits": []}


class PersonLookupTests(unittest.TestCase):
    def test_prepare_shows_name_and_command_without_resolving_roles(self):
        for backend in ("local", "powerset"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as raw:
                directory = Path(raw)
                db = directory / "index.duckdb"
                with duckdb.connect(str(db)):
                    pass
                args = pipeline.build_parser().parse_args([
                    "prepare", "--query", "who is Jordan Bravo", "--backend", backend,
                    "--db", str(db), "--set-id", "s1", "--output-dir", raw,
                ])
                with mock.patch.object(pipeline, "_expand_query", return_value=lookup_payload()), \
                     mock.patch.object(pipeline, "emit") as emit, \
                     mock.patch.object(powerset_api, "call", side_effect=AssertionError("no API during prepare")), \
                     mock.patch.object(pipeline, "_local_title_clusters", side_effect=AssertionError("no titles")), \
                     mock.patch.object(pipeline, "local_pool_estimate", side_effect=AssertionError("no filters")), \
                     mock.patch.dict("os.environ"):
                    self.assertEqual(pipeline.cmd_prepare(args), 0)
                output = emit.call_args.args[0]
                self.assertEqual(output["preview"]["person_name"], "Jordan Bravo")
                self.assertEqual(output["status"], "preview_ready")
                self.assertIn("--payload-json", output["execute_command"])
                self.assertEqual(json.loads(Path(output["payload_json"]).read_text())["intent_type"], "person_lookup")

    def _run(self, directory, backend, name="Jordan Bravo"):
        payload = lookup_payload(name)
        state_path = directory / "state.json"
        state_path.write_text(json.dumps({"task_id": "lookup", "query": payload["normalized_query"],
                                         "steps": [{"id": "expand_search_request", "output": payload}]}))
        args = pipeline.build_parser().parse_args([
            "run", "--backend", backend, "--db", str(directory / "index.duckdb"),
            "--set-id", "s1", "--state", str(state_path), "--env-file", "/dev/null",
        ])
        run = pipeline.run

        def export_only(cmd, **kwargs):
            self.assertIn("results_io.py", cmd[1])
            return run(cmd, **kwargs)

        with mock.patch.object(pipeline, "emit") as emit, mock.patch.dict("os.environ"), \
             mock.patch.object(pipeline, "run", side_effect=export_only):
            code = pipeline.cmd_run(args)
        self.assertEqual(code, 0, emit.call_args.args[0])
        output = emit.call_args.args[0]
        self.assertEqual(output["status"], "completed")
        state = json.loads(state_path.read_text())
        ids = [step["id"] for step in state["steps"]]
        self.assertFalse(set(ids) & {"execute_role_search", "apply_prefilters", "llm_filter_candidates", "llm_rerank_candidates"})
        rows = [json.loads(line) for line in Path(output["artifacts"]["jsonl"]).read_text().splitlines()]
        self.assertTrue(Path(output["artifacts"]["csv"]).exists())
        self.assertEqual(json.loads(Path(output["artifacts"]["manifest"]).read_text())["row_count"], len(rows))
        return output, rows

    def test_local_lookup_matches_all_words_case_insensitively_without_api_or_model(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            with duckdb.connect(str(directory / "index.duckdb")) as conn:
                conn.execute("CREATE TABLE local_person_profiles (person_id VARCHAR, full_name VARCHAR, hydrated_context JSON)")
                for person_id, name in [("p2", "Jordan Z Bravo"), ("p3", "Jordan Delta"), ("p1", "BRAVO jordan")]:
                    conn.execute("INSERT INTO local_person_profiles VALUES (?, ?, ?)",
                                 [person_id, name, json.dumps({"name": name})])
            with mock.patch.object(powerset_api, "call", side_effect=AssertionError("no API")), \
                 mock.patch.object(pipeline, "_expand_query", side_effect=AssertionError("no model")), \
                 mock.patch.object(pipeline, "_local_title_clusters", side_effect=AssertionError("no titles")):
                output, rows = self._run(directory, "local")
            self.assertEqual([row["person_id"] for row in rows], ["p1", "p2"])
            self.assertEqual(output["summary"]["returned_people"], 2)
            with gzip.open(output["artifacts"]["profiles_path"], "rt") as handle:
                self.assertEqual(len(handle.readlines()), 2)

    def test_set_lookup_only_reads_contacts_and_filters_names(self):
        leads = [{"person_id": "p2", "name": "Jordan Z Bravo", "title": "Engineer", "operators": []},
                 {"person_id": "p3", "name": "Casey Delta", "title": "Jordan Bravo", "operators": []},
                 {"person_id": "p1", "name": "BRAVO jordan", "operators": []}]
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch.object(powerset_api, "call", return_value={"leads": leads}) as api, \
             mock.patch.object(pipeline, "_expand_query", side_effect=AssertionError("no model")):
            output, rows = self._run(Path(raw), "powerset")
            self.assertEqual([row["person_id"] for row in rows], ["p1", "p2"])
            self.assertEqual(rows[1]["headline"], "Engineer")
            api.assert_called_once()
            self.assertEqual(api.call_args.args[1], "/v2/set-contacts/s1?search=Jordan%20Bravo&page_size=200")
            # The results-page registration must not fetch attribution from another endpoint.
            pipeline.quick_run_attribution({}, Path(output["state"]), output["artifacts"]["jsonl"], None)
            api.assert_called_once()

    def test_local_cli_runs_prepared_payload_without_a_powerset_account(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            db = directory / "index.duckdb"
            with duckdb.connect(str(db)) as conn:
                conn.execute("CREATE TABLE local_person_profiles (person_id VARCHAR, full_name VARCHAR, hydrated_context JSON)")
                conn.execute("INSERT INTO local_person_profiles VALUES ('p1', 'Jordan Bravo', ?)",
                             [json.dumps({"name": "Jordan Bravo"})])
            payload = directory / "payload.json"
            payload.write_text(json.dumps(lookup_payload()))
            proc = subprocess.run([
                sys.executable, str(Path(pipeline.__file__)), "run", "--backend", "local", "--db", str(db),
                "--query", "who is Jordan Bravo", "--payload-json", str(payload),
                "--ledger", str(directory / "pipeline.json"),
            ], cwd=pipeline.ROOT, capture_output=True, text=True, timeout=10,
               env={**os.environ, "POWERPACKS_CREDENTIALS_PATH": str(directory / "signed-out.json"),
                    "OPENAI_API_KEY": "", "POWERSET_API_URL": "http://127.0.0.1:1"})
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            output = json.loads(proc.stdout)
            self.assertEqual(output["summary"]["returned_people"], 1)
            rows = [json.loads(line) for line in Path(output["artifacts"]["jsonl"]).read_text().splitlines()]
            self.assertEqual(rows[0]["name"], "Jordan Bravo")
            state = json.loads(Path(output["state"]).read_text())
            self.assertEqual([step["id"] for step in state["steps"]],
                             ["expand_search_request", "person_lookup", "hydrate_people"])

    def test_signed_out_set_lookup_cli_emits_failed_json(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            payload = directory / "payload.json"
            payload.write_text(json.dumps(lookup_payload()))
            proc = subprocess.run([
                sys.executable, str(Path(pipeline.__file__)), "run", "--backend", "powerset",
                "--set-id", "s1", "--query", "who is Jordan Bravo", "--payload-json", str(payload),
                "--ledger", str(directory / "pipeline.json"), "--env-file", "/dev/null",
            ], cwd=pipeline.ROOT, capture_output=True, text=True, timeout=10,
               env={**os.environ, "POWERPACKS_CREDENTIALS_PATH": str(directory / "signed-out.json"),
                    "OPENAI_API_KEY": "", "POWERSET_API_URL": "http://127.0.0.1:1"})
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertEqual(json.loads(proc.stdout), {
                "primitive": "search_network_pipeline", "status": "failed",
                "error": "not signed in to Powerset; run `$powerset login` first",
            })

    def test_nobody_found_is_empty_and_successful_for_both_backends(self):
        for backend in ("local", "powerset"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as raw:
                directory = Path(raw)
                with duckdb.connect(str(directory / "index.duckdb")) as conn:
                    conn.execute("CREATE TABLE local_person_profiles (person_id VARCHAR, full_name VARCHAR, hydrated_context JSON)")
                with mock.patch.object(powerset_api, "call", return_value={"leads": []}):
                    output, rows = self._run(directory, backend)
                self.assertEqual(rows, [])
                self.assertEqual(output["summary"]["returned_people"], 0)
                self.assertEqual(output["message"], "No people found named Jordan Bravo.")
