"""JD capability ranking replaces trait reranking and Gemma, keeping integer ratings."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.search.primitives.deep_search import search_harness as harness
from packs.search.primitives.llm_rerank_candidates import llm_rerank_candidates as reranker
from packs.search.primitives.persist_search_results import results_io
from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline


class TerraPipelineTests(unittest.TestCase):
    def test_both_backends_preserve_filter_behavior_for_jd_and_non_jd(self):
        for backend in ("powerset", "local"):
            for jd_mode in (True, False):
                with self.subTest(backend=backend, jd=jd_mode), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    state, db, jd = root / "state.json", root / "local.duckdb", root / "jd.txt"
                    db.touch()
                    jd.write_text("Build storage systems.")
                    payload = {"normalized_query": "Engineers", "traits": [],
                               "role_search_filters": {"semantic_query": "Engineers building storage systems"}}
                    state.write_text(json.dumps({"query": "Engineers", "steps": [
                        {"id": "expand_search_request", "status": "completed", "output": payload}]}))
                    args = pipeline.build_parser().parse_args(["run", "--backend", backend,
                        "--state", str(state), "--ledger", str(root / "pipeline.json"), "--db", str(db),
                        "--confirm-llm", "--filter-batch-size", "7",
                        *(["--jd-file", str(jd)] if jd_mode else [])])
                    result = {"returncode": 0, "json": {**payload, "state": str(state),
                              "hydrated": 1, "passed_count": 1}}
                    with mock.patch.dict(os.environ, {}, clear=True), \
                            mock.patch.object(pipeline, "ROOT", root), \
                            mock.patch.object(pipeline, "configure_local_backend_mode"), \
                            mock.patch.object(pipeline, "apply_local_title_clustering", side_effect=lambda p, db: p), \
                            mock.patch.object(pipeline, "run", return_value=result) as run:
                        output = (pipeline.run_pipeline_local if backend == "local" else pipeline.run_pipeline)(args)
                    self.assertEqual(output["status"], "completed")
                    commands = [call.args[0] for call in run.call_args_list
                                if Path(call.args[0][1]).stem == "llm_filter_candidates"]
                    self.assertEqual(len(commands), 1)
                    command = commands[0]
                    self.assertEqual(command[command.index("--profile-scope") + 1], "auto")
                    self.assertNotIn("--on-error", command)
                    if backend == "powerset":
                        self.assertEqual(command[command.index("--batch-size") + 1], "7")
                    else:
                        self.assertNotIn("--batch-size", command)

    def test_resuming_reviewed_query_preserves_the_state_with_paid_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "scored-state.json"
            state_path.write_text(json.dumps({"steps": [{"id": "llm_rerank_candidates",
                "status": "completed", "output": {"ranked_candidate_ids": ["synthetic"]}}]}))
            original = state_path.read_bytes()
            for backend in ("powerset", "local"):
                args = pipeline.build_parser().parse_args(["run", "--backend", backend,
                    "--query", "Engineers", "--payload-json", str(root / "payload.json")])
                ledger = {"state": str(state_path), "steps": {"llm_rerank_candidates": {"status": "completed"}}}
                with mock.patch.object(pipeline, "run", side_effect=AssertionError("Must not replace existing state")):
                    resumed = (pipeline.init_state(args, root / "ledger.json", ledger) if backend == "powerset"
                               else pipeline.init_state_local(args, root / "ledger.json", ledger, {}, {}))
                self.assertEqual(resumed, state_path)
                self.assertEqual(state_path.read_bytes(), original)

    def test_cli_full_profiles_integer_export_and_downstream_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles_path, state_path, jd_path = [root / name for name in ("profiles.jsonl", "state.json", "jd.txt")]
            profiles = [{"person_id": pid, "positions": [
                {"title": "Engineer", "description": "Current original evidence"},
                {"title": "Engineer", "description": "Earlier original evidence"}]} for pid in ("yes", "no", "filtered")]
            profiles_path.write_text("\n".join(map(json.dumps, profiles)))
            jd_path.write_text("Own distributed storage and recovery protocols.")
            state_path.write_text(json.dumps({"task_id": "synthetic-terra", "query": "software engineers", "steps": [
                {"id": "expand_search_request", "output": {"traits": [{"value": "Software engineer"}]}},
                {"id": "hydrate_people", "status": "completed", "output": {
                    "profiles_path": str(profiles_path), "profile_ids": ["yes", "no", "filtered"]}},
                {"id": "llm_filter_candidates", "status": "completed", "output": {
                    "passed_candidate_ids": ["yes", "no"], "passed_count": 2}},
            ]}))
            native = {"status": "ok", "model": reranker.terra.MODEL, "score_type": reranker.terra.SCORE_TYPE,
                "scores": [{"id": "yes", "score": 4, "evidence": "Relevant work", "basis": "direct"},
                           {"id": "no", "score": 2, "evidence": "Different work", "basis": "mismatch"}]}
            structured = "Title: Storage Engineer\nHiring company: Example Systems\n\nResponsibilities\n- Own storage.\n"
            with mock.patch.object(sys, "argv", ["rerank", "--state", str(state_path), "--write-state",
                    "--jd-file", str(jd_path), "--job-title", "Storage Engineer", "--job-company", "Example Systems",
                    "--capability-judge", "terra", "--api-key", "synthetic-key", "--cross-encoder-beta",
                    "--evaluation-query", "Engineers; exclude frontend-only experience"]), \
                    mock.patch.object(reranker.jd_cleaner, "clean_job_description",
                                      return_value=structured) as cleaner, \
                    mock.patch.object(reranker.terra, "score_candidates", new_callable=mock.AsyncMock, return_value=native) as terra, \
                    mock.patch.object(reranker, "rerank_all") as luna, \
                    mock.patch.object(reranker.cross_encoder, "score_candidates") as gemma, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reranker.main(), 0)
            luna.assert_not_called()
            gemma.assert_not_called()
            cleaner.assert_called_once_with(
                jd=jd_path.read_text(), title="Storage Engineer", company_name="Example Systems",
                output_dir=mock.ANY, api_key="synthetic-key")
            self.assertEqual(terra.call_args.kwargs["profiles"], {p["person_id"]: p for p in profiles[:2]})
            self.assertEqual(terra.call_args.kwargs["jd"], structured
                + "\nUser-reviewed criteria:\nEngineers; exclude frontend-only experience")
            self.assertEqual(jd_path.read_text(), "Own distributed storage and recovery protocols.")
            rows = results_io.result_rows(json.loads(state_path.read_text()))
            self.assertEqual([r["person_id"] for r in rows], ["yes", "no"])
            self.assertEqual([r["cross_encoder_score_1_to_5"] for r in rows], [4, 2])
            self.assertEqual([harness._candidate_judgment_eligible(r) for r in rows], [True, False])
            self.assertEqual([float(r["final_score"]) for r in rows], [.8, .4])
            self.assertTrue(all(r["cross_encoder_model"] == reranker.terra.MODEL for r in rows))
            saved = json.loads(state_path.read_text())["steps"][-1]["output"]
            self.assertEqual(saved["model"], "gpt-5.6-luna")
            self.assertEqual(saved["reasoning_effort"], "low")
            self.assertTrue(all(json.loads(r["trait_scores"]) == {} for r in rows))

    def test_non_jd_luna_preserves_overall_trait_fallback(self):
        result = reranker.RerankResult(id="synthetic", score=.6, verdict="pass", reason="Relevant",
            model="gpt-5.6-luna", elapsed_ms=0, input={})
        rows = reranker.build_query_result_rows([result], state={}, query="Engineer", created_at="2026-09-17")
        self.assertEqual(rows[0]["trait_scores"]["overall"]["score"], .6)

    def test_prepare_carries_jd_and_omits_gemma_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            jd = Path(directory) / "jd.txt"
            jd.write_text("Synthetic JD")
            cleaner_dir = Path(directory) / "structured-jd"
            for command in ("prepare", "run"):
                args = pipeline.build_parser().parse_args([command, "--query", "Engineers",
                    "--jd-file", str(jd), "--job-title", "Engineer", "--job-company", "Example",
                    "--jd-cleaner-output-dir", str(cleaner_dir),
                    "--cross-encoder-beta"])
                self.assertEqual(pipeline.cross_encoder_child_args(args), [
                    "--jd-file", str(jd.resolve()), "--job-title", "Engineer", "--job-company", "Example",
                    "--jd-cleaner-output-dir", str(cleaner_dir)])
                if command == "run":
                    approval = pipeline._llm_approval_payload(args, Path("unused"))
                    self.assertEqual(approval["model"], "gpt-5.6-luna")
                    self.assertEqual(approval["reasoning_effort"], "low")
                    self.assertEqual(approval["jd_cleaner_model"], "gpt-5.6-sol")
                    self.assertEqual(approval["jd_cleaner_reasoning_effort"], "high")
                    self.assertEqual(approval["jd_cleaner_output_dir"], str(cleaner_dir))
                    self.assertNotIn("cross_encoder_beta", approval)

    def test_jd_dry_run_shows_cleaner_request_without_calling_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows, jd = root / "profiles.jsonl", root / "jd.txt"
            rows.write_text(json.dumps({"person_id": "synthetic"}) + "\n")
            jd.write_text("Build storage systems. Benefits: free lunch.")
            stderr = io.StringIO()
            with mock.patch.object(sys, "argv", [
                    "rerank", "--in", str(rows), "--query", "Storage engineers",
                    "--jd-file", str(jd), "--job-title", "Storage Engineer",
                    "--job-company", "Example Systems", "--dry-run",
                    ]), mock.patch.object(
                        reranker.jd_cleaner, "clean_job_description") as cleaner, \
                    mock.patch.object(reranker.terra, "score_candidates") as scorer, \
                    contextlib.redirect_stderr(stderr):
                self.assertEqual(reranker.main(), 0)

        cleaner.assert_not_called()
        scorer.assert_not_called()
        self.assertIn("structured JD request", stderr.getvalue())
        self.assertIn("Build storage systems. Benefits: free lunch.", stderr.getvalue())
        self.assertIn("Capability prompts are built from the cached structured output", stderr.getvalue())

    def test_api_failure_does_not_write_negative_results(self):
        import asyncio
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(reranker.jd_cleaner, "clean_job_description", return_value="Clean JD"), \
                mock.patch.object(reranker.terra, "score_candidates", side_effect=RuntimeError("unavailable")), \
                self.assertRaisesRegex(RuntimeError, "unavailable"):
            asyncio.run(reranker._rerank_with_terra(
                [reranker.RerankItem(position=0, payload={"person_id": "synthetic"})],
                jd="JD", title="Engineer", company_name="Example", evaluation_query="",
                as_of="2026-09-16", output_dir=Path(directory), cleaner_output_dir=Path(directory) / "jd",
                api_key="test", concurrency=1))
