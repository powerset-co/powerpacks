"""Plain searches publish screening before LLM ranking; JD score exports stay native."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from packs.search.primitives.deep_search import search_harness
from packs.search.primitives.deep_search.results_web import model
from packs.search.primitives.llm_filter_candidates import llm_filter_candidates as screening
from packs.search.primitives.llm_rerank_candidates import llm_rerank_candidates as reranker
from packs.search.primitives.persist_search_results import results_io
from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline
from tests.test_search_harness import _start, _payload


class CrossEncoderSearchTests(unittest.TestCase):
    def test_qualification_score_export_stays_native_and_persists_decision_metadata(self):
        threshold = 0.29855554570561965
        state = {"steps": [
            {"id": "hydrate_people", "output": {"profiles": [{"person_id": "first"}]}},
            {"id": "llm_rerank_candidates", "output": {
                "ranked_candidate_ids": ["first"],
                "cross_encoder": {
                    "status": "ok", "model": "jev", "score_type": "qualification_score",
                    "threshold": threshold,
                    "scores": [{"id": "first", "score": 0.42, "passed": True}],
                }}},
        ]}

        row = results_io.result_rows(state)[0]

        self.assertEqual(row["cross_encoder_score"], 0.42)
        self.assertIsNone(row["cross_encoder_score_1_to_5"])
        self.assertEqual(row["cross_encoder_score_type"], "qualification_score")
        self.assertEqual(row["cross_encoder_threshold"], threshold)
        self.assertIs(row["cross_encoder_passed"], True)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.csv"
            results_io.write_csv(path, [row])
            exported = path.read_text().splitlines()
        self.assertIn("cross_encoder_score_type", exported[0])
        self.assertIn("cross_encoder_threshold", exported[0])
        self.assertIn("cross_encoder_passed", exported[0])
        self.assertIn("qualification_score", exported[1])

    def test_native_rating_export_does_not_apply_qwen_sigmoid(self):
        state = {"steps": [
            {"id": "hydrate_people", "output": {"profiles": [{"person_id": "first"}]}},
            {"id": "llm_rerank_candidates", "output": {
                "ranked_candidate_ids": ["first"],
                "cross_encoder": {"status": "ok", "model": "gemma", "score_type": "expected_rating_1_to_5",
                                  "scores": [{"id": "first", "score": 2.25}]}}},
        ]}
        row = results_io.result_rows(state)[0]
        self.assertEqual(row["cross_encoder_score"], 2.25)
        self.assertEqual(row["cross_encoder_score_1_to_5"], 2.25)

    def test_deep_pond_uses_jev_and_original_jd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results_path = _start(root)
            payload = root / "payload.json"
            payload.write_text(json.dumps(_payload()))
            results = json.loads(results_path.read_text())
            results["status"] = "ready_to_run"
            results["pending_payload"] = {"pond_n": 1, "query": "software engineers",
                "payload_json": str(payload), "ledger": str(root / "pipeline.json"), "limit": 1000}
            results_path.write_text(json.dumps(results))
            with mock.patch.object(search_harness, "_run_command", side_effect=RuntimeError("captured")) as run, \
                    self.assertRaisesRegex(RuntimeError, "captured"):
                search_harness.run_pond(run_dir=root, env_file="/dev/null")
            command = run.call_args.args[0]
            self.assertNotIn("--cross-encoder-beta", command)
            self.assertEqual(command[command.index("--jd-file") + 1], str(root / "jd.txt"))
            self.assertEqual(command[command.index("--job-title") + 1], "Search Engineer")
            self.assertEqual(command[command.index("--job-company") + 1], "Acme")
            self.assertEqual(command[command.index("--model") + 1], "jev-1.13.0")
            self.assertEqual(command[command.index("--capability-judge") + 1], "jev")


class PlainSearchPipelineTests(unittest.TestCase):
    def test_prepare_has_no_cross_encoder_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {"normalized_query": "Storage engineers", "traits": [],
                       "role_search_filters": {"semantic_query": "Engineers who build and operate distributed storage systems, recovery protocols and database infrastructure"}}
            stdout = io.StringIO()
            with mock.patch.object(pipeline, "run", return_value={"returncode": 0, "json": payload}), \
                    contextlib.redirect_stdout(stdout):
                args = pipeline.build_parser().parse_args([
                    "prepare", "--query", "Storage engineers", "--output-dir", directory])
                self.assertEqual(pipeline.cmd_prepare(args), 0)
            output = json.loads(stdout.getvalue())
            self.assertEqual(output["status"], "preview_ready")
            self.assertNotIn("--cross-encoder", output["execute_command"])

    def test_screened_catalog_running_then_completed_with_1434_ranked_people(self):
        for backend in ("powerset", "local"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                self._run_plain_fixture(Path(directory), backend)

    def test_ranking_failure_marks_screened_catalog_failed(self):
        for backend in ("powerset", "local"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                self._run_plain_fixture(Path(directory), backend, ranking_fails=True)

    def test_forced_rerun_keeps_previous_ranked_results_until_ranking_finishes(self):
        for backend in ("powerset", "local"):
            for flag in ("--force", "--force-llm"):
                for rerank_done in (True, False):
                    with self.subTest(backend=backend, flag=flag, rerank_done=rerank_done), \
                            tempfile.TemporaryDirectory() as directory:
                        self._run_plain_fixture(Path(directory), backend, rerun_flag=flag, rerank_done=rerank_done)

    def _run_plain_fixture(self, root, backend, *, ranking_fails=False, rerun_flag=None, rerank_done=True):
        run_dir = root / "search" / "synthetic-storage"
        run_dir.mkdir(parents=True)
        state_path, ledger_path = run_dir / "state.json", run_dir / "pipeline.json"
        profiles_path, db = run_dir / "profiles.jsonl", root / "local.duckdb"
        db.touch()
        ids = [f"synthetic-{index}" for index in range(1434)]
        profiles = [{"person_id": pid, "name": "Jordan Bravo", "education": [], "positions": [
            {"title": "Engineer", "description": "Built storage systems"}]} for pid in [*ids, "rejected"]]
        profiles_path.write_text("\n".join(map(json.dumps, profiles)) + "\n")
        payload = {"normalized_query": "Storage engineers", "traits": [],
                   "role_search_filters": {"semantic_query": "Engineers who build and operate distributed storage systems, recovery protocols and database infrastructure"}}
        hydrated = {"profiles_path": str(profiles_path), "profile_ids": [p["person_id"] for p in profiles],
                    "hydrated": len(profiles)}
        state_path.write_text(json.dumps({"task_id": "synthetic-storage", "query": "Storage engineers", "steps": [
            {"id": "expand_search_request", "status": "completed", "output": payload},
            {"id": "hydrate_people", "status": "completed", "output": hydrated}]}))
        scores = {pid: {"person_id": pid, "score": 0.8, "reason": "Relevant work"} for pid in ids}
        scores["rejected"] = {"person_id": "rejected", "score": 0.0, "reason": "Unrelated work"}
        stages = []
        catalog_root = root / "deep-search"
        stderr = io.StringIO()
        forced_rerun = False

        async def rank(items, **kwargs):
            # Observe the real catalog and exported viewer rows before ranking returns.
            card, = model.load_catalog(catalog_root)
            search = model.load_search(catalog_root, run_dir.name)
            saved = pipeline.read_json(ledger_path)
            rows = results_io.read_jsonl(Path(saved["artifacts"]["jsonl"]))
            if forced_rerun:
                self.assertEqual((card.status, card.candidates), ("completed", 1434))
                self.assertEqual([row.person_id for row in search.ponds[0].candidates], list(reversed(ids)))
                self.assertTrue(all(row["final_score"] != "" for row in rows))
                self.assertNotIn("screened, not ranked yet", stderr.getvalue())
                self.assertNotIn("results_io", stages)
            else:
                self.assertEqual((card.status, card.candidates), ("running", 1434))
                self.assertEqual([row.person_id for row in search.ponds[0].candidates], ids)
                self.assertFalse(pipeline.done(saved, "persist_search_results"))
                self.assertTrue(all(row["final_score"] == "" for row in rows))
                self.assertIn("1434 screened, not ranked yet", stderr.getvalue())
            self.assertEqual([item.id for item in items], ids)
            if ranking_fails:
                raise RuntimeError("Ranking unavailable")
            return [reranker.RerankResult(id=item.id, score=index / len(items), verdict="pass",
                    reason="Relevant work", model="synthetic", elapsed_ms=0, input=item.payload)
                    for index, item in enumerate(items)]

        def child(command, **kwargs):
            stage = Path(command[1]).stem
            stages.append(stage)
            output = io.StringIO()
            with mock.patch.object(sys, "argv", command[1:]), contextlib.redirect_stdout(output):
                if stage == "llm_filter_candidates":
                    screening.main()
                elif stage == "llm_rerank_candidates":
                    self.assertEqual(reranker.main(), 0)
                elif stage == "results_io":
                    results_io.main()
                else:
                    self.assertIn(stage, ("resolve_set_operators", "apply_prefilters", "execute_role_search", "hydrate_people"))
                    return {"returncode": 0, "json": hydrated if stage == "hydrate_people" else {}}
            return {"returncode": 0, "json": json.loads(output.getvalue())}

        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic", "POWERSET_API_KEY": "synthetic"}), \
                mock.patch.object(pipeline, "QUICK_SEARCH_ROOT", root / "search"), \
                mock.patch.object(pipeline, "DEEP_SEARCH_ROOT", catalog_root), \
                mock.patch.object(pipeline, "configure_local_backend_mode"), \
                mock.patch.object(pipeline, "apply_local_title_clustering", side_effect=lambda p, db: p), \
                mock.patch.object(pipeline, "run", side_effect=child), \
                mock.patch.object(screening, "score_batches", new_callable=mock.AsyncMock, return_value=([], scores)) as filtered, \
                mock.patch.object(reranker, "rerank_all", side_effect=rank) as ranked, \
                mock.patch.object(httpx.Client, "send", side_effect=AssertionError("No network")) as sync_send, \
                mock.patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("No network")) as async_send, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            args = pipeline.build_parser().parse_args([
                "run", "--backend", backend, "--state", str(state_path), "--ledger", str(ledger_path),
                "--db", str(db), "--execute-approved"])
            self.assertEqual(pipeline.cmd_run(args), 1 if ranking_fails else 0, stdout.getvalue())
            if rerun_flag:
                forced_rerun = True
                stages.clear()
                stderr.seek(0)
                stderr.truncate()
                filtered.reset_mock()
                ranked.reset_mock()
                if not rerank_done:
                    saved = pipeline.read_json(ledger_path)
                    saved["steps"]["llm_rerank_candidates"]["status"] = "running"
                    pipeline.save(ledger_path, saved)
                setattr(args, rerun_flag.removeprefix("--").replace("-", "_"), True)
                self.assertEqual(pipeline.cmd_run(args), 0, stdout.getvalue())
        filtered.assert_awaited_once()
        ranked.assert_called_once()
        sync_send.assert_not_called()
        async_send.assert_not_called()
        if ranking_fails:
            card, = model.load_catalog(catalog_root)
            self.assertEqual((card.status, card.candidates), ("failed", 1434))
            search = model.load_search(catalog_root, run_dir.name)
            self.assertEqual([row.person_id for row in search.ponds[0].candidates], ids)
            self.assertIn("Ranking unavailable", json.loads(stdout.getvalue())["error"])
            self.assertFalse(pipeline.done(pipeline.read_json(ledger_path), "persist_search_results"))
            return
        self.assertEqual(stages.count("results_io"), 1 if forced_rerun else 2)
        self.assertLess(stages.index("llm_filter_candidates"), stages.index("results_io"))
        if not forced_rerun:
            self.assertLess(stages.index("results_io"), stages.index("llm_rerank_candidates"))
        card, = model.load_catalog(catalog_root)
        self.assertEqual((card.status, card.candidates), ("completed", 1434))
        search = model.load_search(catalog_root, run_dir.name)
        self.assertEqual([row.person_id for row in search.ponds[0].candidates], list(reversed(ids)))
        self.assertTrue(pipeline.done(pipeline.read_json(ledger_path), "persist_search_results"))
        self.assertIn("1434 ranked", stderr.getvalue())
