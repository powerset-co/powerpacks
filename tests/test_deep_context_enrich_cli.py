"""Offline enrichment command, receipts, and saved-work reuse."""

import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from deep_context_sqlite_test_helpers import seed_identity
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich import enrichment_pipeline as pipeline_module
from packs.ingestion.primitives.deep_context.manifests.receipt_status import ReceiptStatus
from packs.ingestion.primitives.deep_context.review import cli as review_cli


STEPS = ["research", "profiles", "identity", "relationships", "settle", "synthetic"]


class EnrichCommandTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.manifest = self.root / "deep-research" / "manifest.json"

    def pipeline(self, **kwargs):
        return pipeline_module.EnrichmentPipeline(self.db, manifest=self.manifest, **kwargs)

    def mock_steps(self, seen, *, fail=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        outcomes = {
            "research": SimpleNamespace(status=ReceiptStatus.REUSED),
            "profiles": SimpleNamespace(status="completed"),
            "identity": SimpleNamespace(judge_errors=0),
            "relationships": {"status": "completed"},
            "settle": None,
            "synthetic": None,
        }
        for phase, target in zip(STEPS, (
            "ReconcileDeepResearch.run", "PrefetchProfiles.run", "judge_mapped_candidates",
            "ReviewRelationships.run", "SettleEnrichment.run", "AssembleSyntheticProfile.run",
        )):
            def step(*args, phase=phase, **kwargs):
                receipt = json.loads(self.manifest.read_text())
                self.assertEqual((receipt["status"], receipt["phase"]), ("running", phase))
                seen.append(phase)
                if phase == fail:
                    raise RuntimeError("fixture failure")
                return outcomes[phase]
            stack.enter_context(mock.patch(
                f"{pipeline_module.__name__}.{target}", side_effect=step,
            ))
        return stack

    def test_order_and_receipts(self):
        seen = []
        self.mock_steps(seen)
        result = self.pipeline().run(total=1, budget=0, request_fingerprint="fixture")
        self.assertEqual(seen, STEPS)
        self.assertEqual(result["steps"], STEPS)
        self.assertEqual(result["errors"], [])
        receipt = json.loads(self.manifest.read_text())
        self.assertEqual((receipt["status"], receipt["phase"]), ("completed", "synthetic"))

    def test_failure_propagates_and_retry_starts_at_research(self):
        seen = []
        patches = self.mock_steps(seen, fail="identity")
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            self.pipeline().run(total=1, budget=0, request_fingerprint="fixture")
        failed = json.loads(self.manifest.read_text())
        self.assertEqual((failed["status"], failed["phase"]), ("failed", "identity"))
        self.assertIn("fixture failure", failed["error"])
        patches.close()
        seen.clear()
        self.mock_steps(seen)
        result = self.pipeline().run(total=1, budget=0, request_fingerprint="fixture")
        self.assertEqual(seen, STEPS)
        self.assertEqual(result["status"], "completed")

    def test_running_receipt_is_not_a_checkpoint(self):
        # An earlier run died mid-step: the next run starts at the first step all the same.
        self.manifest.parent.mkdir()
        self.manifest.write_text(json.dumps({"status": "running", "phase": "settle"}))
        seen = []
        self.mock_steps(seen)
        result = self.pipeline().run(total=0, budget=0, request_fingerprint="fixture")
        self.assertEqual(seen, STEPS)
        self.assertEqual(result["status"], "completed")

    def test_server_uses_same_sequence(self):
        seen = []
        self.mock_steps(seen)
        finished = threading.Event()
        pipeline = self.pipeline(on_change=lambda: None, on_finish=finished.set)
        with mock.patch.object(pipeline, "run", wraps=pipeline.run) as run:
            self.assertTrue(pipeline.start(1, 0, "fixture"))
            self.assertTrue(finished.wait(3))
        run.assert_called_once()
        self.assertEqual(seen, STEPS)
        self.assertFalse(pipeline.running())
        self.assertIsNone(pipeline.last_error)
        self.assertIsNotNone(pipeline.applied_fingerprint)

    def test_dry_run_writes_no_stage_output_and_has_one_total(self):
        from packs.ingestion.primitives.deep_context.enrich import cli
        seed_identity(self.db, parent_id="parent", person_id="person", row_key="candidate:person",
            name="Jordan Bravo", machine_worth="yes", include_link=False)
        seed_identity(self.db, parent_id="attached", person_id="person:attached", row_key="casey-delta",
            name="Casey Delta", machine_worth="yes", linkedin_url="https://www.linkedin.com/in/casey-delta")
        rows = lambda: [tuple(row) for table in ("parents", "links", "research", "artifacts")
            for row in self.db.query(f"SELECT * FROM {table} ORDER BY 1")]
        before = rows()

        with (
            mock.patch.object(cli, "load_env", side_effect=AssertionError("dry run needs no env")),
            mock.patch.object(pipeline_module.EnrichmentPipeline, "run", side_effect=AssertionError("no work")),
            redirect_stdout(io.StringIO()) as out,
        ):
            code = cli.main(["--dry-run", "--db", str(self.db.db_path), "--manifest", str(self.manifest)])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["would_submit"], 1)
        self.assertEqual(payload["profile_fetches"], 1)
        self.assertEqual(payload["estimated_usd"], payload["parallel_estimated_usd"]
            + payload["judgment_estimated_usd"] + payload["jev_estimated_usd"])
        # Counts only: the plan names nobody.
        self.assertNotIn("Jordan", out.getvalue())
        self.assertEqual(rows(), before)
        self.assertFalse(self.manifest.exists())

    def test_cli_sets_parallel_budget_and_emits_next_action(self):
        from packs.ingestion.primitives.deep_context.enrich import cli
        seed_identity(self.db, parent_id="parent", person_id="person", row_key="candidate:person",
            name="Jordan Bravo", machine_worth="yes", include_link=False)
        seen = []
        self.mock_steps(seen)
        with (
            mock.patch.object(cli, "load_env"),
            mock.patch.object(pipeline_module.EnrichmentPipeline, "run",
                wraps=self.pipeline().run) as run,
            redirect_stdout(io.StringIO()) as out,
            redirect_stderr(io.StringIO()) as log,
        ):
            code = cli.main(["--db", str(self.db.db_path), "--manifest", str(self.manifest)])
        payload = json.loads(out.getvalue())
        self.assertEqual((code, payload["status"], payload["next_action"]), (0, "completed", "enrich"))
        self.assertEqual(run.call_args.kwargs["budget"], .05)
        self.assertIn("[enrich] research", log.getvalue())

    def test_cli_failure_is_one_json_and_nonzero(self):
        from packs.ingestion.primitives.deep_context.enrich import cli
        self.mock_steps([], fail="profiles")
        with mock.patch.object(cli, "load_env"), redirect_stdout(io.StringIO()) as out:
            code = cli.main(["--db", str(self.db.db_path), "--manifest", str(self.manifest)])
        payload = json.loads(out.getvalue())
        self.assertNotEqual(code, 0)
        self.assertEqual((payload["status"], payload["phase"]), ("failed", "profiles"))
        self.assertEqual(payload["steps"], ["research", "profiles"])

    def test_cli_completed_with_step_errors_exits_zero(self):
        from packs.ingestion.primitives.deep_context.enrich import cli
        self.mock_steps([])
        with (
            mock.patch.object(cli, "load_env"),
            mock.patch.object(pipeline_module.PrefetchProfiles, "run",
                return_value=SimpleNamespace(status="completed_with_failures", note="fixture deferred")),
            redirect_stdout(io.StringIO()) as out,
        ):
            code = cli.main(["--db", str(self.db.db_path), "--manifest", str(self.manifest)])
        payload = json.loads(out.getvalue())
        self.assertEqual((code, payload["status"]), (0, "completed"))
        self.assertIn("fixture deferred", payload["errors"][0])

    def test_bin_dispatches_to_real_cli_without_uv_or_providers(self):
        # The runner's existing uv invocation uses the prescribed test Python.
        shim = self.root / "uv"
        shim.write_text('#!/bin/sh\nshift 4\nexec /Users/arthur/workspace/powerpacks/.venv/bin/python "$@"\n')
        shim.chmod(0o755)
        result = subprocess.run(["bash", "bin/deep-context", "enrich", "--dry-run",
            "--db", str(self.db.db_path), "--manifest", str(self.manifest)],
            env={**os.environ, "PATH": f"{self.root}:{os.environ['PATH']}"},
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual((payload["status"], payload["estimated_usd"]), ("dry_run", 0))
        self.assertFalse(self.manifest.exists())

    def test_enrich_is_agent_action_and_wait_returns_immediately(self):
        seed_identity(self.db, parent_id="parent", person_id="person", row_key="candidate:person",
            name="Jordan Bravo", machine_worth="yes", include_link=False)
        with (
            mock.patch.object(review_cli, "CANONICAL_DB", self.db.db_path),
            mock.patch.object(review_cli.time, "sleep", side_effect=AssertionError("must not wait")),
            redirect_stdout(io.StringIO()) as out,
        ):
            review_cli.main(["status", "--wait", "--timeout", "3"])
        payload = json.loads(out.getvalue())
        self.assertEqual((payload["next_action"], payload["command"]), ("enrich", "bin/deep-context enrich"))
        self.assertIn("enrich", review_cli._AGENT_ACTIONS)

    def test_unchanged_store_reuses_every_paid_stage_and_later_import_is_new(self):
        from packs.ingestion.primitives.deep_context.enrich.parallel_research import driver
        from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import ResearchRunResult
        from packs.ingestion.primitives.deep_context.enrich.parallel_research.projection import research_artifact_projection
        from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import jev_judge
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.candidate_selection import (
            RelationshipDecision, cache_relationship_judgment,
        )
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
            IdentityJudgeResult, IdentityUsage, IdentityVerdict,
        )
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.relationship import ReviewRelationships
        from packs.ingestion.primitives.deep_context.enrich.profiles.prefetch import PrefetchProfiles
        from packs.ingestion.primitives.deep_context.enrich.estimate import estimate_enrichment
        from packs.ingestion.primitives.deep_context.enrich.research_reconcile.selection import select_research
        from packs.ingestion.primitives.deep_context.enrich.parallel_research.config import DEFAULT_PROCESSOR
        from packs.ingestion.primitives.enrich.rapidapi_client import RapidApiClient
        from packs.ingestion.primitives.deep_context.shared.openai_responses import OpenAIResponsesCaller

        seed_identity(self.db, parent_id="lookup", person_id="person:lookup", row_key="candidate:lookup",
            name="Casey Delta", machine_worth="yes", include_link=False)
        seed_identity(self.db, parent_id="attached", person_id="person:attached", row_key="jordan-bravo",
            name="Jordan Bravo", machine_worth="yes", linkedin_url="https://www.linkedin.com/in/jordan-bravo")

        def research(params):
            for row in params.rows:
                result = ResearchResult.from_payload({"type": "json", "basis": [], "content": {
                    "real_name": "Casey Delta", "location_city": "Austin", "linkedin_url": "",
                    "work_experience": [], "education": [],
                }})
                data = result.output.model_dump_json().encode()
                path = params.output_dir / f"{row.handle}.json"
                path.write_bytes(data)
                self.db.project_rows((research_artifact_projection(params, row, result, path, data),))
            return ResearchRunResult(len(params.rows), len(params.rows))

        async def relationships(stage, tasks):
            records = {}
            for task in tasks:
                decision = RelationshipDecision.from_payload(task.parent_id, task.fingerprint, {
                    "candidates": [{"url": "https://www.linkedin.com/in/jordan-bravo",
                        "verdict": "review", "reason": "Owner can identify colleague", "confidence": .5}],
                })
                cache_relationship_judgment(self.db, decision)
                records[task.parent_id] = decision
            return records, []

        def run():
            plan = select_research(self.db, processor=DEFAULT_PROCESSOR)
            return self.pipeline().run(total=plan.deduped_total, budget=plan.estimated_usd,
                request_fingerprint=plan.request_fingerprint)

        with (
            mock.patch.object(pipeline_module, "PrefetchProfiles", side_effect=lambda **kwargs:
                PrefetchProfiles(**kwargs, profile_cache_dir=self.root / "profile-cache")),
            mock.patch.object(OpenAIResponsesCaller, "__init__", side_effect=AssertionError("no provider")),
            mock.patch.object(RapidApiClient, "resolve_key", return_value="fixture"),
            mock.patch.object(driver, "run_research", side_effect=research) as submitted,
            mock.patch.object(RapidApiClient, "get_profile", return_value={
                "state": "content", "fetched": True, "from_cache": False,
                "normalized_profile": {"success": True, "full_name": "Jordan Bravo",
                    "experiences": [{"title": "Engineer", "company_name": "Bravo Labs"}], "education": []},
            }) as fetched,
            mock.patch.object(jev_judge, "judge_batch", return_value=[IdentityJudgeResult(
                IdentityVerdict.from_payload({"verdict": "needs_review", "confidence": .5, "reason": "uncertain"}),
                IdentityUsage(), "", "fixture",
            )]) as judged,
            mock.patch.object(ReviewRelationships, "_judge", autospec=True, side_effect=relationships) as compared,
        ):
            self.assertEqual(run()["status"], "completed")
            for paid in (submitted, fetched, judged, compared):
                paid.assert_called_once()
                paid.reset_mock()
                paid.side_effect = AssertionError("unchanged store must not spend")
            second = run()
            self.assertEqual((second["status"], second["errors"]), ("completed", []))
            for paid in (submitted, fetched, judged, compared):
                paid.assert_not_called()
            seed_identity(self.db, parent_id="new", person_id="person:new", row_key="candidate:new",
                name="Riley Echo", machine_worth="yes", include_link=False)
            plan = estimate_enrichment(self.db).research
            self.assertEqual([row.parent_id for row in plan.pending], ["new"])


if __name__ == "__main__":
    unittest.main()
