"""Focused HTTP tests for the SQLite-only Deep Context review runtime."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    CandidatePersonRow,
    FactRow,
    LinkRow,
    ParentRow,
    PersonRow,
    ProjectionStatus,
    ResearchRow,
    ResearchStatus,
    RowKind,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_review
from packs.ingestion.primitives.deep_context.db.worth_views import worth_review
from packs.ingestion.primitives.deep_context.parallel_research.queue import (
    build_input,
    input_fingerprint,
)
from packs.ingestion.primitives.deep_context.research_reconcile.selection import (
    build_queue,
    select_research,
)
from packs.ingestion.primitives.deep_context.guided_retarget import GuidedRetargetWorker
from packs.ingestion.primitives.deep_context.identity_reconcile.guidance import GuidanceRequest
from packs.ingestion.primitives.deep_context.review_web import server as review_server
from packs.ingestion.primitives.deep_context.review_web import sqlite_adapter as review_adapter
from packs.ingestion.primitives.deep_context import enrichment_pipeline
from packs.ingestion.primitives.deep_context.review_web.sqlite_adapter import (
    SqliteReviewAdapter,
)
from deep_context_sqlite_test_helpers import query, replace_candidate_people
from http_handler_test_helpers import InProcessHttpClient


class DeepContextSqliteWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.review = self.root / "review.csv"
        self.review.write_text("legacy state must not be opened\n", encoding="utf-8")
        self._seed_parent(
            "worth-parent",
            "worth-person",
            "casey-delta",
            "Casey Delta",
            "maybe",
            "candidate:email:casey@example.com",
            RowKind.CANDIDATE_EMAIL.value,
            candidate_origin=1,
            raw_import=1,
        )
        self._seed_parent(
            "linkedin-parent",
            "linkedin-person",
            "jordan-bravo",
            "Jordan Bravo",
            "yes",
            "jordan-bravo",
            RowKind.PUB.value,
            paid_profile=1,
        )
        self.queue = GuidedRetargetWorker(
            self.db,
            runner=lambda _: {"new_url": "https://www.linkedin.com/in/jordan-bravo-correct"},
            use_llm=False,
        )
        handler = review_server.make_handler(
            confirm_threshold=0.7,
            run_jobs=True,
            guided_retargets=self.queue,
            db=self.db,
        )
        self.review.unlink()
        self.http = InProcessHttpClient(handler)

    def tearDown(self) -> None:
        worker = self.queue._thread
        if worker is not None:
            worker.join(timeout=5)
        self.tmp.cleanup()

    def _seed_parent(
        self,
        parent_id: str,
        person_id: str,
        slug: str,
        name: str,
        worth: str,
        candidate_key: str,
        kind: str,
        **flags: int,
    ) -> None:
        parent = ParentRow(
            parent_id,
            f"parent-worth:{parent_id}",
            name,
            slug,
            worth,
            "fixture",
        )
        dossier = self.root / f"{slug}.md"
        dossier.write_text(f"# {name}\n\n## Relationship\nSynthetic collaborator.\n", encoding="utf-8")
        fact_path = self.root / f"{person_id}.jsonl"
        fact_path.write_text("{}\n", encoding="utf-8")
        fact_artifact = ArtifactRow(
            f"facts:{person_id}",
            ArtifactKind.FACTS.value,
            parent_id,
            str(fact_path),
            hashlib.sha256(fact_path.read_bytes()).hexdigest(),
            ProjectionStatus.PROJECTED.value,
            person_id=person_id,
        )
        fact = FactRow(
            person_id,
            parent_id,
            fact_artifact.artifact_key,
            person_id,
            worth,
            "fixture",
            0.6,
            facts_json=json.dumps(
                {
                    "canonical_name": name,
                    "network_worth": {"decision": worth, "reason": "fixture"},
                }
            ),
        )
        candidate = LinkRow(
            candidate_key,
            parent_id,
            candidate_key,
            kind,
            f"https://www.linkedin.com/in/{candidate_key}" if kind == RowKind.PUB.value else None,
            name,
            machine_action="verify",
            machine_confidence=0.5,
            judgment_payload_json=json.dumps(
                {
                    "linkedin": {"full_name": name, "headline": "Synthetic operator", "has_profile": True},
                }
            ),
            **flags,
        )
        dossier_artifact = ArtifactRow(
            f"dossier:{parent_id}",
            ArtifactKind.DOSSIER.value,
            parent_id,
            str(dossier),
            hashlib.sha256(dossier.read_bytes()).hexdigest(),
            ProjectionStatus.PROJECTED.value,
            payload_json=json.dumps({
                "parent_id": parent_id,
                "name": name,
                "path": f"parents/{slug}.md",
                "children": [slug],
                "body": dossier.read_text(encoding="utf-8"),
            }),
        )
        self.db.project_rows(
            (
                parent,
                PersonRow(person_id, parent_id, slug, slug, name),
                fact_artifact,
                fact,
                candidate,
                dossier_artifact,
            )
        )
        replace_candidate_people(
            self.db,
            candidate_key,
            (CandidatePersonRow(candidate_key, person_id, parent_id),),
        )
        if kind == RowKind.PUB.value:
            avatar = self.root / f"{slug}.image"
            avatar.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
            self.db.project_rows(
                (
                    ArtifactRow(
                        f"avatar:{candidate_key}",
                        ArtifactKind.AVATAR.value,
                        parent_id,
                        str(avatar),
                        hashlib.sha256(avatar.read_bytes()).hexdigest(),
                        ProjectionStatus.PROJECTED.value,
                        candidate_key=candidate_key,
                        payload_json=json.dumps({
                            "content_type": "image/png",
                            "base64": base64.b64encode(avatar.read_bytes()).decode("ascii"),
                        }),
                    ),
                )
            )

    def request(self, method: str, path: str, fields: dict[str, str] | None = None) -> tuple[int, str, bytes]:
        status, content_type, body, _ = self.http.request(method, path, fields)
        return status, content_type, body

    def json_request(self, method: str, path: str, fields: dict[str, str] | None = None) -> tuple[int, dict]:
        status, content_type, body = self.request(method, path, fields)
        self.assertEqual(content_type, "application/json; charset=utf-8")
        return status, json.loads(body)

    def adapter(self) -> SqliteReviewAdapter:
        return SqliteReviewAdapter(self.db, 0.7)

    def wait_for_enrichment_job(self, status: str) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = query(
                self.db,
                "SELECT * FROM jobs WHERE kind='enrichment' ORDER BY started_at DESC",
            )
            if rows and rows[0]["status"] == status:
                return dict(rows[0])
            time.sleep(0.01)
        self.fail(f"enrichment job did not reach {status}")

    def cache_enrichment_result(self, adapter: SqliteReviewAdapter) -> None:
        state = adapter.snapshot()
        plan = select_research(
            self.db,
            processor="core2x",
            confirm_threshold=adapter.confirm_threshold,
            include_plausibly_absent=True,
            include_candidates=True,
            fingerprint=state["selection"],
        )
        self.assertEqual((len(plan.eligible), len(plan.pending)), (1, 1))
        row = dict(plan.queue[0])
        result_path = self.root / "research" / row["handle"] / "01_research_parallel.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps({
                "metadata": {
                    "input_fingerprint": input_fingerprint(row, row["handle"]),
                }
            }),
            encoding="utf-8",
        )
        payload = result_path.read_text(encoding="utf-8")
        fingerprint = input_fingerprint(row, row["handle"])
        self.db.project_rows((
            ArtifactRow(
                f"research:{row['handle']}", ArtifactKind.RESEARCH.value,
                "worth-parent", str(result_path), hashlib.sha256(payload.encode()).hexdigest(),
                ProjectionStatus.PROJECTED.value,
                candidate_key="candidate:email:casey@example.com",
                input_fingerprint=fingerprint,
                payload_json=payload,
            ),
            ResearchRow(
                row["handle"], "worth-parent", ResearchStatus.COMPLETE.value,
                candidate_key="candidate:email:casey@example.com",
                artifact_key=f"research:{row['handle']}",
                result_json=payload,
            ),
        ))

    def test_handler_requires_explicit_supported_db(self) -> None:
        with self.assertRaisesRegex(StoreError, "explicit supported"):
            review_server.make_handler()

    def test_gets_query_sqlite_after_legacy_files_disappear(self) -> None:
        status, payload = self.json_request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload),
            {
                "primitive",
                "ok",
                "manifest",
                "stage",
                "next_action",
                "state_token",
            },
        )
        for path, marker in (
            ("/api/worth-card", b"Casey Delta"),
            ("/api/linkedin-card", b"Jordan Bravo"),
            ("/api/person?slug=jordan-bravo", b"Synthetic collaborator"),
            ("/directory", b"data-directory"),
        ):
            with self.subTest(path=path):
                code, content_type, body = self.request("GET", path)
                self.assertEqual((code, content_type), (200, "text/html; charset=utf-8"))
                self.assertIn(marker.lower(), body.lower())

    def test_dossier_and_avatar_open_only_projected_paths(self) -> None:
        for path in (*self.root.glob("*.md"), *self.root.glob("*.image")):
            path.unlink()
        status, content_type, body = self.request("GET", "/api/dossier?slug=jordan-bravo")
        self.assertEqual((status, content_type), (200, "text/html; charset=utf-8"))
        self.assertIn(b"Synthetic collaborator", body)
        status, content_type, body = self.request("GET", "/api/avatar?pub=jordan-bravo")
        self.assertEqual((status, content_type), (200, "image/png"))
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_worth_and_identity_clicks_commit_domain_transactions(self) -> None:
        self.assertEqual(
            [row["key"] for row in worth_review(self.db, "queue")],
            ["parent-worth:worth-parent"],
        )
        status, payload = self.json_request(
            "POST",
            "/worth",
            {
                "pub": "parent-worth:worth-parent",
                "worth": "yes",
                "parent_slug": "casey-delta",
                "note": "Synthetic note",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["effective"], "yes")
        self.assertEqual(
            query(self.db, "SELECT human_worth FROM parents WHERE parent_id='worth-parent'")[0]["human_worth"], "yes"
        )
        self.assertEqual(worth_review(self.db, "queue"), [])
        self.assertEqual(
            [parent["parent_id"] for parent in linkedin_review(self.db, "queue")],
            ["linkedin-parent"],
        )
        status, payload = self.json_request(
            "POST",
            "/decide",
            {
                "pub": "jordan-bravo",
                "decision": "keep",
                "parent_slug": "jordan-bravo",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["action"], "verify")
        row = query(self.db, "SELECT decision_action, decision_approved FROM links WHERE row_key='jordan-bravo'")[0]
        self.assertEqual(tuple(row), ("verify", "yes"))
        self.assertEqual(linkedin_review(self.db, "queue"), [])

    def test_enrichment_preview_reuses_exact_paid_artifact_fingerprint(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        adapter = self.adapter()
        state = adapter.snapshot()
        self.cache_enrichment_result(adapter)

        preview = adapter.enrichment(state)
        self.assertEqual(preview["would_submit"], 0)
        self.assertEqual(preview["reused_completed"], 1)
        self.assertEqual(preview["estimated_usd"], 0.0)
        self.assertEqual((preview["status"], preview["state"]), ("not_started", "free_pending"))

    def test_workflow_http_snapshot_is_derived_once(self) -> None:
        with mock.patch.object(
            review_adapter,
            "workflow_state",
            wraps=review_adapter.workflow_state,
        ) as workflow_state:
            payload = self.adapter().workflow_status()
        self.assertEqual(payload["next_action"], "review_people")
        self.assertEqual(workflow_state.call_count, 1)

    def test_enrichment_with_only_cached_results_launches_for_free(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        self.cache_enrichment_result(self.adapter())
        with (
            mock.patch.object(enrichment_pipeline, "ReconcileDeepResearch") as reconcile,
            mock.patch.object(enrichment_pipeline, "AssembleSyntheticProfile") as assemble,
            mock.patch.object(enrichment_pipeline, "PrefetchProfiles") as prefetch,
        ):
            reconcile.return_value.run.return_value = {"status": "research_complete"}
            assemble.return_value.run.return_value = {"status": "completed"}
            prefetch.return_value.run.return_value = {"status": "completed"}
            status, _, _ = self.request("GET", "/?stage=enrich")
            self.assertEqual(status, 200)
            self.wait_for_enrichment_job("applied")
            status, _, _ = self.request("GET", "/?stage=enrich")
            self.assertEqual(status, 200)
        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual(reconcile.call_args.kwargs["budget"], 0.0)
        self.assertIs(reconcile.call_args.kwargs["approve"], True)

    def test_enrichment_job_preserves_existing_unconditional_stage_chain(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        with (
            mock.patch.object(enrichment_pipeline, "ReconcileDeepResearch") as reconcile,
            mock.patch.object(enrichment_pipeline, "AssembleSyntheticProfile") as assemble,
            mock.patch.object(enrichment_pipeline, "PrefetchProfiles") as prefetch,
        ):
            reconcile.return_value.run.return_value = {"status": "failed"}
            assemble.return_value.run.return_value = {"status": "completed"}
            prefetch.return_value.run.return_value = {"status": "completed"}
            status, payload = self.json_request("POST", "/approve-enrichment", {})
            self.assertEqual(status, 200)
            self.assertEqual(payload["enrichment"]["approval"]["status"], "approved")
            self.wait_for_enrichment_job("applied")
        assemble.return_value.run.assert_called_once_with()
        prefetch.return_value.run.assert_called_once_with()

    def test_running_enrichment_approval_is_idempotent(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        entered, release = threading.Event(), threading.Event()

        def reconcile_run():
            entered.set()
            self.assertTrue(release.wait(5))
            return {"status": "research_complete"}

        with (
            mock.patch.object(enrichment_pipeline, "ReconcileDeepResearch") as reconcile,
            mock.patch.object(enrichment_pipeline, "AssembleSyntheticProfile") as assemble,
            mock.patch.object(enrichment_pipeline, "PrefetchProfiles") as prefetch,
        ):
            reconcile.return_value.run.side_effect = reconcile_run
            assemble.return_value.run.return_value = {"status": "completed"}
            prefetch.return_value.run.return_value = {"status": "completed"}
            first_status, first = self.json_request("POST", "/approve-enrichment", {})
            self.assertEqual(first_status, 200)
            self.assertEqual(first["enrichment"]["approval"]["status"], "approved")
            self.assertTrue(entered.wait(5))
            second_status, second = self.json_request("POST", "/approve-enrichment", {})
            self.assertEqual(second_status, 200)
            self.assertEqual(second["enrichment"]["status"], "running")
            release.set()
            self.wait_for_enrichment_job("applied")
            self.assertEqual(reconcile.return_value.run.call_count, 1)

    def test_arbitrary_guidance_is_durably_queued_in_sqlite(self) -> None:
        with mock.patch.object(review_server, "build_feedback_request", side_effect=SystemExit("disabled")):
            status, payload = self.json_request(
                "POST",
                "/retarget",
                {
                    "pub": "jordan-bravo",
                    "parent_slug": "jordan-bravo",
                    "guidance": "Find the synthetic operator I met through Casey.",
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["item"]["state"], "queued")
        guidance = query(self.db, "SELECT guidance, state FROM guidance")
        self.assertEqual(guidance[0]["guidance"], "Find the synthetic operator I met through Casey.")
        self.assertIn(guidance[0]["state"], {"pending", "running", "applied"})
        self.assertEqual(query(self.db, "SELECT * FROM jobs WHERE kind='guided_retarget'"), [])

    def test_pasted_linkedin_applies_directly_without_research(self) -> None:
        worker = GuidedRetargetWorker(
            self.db,
            runner=lambda _: self.fail("direct pasted URL must not run paid research"),
        )
        item = worker.submit(
            GuidanceRequest(
                "jordan-bravo",
                "jordan-bravo",
                "Jordan Bravo",
                "Use https://www.linkedin.com/in/jordan-bravo-correct",
                person_ids=("linkedin-person",),
                queue_slug="jordan-bravo",
                submitted_at="2026-08-05T00:00:00Z",
            )
        )
        self.assertEqual(item["state"], "applied")
        row = query(
            self.db, "SELECT decision_action, replacement_public_identifier FROM links WHERE row_key='jordan-bravo'"
        )[0]
        self.assertEqual(tuple(row), ("retarget", "jordan-bravo-correct"))

    def test_guided_research_uses_canonical_dossier_and_reuse_home(self) -> None:
        queue_dir = self.root / "guided"
        research_dir = self.root / "deep-research"
        captured: dict[str, str] = {}

        def run_research(params):
            self.assertEqual(params.output_dir, research_dir)
            self.assertIs(params.db, self.db)
            row = params.rows[0]
            captured.update(row)
            self.db.project_rows((ResearchRow(
                row["handle"],
                "linkedin-parent",
                ResearchStatus.COMPLETE.value,
                candidate_key="jordan-bravo",
                result_json=json.dumps({
                    "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo-correct"},
                    "metadata": {"research_notes": "matched the dossier"},
                }),
            ),))
            return {"status": "completed"}

        worker = GuidedRetargetWorker(
            self.db,
            research_dir=research_dir,
        )
        request = GuidanceRequest(
            "jordan-bravo",
            "jordan-bravo",
            "Jordan Bravo",
            "Find the operator I met through Casey.",
            person_ids=("linkedin-person",),
            linkedin_url="https://www.linkedin.com/in/jordan-bravo",
            queue_slug="jordan-bravo",
            match_emails=("jordan@example.com",),
            match_phones=("+15550100",),
        )
        with mock.patch(
            "packs.ingestion.primitives.deep_context.identity_reconcile.guided.run_research",
            side_effect=run_research,
        ):
            result = worker.service.research(request)
        expected = build_queue(
            [
                {
                    "parent_slug": "jordan-bravo",
                    "person_ids": ["linkedin-person"],
                    "candidate_key": "jordan-bravo",
                    "name": "Jordan Bravo",
                    "linkedin": {
                        "linkedin_url": "https://www.linkedin.com/in/jordan-bravo"
                    },
                    "verdict": {"reason": ""},
                    "match_emails": ["jordan@example.com"],
                    "match_phones": ["+15550100"],
                }
            ],
            canonical_snapshot(self.db),
            guidance="Find the operator I met through Casey.",
        )[0]
        self.assertEqual(result["new_url"], "https://www.linkedin.com/in/jordan-bravo-correct")
        self.assertEqual(result["detail"], "matched the dossier")
        self.assertEqual(captured, expected)
        self.assertEqual(
            build_input(captured, captured["handle"]),
            build_input(expected, expected["handle"]),
        )
        self.assertFalse((queue_dir / "manifest.json").exists())

    def test_guided_provider_result_below_threshold_is_not_applied(self) -> None:
        worker = GuidedRetargetWorker(
            self.db,
            profile_cache_dir=self.root / "profile-cache",
            use_llm=False,
        )
        request = GuidanceRequest(
            "jordan-bravo",
            "jordan-bravo",
            "Jordan Bravo",
            "Find the operator I met through Casey.",
            person_ids=("linkedin-person",),
            queue_slug="jordan-bravo",
        )
        result = {
            "new_url": "https://www.linkedin.com/in/jordan-bravo-wrong",
            "research_profile": {
                "person": {"full_name": "Jordan Bravo", "confidence": 0.2},
                "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo-wrong"},
                "metadata": {"research_notes": "best guess only"},
            },
        }
        with mock.patch(
            "packs.ingestion.primitives.deep_context.profile_projection.hydrate_profiles",
            return_value={"ok": 0, "failed": 0},
        ):
            item = worker.service.apply_provider_result(
                "linkedin-parent", {"name": "Jordan Bravo"}, request, result
            )

        self.assertEqual(item["state"], "no_match")
        link = query(
            self.db,
            "SELECT decision_action, replacement_url, machine_action, machine_reject, "
            "machine_proposed_url FROM links WHERE row_key='jordan-bravo'",
        )[0]
        self.assertEqual((link["decision_action"], link["replacement_url"]), (None, None))
        self.assertEqual((link["machine_action"], link["machine_reject"]), ("retarget", "yes"))
        self.assertEqual(
            link["machine_proposed_url"],
            "https://www.linkedin.com/in/jordan-bravo-wrong",
        )

    def test_guided_provider_result_clearing_judge_is_machine_projected(self) -> None:
        worker = GuidedRetargetWorker(
            self.db,
            profile_cache_dir=self.root / "profile-cache",
        )
        request = GuidanceRequest(
            "jordan-bravo",
            "jordan-bravo",
            "Jordan Bravo",
            "Find the operator I met through Casey.",
            person_ids=("linkedin-person",),
            queue_slug="jordan-bravo",
        )
        result = {
            "new_url": "https://www.linkedin.com/in/jordan-bravo-correct",
            "research_profile": {
                "person": {"full_name": "Jordan Bravo", "confidence": 0.9},
                "positions": [{"title": "Founder", "company_name": "Bravo Robotics"}],
                "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo-correct"},
                "metadata": {"research_notes": "employer and relationship corroborated"},
            },
        }
        verdict = {
            "verdict": "confirmed",
            "confidence": 0.91,
            "reason": "employer and relationship corroborated",
        }
        with mock.patch(
            "packs.ingestion.primitives.deep_context.profile_projection.hydrate_profiles",
            return_value={"ok": 0, "failed": 0},
        ), mock.patch(
            "packs.ingestion.primitives.deep_context.identity_evidence.judge_batch",
            side_effect=lambda tasks, **_: [
                {"verdict": verdict, "usage": {}, "error": ""} for _ in tasks
            ],
        ):
            item = worker.service.apply_provider_result(
                "linkedin-parent", {"name": "Jordan Bravo"}, request, result
            )

        self.assertEqual(item["state"], "applied")
        link = query(
            self.db,
            "SELECT decision_action, machine_action, machine_reject, machine_confidence, "
            "machine_proposed_url FROM links WHERE row_key='jordan-bravo'",
        )[0]
        self.assertIsNone(link["decision_action"])
        self.assertEqual(link["machine_action"], "retarget")
        self.assertIsNone(link["machine_reject"])
        self.assertEqual(link["machine_confidence"], 0.91)
        self.assertEqual(
            link["machine_proposed_url"],
            "https://www.linkedin.com/in/jordan-bravo-correct",
        )

    def test_guided_provider_result_reuses_main_judge_fingerprint(self) -> None:
        worker = GuidedRetargetWorker(
            self.db,
            profile_cache_dir=self.root / "profile-cache",
        )
        request = GuidanceRequest(
            "jordan-bravo",
            "jordan-bravo",
            "Jordan Bravo",
            "Find the operator I met through Casey.",
            person_ids=("linkedin-person",),
            queue_slug="jordan-bravo",
        )
        result = {
            "new_url": "https://www.linkedin.com/in/jordan-bravo-correct",
            "research_profile": {
                "person": {"full_name": "Jordan Bravo", "confidence": 0.9},
                "social": {
                    "linkedin_url": "https://www.linkedin.com/in/jordan-bravo-correct"
                },
                "metadata": {"research_notes": "matched employer"},
            },
        }
        verdict = {
            "verdict": "confirmed",
            "confidence": 0.91,
            "reason": "matched employer",
        }
        with mock.patch(
            "packs.ingestion.primitives.deep_context.profile_projection.hydrate_profiles",
            return_value={"ok": 0, "failed": 0},
        ), mock.patch(
            "packs.ingestion.primitives.deep_context.identity_evidence.judge_batch",
            side_effect=lambda tasks, **_: [
                {"verdict": verdict, "usage": {}, "error": ""} for _ in tasks
            ],
        ) as judge:
            first = worker.service.apply_provider_result(
                "linkedin-parent", {"name": "Jordan Bravo"}, request, result
            )
            second = worker.service.apply_provider_result(
                "linkedin-parent", {"name": "Jordan Bravo"}, request, result
            )

        self.assertEqual((first["state"], second["state"]), ("applied", "applied"))
        self.assertEqual(judge.call_count, 1)
        fingerprint = query(
            self.db,
            "SELECT judgment_fingerprint FROM links WHERE row_key='jordan-bravo'",
        )[0]["judgment_fingerprint"]
        self.assertTrue(fingerprint)

    def test_pending_guided_job_resumes_from_sqlite(self) -> None:
        release = threading.Event()
        request = GuidanceRequest(
            "jordan-bravo",
            "jordan-bravo",
            "Jordan Bravo",
            "Find the synthetic operator from Casey.",
            person_ids=("linkedin-person",),
            queue_slug="jordan-bravo",
            submitted_at="2026-08-05T00:00:00Z",
        )
        accepted = {
            "verdict": {"verdict": "confirmed", "confidence": 0.9,
                        "reason": "corroborated"},
            "usage": {}, "error": "",
        }
        with mock.patch(
            "packs.ingestion.primitives.deep_context.profile_projection.hydrate_profiles",
            return_value={"ok": 0, "failed": 0},
        ), mock.patch(
            "packs.ingestion.primitives.deep_context.identity_evidence.judge_batch",
            side_effect=lambda tasks, **_: [accepted for _ in tasks],
        ):
            first = GuidedRetargetWorker(
                self.db,
                runner=lambda _: (
                    (release.wait(5) and {"new_url": "https://www.linkedin.com/in/jordan-bravo-correct"}) or {}
                ),
            )
            self.assertEqual(first.submit(request)["state"], "queued")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if query(self.db, "SELECT state FROM guidance")[0]["state"] == "running":
                    break
                time.sleep(0.01)
            resumed = GuidedRetargetWorker(
                self.db,
                runner=lambda _: {
                    "new_url": "https://www.linkedin.com/in/jordan-bravo-correct",
                    "detail": "resumed result",
                },
            )
            self.assertEqual(resumed.resume(), 1)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = query(self.db, "SELECT state FROM guidance")[0]["state"]
                if state == "applied":
                    break
                time.sleep(0.01)
            release.set()
            if first._thread:
                first._thread.join(timeout=2)
            if resumed._thread:
                resumed._thread.join(timeout=2)
        self.assertEqual(state, "applied")
        self.assertEqual(query(self.db, "SELECT * FROM jobs WHERE kind='guided_retarget'"), [])


if __name__ == "__main__":
    unittest.main()
