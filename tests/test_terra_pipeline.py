"""JD ranking replaces both Luna reranking and Gemma, keeping native integer ratings."""
import contextlib
import io
import json
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
                    "--api-key", "synthetic-key", "--cross-encoder-beta",
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
                    self.assertEqual(approval["model"], "gpt-5.6-terra")
                    self.assertEqual(approval["reasoning_effort"], "high")
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
