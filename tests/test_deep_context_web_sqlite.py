"""Focused HTTP tests for the SQLite-only Deep Context review runtime."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from parallel.types import TaskRunJsonOutput

from packs.ingestion.primitives.common.jsonio import read_json
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    IdentityMachineProjection,
    ParentRow,
    PersonIdentifierRow,
    PersonRow,
    ProjectionStatus,
    ResearchRow,
    ResearchStatus,
    RowKind,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db import _view_rows as view_rows
from packs.ingestion.primitives.deep_context.db.identity_views import (
    linkedin_queue,
    linkedin_queue_order,
    linkedin_queue_parent,
)
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.db.worth_views import worth_queue
from packs.ingestion.primitives.deep_context.db.view_models import EnrichmentQueueRow
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import (
    ResearchQueueRow,
    build_input,
    input_fingerprint,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
    ResearchRunResult,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.selection import (
    build_queue,
    select_research,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import (
    EnrichmentProgress,
    ResearchOutcome,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.primitives.deep_context.review.guided_retarget import GuidedRetargetWorker
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guidance import (
    ACTIVE_GUIDANCE_STATES,
    GuidanceRequest,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guided import (
    GuidanceOutcome,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityJudgeResult,
    IdentityUsage,
    IdentityVerdict,
)
from packs.ingestion.primitives.deep_context.review import cli as review_cli
from packs.ingestion.primitives.deep_context.review import server as review_server
from packs.ingestion.primitives.deep_context.review import enrichment as review_enrichment
from packs.ingestion.primitives.deep_context.review import sqlite_adapter as review_adapter
from packs.ingestion.primitives.deep_context.review.models import DecisionResult, GuidanceViewRow
from packs.ingestion.primitives.deep_context.enrich import enrichment_pipeline
from packs.ingestion.primitives.deep_context.manifests.enrichment_receipt import (
    EnrichmentReceipt,
)
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts
from packs.ingestion.primitives.deep_context.manifests.receipt_status import ReceiptStatus
from packs.ingestion.primitives.deep_context.enrich.profiles import projection
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponsesCaller,
)
from packs.ingestion.primitives.enrich.rapidapi_client import RapidApiClient
from packs.ingestion.primitives.deep_context.review.sqlite_adapter import (
    SqliteReviewAdapter,
)
from deep_context_sqlite_test_helpers import (
    query,
    replace_person_identifiers,
    seed_identity,
    stub_identity_judge,
)
from http_handler_test_helpers import InProcessHttpClient


# What the stubbed judge answers for the guided tests below: a rejection, which
# is what keeps a speculative research proposal out of the identity graph.
JUDGE_REJECTS = {
    "verdict": "wrong_person",
    "confidence": 0.91,
    "supporting_evidence": [],
    "contradicting_evidence": ["employer and city both disagree with the dossier"],
    "linkedin_plausibly_absent": False,
    "recommend_deep_research": False,
    "reason": "different person: employer and city contradict the dossier",
}


def guided_result(
    url: str,
    *,
    reason: str = "matched the dossier",
) -> ResearchResult:
    research = ResearchResult.from_output(TaskRunJsonOutput(
        type="json",
        content={
            "real_name": "Jordan Bravo",
            "work_experience": [{"title": "Founder", "company_name": "Bravo Robotics"}],
            "education": [],
            "location_city": "",
            "location_country": "",
            "linkedin_url": url,
            "github_url": "",
            "summary": "",
        },
        basis=[{"field": "linkedin_url", "reasoning": reason, "citations": []}],
    ))
    return research


def judge_result(verdict: str, confidence: float, reason: str) -> IdentityJudgeResult:
    return IdentityJudgeResult(
        verdict=IdentityVerdict.from_payload(
            {
                "verdict": verdict,
                "confidence": confidence,
                "reason": reason,
            }
        ),
        usage=IdentityUsage(),
        error="",
        fingerprint="",
    )


def refuse_paid_call(*_args, **_kwargs):
    raise AssertionError("paid call in unit test")


class DeepContextSqliteWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Every test here runs with the RapidAPI profile fetch and the OpenAI
        # caller refused at their definitions, so a missing stub fails loudly
        # instead of billing the repo-root .env.
        for patcher in (
            mock.patch.object(RapidApiClient, "get_profile", refuse_paid_call),
            mock.patch.object(OpenAIResponsesCaller, "__init__", refuse_paid_call),
        ):
            patcher.start()
            cls.addClassCleanup(patcher.stop)

    def finish_questions(self):
        from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.candidate_selection import RelationshipDecision, finish_reviews
        from packs.ingestion.primitives.deep_context.db.identity_views import pending_parent_ids
        from packs.ingestion.primitives.deep_context.db.identity_queries import links
        finish_reviews(self.db, tuple(RelationshipDecision.from_payload(parent, f"question:{parent}",
            {"candidates": [{"url": url, "verdict": "review", "reason": "Owner can identify colleague", "confidence": .5}
                for url in sorted({row.machine_proposed_url or row.linkedin_url for row in links(self.db, parent_id=parent)
                    if not row.decision_action and row.kind != "synthetic"} - {None, ""})]})
            for parent in pending_parent_ids(self.db)))
        return {"status": "completed"}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.enrichment_manifest = self.root / "deep-research" / "manifest.json"
        self._manifest_patches = (
            mock.patch.object(
                enrichment_pipeline,
                "ENRICH_MANIFEST",
                self.enrichment_manifest,
            ),
            # The manifest is write-only in review.enrichment now; the
            # pipeline (enrichment_pipeline) owns it for its own writes.

        )
        for patcher in self._manifest_patches:
            patcher.start()
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
        # Cleanups run after tearDown joins the worker thread, so these stubs
        # cover the whole guided run.
        for patcher in (
            mock.patch.object(projection, "hydrate_profiles", return_value={"ok": 0, "failed": 0}),
            stub_identity_judge(JUDGE_REJECTS),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.queue = GuidedRetargetWorker(
            self.db,
            runner=lambda _: guided_result("https://www.linkedin.com/in/jordan-bravo-correct"),
            research_dir=self.root / "deep-research",
            profile_cache_dir=self.root / "profile-cache",
        )
        handler = review_server.make_handler(
            confirm_threshold=0.7,
            run_jobs=True,
            guided_retargets=self.queue,
            db=self.db,
        )
        self.review.unlink()
        self.http = InProcessHttpClient(handler)

    def test_last_worth_card_has_no_intermediate_completion_screen(self) -> None:
        key = worth_queue(self.db)[0].key
        status, _, body = self.request("GET", f"/api/worth-card?exclude={key}")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

    def test_completed_worth_review_opens_enrichment_directly(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        status, _, body = self.request("GET", "/?stage=worth")
        self.assertEqual(status, 200)
        self.assertIn(b"data-stage='enrich'", body)
        self.assertNotIn(b"Decisions ready", body)

    def tearDown(self) -> None:
        worker = self.queue._thread
        if worker is not None:
            worker.join(timeout=5)
        for patcher in reversed(self._manifest_patches):
            patcher.stop()
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
        seed_identity(
            self.db,
            parent_id=parent_id,
            person_id=person_id,
            row_key=candidate_key,
            name=name,
            machine_worth=worth,
            display_slug=slug,
            kind=kind,
            linkedin_url=(f"https://www.linkedin.com/in/{candidate_key}" if kind == RowKind.PUB.value else None),
            link_updates={
                "machine_action": "verify",
                "machine_confidence": 0.5,
                **flags,
            },
            candidate_people=True,
            artifact_root=self.root,
            dossier_body=f"# {name}\n\n## Relationship\nSynthetic collaborator.\n",
            avatar_bytes=(b"\x89PNG\r\n\x1a\nsynthetic" if kind == RowKind.PUB.value else b""),
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
        expected = "completed" if status == "applied" else status
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.enrichment_manifest.exists():
                payload = read_json(self.enrichment_manifest, {})
                if payload.get("status") == expected:
                    return payload
            time.sleep(0.01)
        self.fail(f"enrichment job did not reach {status}")

    def wait_for_guidance_done(self) -> list[dict]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = query(self.db, "SELECT guidance, state, detail_json FROM guidance")
            if rows[0]["state"] not in ACTIVE_GUIDANCE_STATES:
                return rows
            time.sleep(0.01)
        self.fail("guided retarget worker did not finish")

    def cache_enrichment_result(self, adapter: SqliteReviewAdapter) -> None:
        state = adapter.snapshot()
        plan = select_research(
            self.db,
            processor="core2x",
            fingerprint=state.selection,
        )
        self.assertEqual((len(plan.eligible), len(plan.pending)), (1, 1))
        row = plan.pending[0]
        result_path = self.root / "research" / row.handle / "00_parallel_result.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {"type": "json", "content": {
                    "real_name": "Casey Delta", "work_experience": [], "education": [], "summary": "",
                }, "basis": []}
            ),
            encoding="utf-8",
        )
        payload = result_path.read_text(encoding="utf-8")
        fingerprint = input_fingerprint(row)
        self.db.project_rows(
            (
                ArtifactRow(
                    f"research:{row.handle}",
                    ArtifactKind.RESEARCH.value,
                    "worth-parent",
                    str(result_path),
                    hashlib.sha256(payload.encode()).hexdigest(),
                    ProjectionStatus.PROJECTED.value,
                    candidate_key="candidate:email:casey@example.com",
                    input_fingerprint=fingerprint,
                    payload_json=payload,
                ),
                ResearchRow(
                    row.handle,
                    "worth-parent",
                    ResearchStatus.COMPLETE.value,
                    candidate_key="candidate:email:casey@example.com",
                    artifact_key=f"research:{row.handle}",
                    result_json=payload,
                ),
            )
        )

    def test_handler_requires_explicit_supported_db(self) -> None:
        with self.assertRaisesRegex(TypeError, "required keyword-only argument: 'db'"):
            review_server.make_handler()

    def test_attached_only_enters_enrichment_without_research(self) -> None:
        status, workflow = self.json_request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(workflow["next_action"], "review_people")
        self.db.decide_worth("worth-parent", "no")
        status, workflow = self.json_request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(workflow["next_action"], "enrich")
        view = self.adapter().enrichment()
        self.assertEqual(view.would_submit, 0)
        self.assertEqual(view.state, "profile_prep_pending")

    def test_gets_query_sqlite_after_legacy_files_disappear(self) -> None:
        status, payload = self.json_request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload),
            {
                "primitive",
                "ok",
                "stage",
                "next_action",
                "state_token",
            },
        )
        for path, marker in (
            ("/api/worth-card", b"Casey Delta"),
            ("/api/linkedin-card", b"Jordan Bravo"),
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

    def test_collapsed_worth_row_loads_profile_only_when_expanded(self) -> None:
        status, _, body = self.request("GET", "/?stage=worth&view=yes&preview=1")
        self.assertEqual(status, 200)
        self.assertIn(b"Jordan Bravo", body)
        self.assertNotIn(b"Synthetic collaborator", body)
        self.assertNotIn(b"/api/avatar?", body)
        status, _, body = self.request("GET", "/api/worth-details?slug=jordan-bravo")
        self.assertEqual(status, 200)
        self.assertIn(b"Synthetic collaborator", body)
        self.assertIn(b"https://www.linkedin.com/in/jordan-bravo", body)

    def test_avatar_uses_profile_picture_only_when_present(self) -> None:
        parent = person_detail(self.db, "jordan-bravo")
        self.assertIsNotNone(parent)
        candidate = replace(parent.candidates[0], profile_pic_url="")
        card = review_server.render_worth_card(replace(parent, candidates=(candidate,)))
        self.assertNotIn("<img", card)
        candidate = replace(candidate, profile_pic_url="https://example.com/photo.png")
        card = review_server.render_worth_card(replace(parent, candidates=(candidate,)))
        self.assertIn("src='https://example.com/photo.png'", card)

    def test_worth_and_identity_clicks_commit_domain_transactions(self) -> None:
        self.assertEqual(
            [row.key for row in worth_queue(self.db)],
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
        self.assertEqual(worth_queue(self.db), [])
        self.assertEqual(
            [parent.parent_id for parent in linkedin_queue(self.db)],
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
        self.assertEqual(linkedin_queue(self.db), [])

    def test_enrichment_estimate_includes_judgments_created_by_new_research(self) -> None:
        from packs.ingestion.primitives.deep_context.shared.openai_responses import estimate_cost_usd
        from packs.ingestion.primitives.deep_context.db.identity_views import judge_candidates, review_questions_pending
        self.db.decide_worth("worth-parent", "yes")
        plan = select_research(self.db, processor="core2x")
        self.assertEqual(len(plan.pending), 1)
        existing = len(judge_candidates(self.db)) + review_questions_pending(self.db)
        from packs.search.primitives.llm_rerank_candidates.jev.client import INPUT_PRICE_PER_MILLION
        required = plan.estimated_usd + 2 * (existing + 1) * 2000 * INPUT_PRICE_PER_MILLION / 1_000_000 + estimate_cost_usd(2000 * (existing + 1),
            1500 * (existing + 1), "gpt-6.1-sol")
        self.assertGreaterEqual(self.adapter().enrichment().estimated_usd, required)

    def test_enrichment_preview_does_not_repeat_workflow_identity_queries(self) -> None:
        adapter = self.adapter()
        state = adapter.snapshot()
        with mock.patch.object(self.db, "query", wraps=self.db.query) as reads:
            adapter.enrichment(state)
        statements = [call.args[0] for call in reads.call_args_list]
        self.assertFalse(any("candidate_policy AS" in sql for sql in statements))
        self.assertFalse(any("research_link_rejected" in sql for sql in statements))

    def test_worth_page_reads_pending_queue_once(self) -> None:
        with mock.patch.object(review_server, "worth_queue", wraps=worth_queue) as reads:
            status, _, _ = self.request("GET", "/?stage=worth")
        self.assertEqual(status, 200)
        self.assertEqual(reads.call_count, 1)

    def test_unassembled_saved_research_requires_paid_question_estimate(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        self.db.decide_identity("jordan-bravo", "verify")
        self.db.project_rows((ResearchRow(
            "casey-delta", "worth-parent", ResearchStatus.NO_MATCH.value,
            candidate_key="candidate:email:casey@example.com",
            result_json=guided_result("").output.model_dump_json(exclude_none=True),
        ),))
        preview = self.adapter().enrichment()
        self.assertEqual(preview.would_submit, 0)
        self.assertGreater(preview.estimated_usd, 0)
        self.assertTrue(preview.approvable)
    def _seed_linkedin_queue(self) -> None:
        for slug, name in (("riley-stone", "Riley Stone"), ("avery-quinn", "Avery Quinn")):
            self._seed_parent(
                f"{slug}-parent", f"{slug}-person", slug, name, "yes", slug, RowKind.PUB.value, paid_profile=1,
            )

    def test_linkedin_queue_order_names_the_queue_without_its_cards(self) -> None:
        self._seed_linkedin_queue()
        queue = linkedin_queue(self.db)
        order = linkedin_queue_order(self.db)
        self.assertEqual(
            [(row.parent_id, row.slug) for row in order],
            [(parent.parent_id, parent.slug) for parent in queue],
        )
        self.assertEqual(len(order), 3)
        self.assertEqual(linkedin_queue_parent(self.db, order[1].parent_id), queue[1])

    def test_linkedin_click_loads_one_card_and_no_workflow_state(self) -> None:
        self._seed_linkedin_queue()
        with (
            mock.patch.object(view_rows, "_hydrate_parents", wraps=view_rows._hydrate_parents) as hydrate,
            mock.patch.object(review_adapter, "workflow_state", wraps=review_adapter.workflow_state) as workflow_state,
        ):
            status, payload = self.json_request(
                "POST",
                "/decide",
                {"pub": "jordan-bravo", "decision": "keep", "parent_slug": "jordan-bravo"},
            )
        self.assertEqual(status, 200)
        # Two parents are still pending; the response carries the next one's card.
        self.assertIn("Avery Quinn", payload["next"])
        self.assertNotIn("Riley Stone", payload["next"])
        self.assertEqual(
            payload["progress"],
            {"worth_pending": 1, "worth_yes": 3, "worth_no": 0, "linkedin_pending": 2},
        )
        self.assertEqual(max(len(call.args[1]) for call in hydrate.call_args_list), 1)
        self.assertEqual(workflow_state.call_count, 0)

    def test_reresearch_landing_mid_request_never_serves_a_blank_card(self) -> None:
        self._seed_linkedin_queue()
        # Avery Quinn is first in the queue and is being re-researched. The worker's
        # result lands right after this request read the queue's order.
        researching = GuidanceViewRow(
            slug="avery-quinn", row_key="avery-quinn", name="Avery Quinn", guidance="Synthetic guidance",
            state="researching", detail="", submitted_at="", updated_at="", new_url="", wire_fields=(),
        )
        landed = False
        read_order = review_server.linkedin_queue_order

        def order_then_land(db: Db) -> list:
            nonlocal landed
            order = read_order(db)
            db.decide_identity("avery-quinn", "verify")
            landed = True
            return order

        def retargets(_adapter: SqliteReviewAdapter) -> list[GuidanceViewRow]:
            return [] if landed else [researching]

        with (
            mock.patch.object(review_server, "linkedin_queue_order", order_then_land),
            mock.patch.object(SqliteReviewAdapter, "retargets", retargets),
        ):
            status, _, body = self.request("GET", "/api/linkedin-card")
        self.assertEqual(status, 200)
        self.assertIn(b"Jordan Bravo", body)

    def test_enrichment_preview_reuses_exact_paid_artifact_fingerprint(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        adapter = self.adapter()
        state = adapter.snapshot()
        self.cache_enrichment_result(adapter)

        preview = adapter.enrichment(state)
        self.assertEqual(preview.would_submit, 0)
        self.assertEqual(preview.reused_completed, 0)
        self.assertGreater(preview.estimated_usd, 0.0)
        # A fully-reused plan needs no spend, but its cached research still
        # needs the free local chain (synthetic assembly, profile prefetch).
        # The stage reads complete and the button is a $0 continue, not an
        # "Approve $0.00" prompt.
        self.assertEqual((preview.status, preview.state), ("not_started", "profile_prep_pending"))
        self.assertTrue(preview.approvable)

    def test_workflow_http_snapshot_is_derived_once(self) -> None:
        with mock.patch.object(
            review_adapter,
            "workflow_state",
            wraps=review_adapter.workflow_state,
        ) as workflow_state:
            payload = self.adapter().workflow_status()
        self.assertEqual(payload["next_action"], "review_people")
        self.assertEqual(workflow_state.call_count, 1)

    def test_review_startup_binds_without_calculating_workflow_queues(self) -> None:
        server = mock.Mock(server_address=("127.0.0.1", 8765))
        server.serve_forever.side_effect = KeyboardInterrupt
        with (
            mock.patch.object(review_cli, "CANONICAL_DB", self.db.db_path),
            mock.patch.object(review_cli, "load_env"),
            mock.patch.object(review_cli.urllib.request, "urlopen", side_effect=OSError),
            mock.patch.object(review_cli, "open_existing_db", return_value=self.db),
            mock.patch.object(review_server, "GuidedRetargetWorker"),
            mock.patch.object(review_cli, "ThreadingHTTPServer", return_value=server) as bind,
            mock.patch.object(review_cli, "_announce"),
            mock.patch.object(self.db, "query", wraps=self.db.query) as query,
        ):
            review_cli.main(["serve"])
        bind.assert_called_once()
        self.assertEqual(query.call_count, 1, "Startup must check existence, not calculate review or research queues")

    def test_empty_review_store_still_refuses_startup(self) -> None:
        empty = Db(self.root / "empty.sqlite")
        with self.assertRaisesRegex(ValueError, "database is empty"):
            review_server.make_handler(db=empty)

    def test_enrichment_get_never_launches_job(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        self.cache_enrichment_result(self.adapter())
        with mock.patch.object(
            enrichment_pipeline.EnrichmentPipeline,
            "start",
            side_effect=AssertionError("GET must not reach enrichment work"),
        ) as start:
            http = InProcessHttpClient(
                review_server.make_handler(
                    confirm_threshold=0.7,
                    run_jobs=True,
                    guided_retargets=self.queue,
                    db=self.db,
                )
            )
            status, _, _, _ = http.request("GET", "/?stage=enrich")

        self.assertEqual(status, 200)
        start.assert_not_called()

    def test_identity_selection_prevents_pending_synthetic_for_accepted_real_profile(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        self.cache_enrichment_result(self.adapter())
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE research SET result_json=json_set(result_json, '$.content.location_city', 'Oakland') "
                "WHERE parent_id='worth-parent'",
            )
        enrichment_pipeline.AssembleSyntheticProfile(db=self.db).run()
        self.assertTrue(self.db.query(
            "SELECT 1 FROM links WHERE parent_id='worth-parent' AND kind='synthetic'",
        ))

        def accept_real_profile():
            self.db.project_rows((IdentityMachineProjection(
                "candidate:email:casey@example.com",
                machine_action="retarget", machine_approved="yes",
                machine_judgment="confirmed", machine_confidence=.95,
                machine_proposed_url="https://www.linkedin.com/in/casey-delta",
                machine_proposed_public_identifier="casey-delta",
                source=WriterSource.DEEP_RESEARCH.value,
            ),))
            return {"status": "completed"}

        pipeline = enrichment_pipeline.EnrichmentPipeline(
            self.db, on_change=lambda: None, on_finish=lambda: None,
        )
        with (
            mock.patch.object(enrichment_pipeline, "ReconcileDeepResearch") as research,
            mock.patch.object(enrichment_pipeline, "PrefetchProfiles") as profiles,
            mock.patch.object(enrichment_pipeline, "judge_mapped_candidates") as judge,
            mock.patch.object(enrichment_pipeline, "ReviewRelationships") as selection,
        ):
            research.return_value.run.return_value = ResearchOutcome(
                ReceiptStatus.REUSED, ReceiptCounts(1, 1, 0, 0), None, 0.0, 0,
            )
            profiles.return_value.run.return_value.status = "completed"
            judge.return_value.judge_errors = 0
            selection.return_value.run.side_effect = accept_real_profile
            pipeline._run(0.0, lambda _: None)
        self.assertEqual(self.db.query(
            "SELECT row_key FROM links WHERE parent_id='worth-parent' AND kind='synthetic'",
        ), [])
        self.assertNotIn("worth-parent", {row.parent_id for row in linkedin_queue_order(self.db)})

    def test_cached_enrichment_launches_only_after_explicit_approval(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE links SET judgment_payload_json=?, judgment_fingerprint='fixture' "
                "WHERE row_key='jordan-bravo'",
                (json.dumps({"verdict": "needs_review", "confidence": 0.5}),),
            )
        self.cache_enrichment_result(self.adapter())
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE research SET status='no_match', result_json=? WHERE parent_id='worth-parent'",
                (json.dumps({"type": "json", "content": {
                    "real_name": "Casey Delta", "work_experience": [], "education": [],
                    "location_city": "Oakland", "linkedin_url": "",
                }, "basis": []}),),
            )
        with (
            mock.patch.object(enrichment_pipeline, "ReconcileDeepResearch") as reconcile,
            mock.patch.object(enrichment_pipeline, "PrefetchProfiles") as prefetch,
            mock.patch.object(enrichment_pipeline, "judge_mapped_candidates") as mapped_judge,
            mock.patch.object(enrichment_pipeline, "ReviewRelationships") as relationships,
        ):
            reconcile.return_value.run.return_value = ResearchOutcome(
                ReceiptStatus.REUSED,
                ReceiptCounts(1, 1, 0, 0),
                None,
                0.0,
                0,
            )
            prefetch.return_value.run.return_value.status = "completed"
            prefetch.return_value.run.return_value.note = None
            relationships.return_value.run.side_effect = self.finish_questions
            mapped_judge.return_value.judge_errors = 0
            judge_phases = []

            def judge_progress(*_args, heartbeat, **_kwargs):
                heartbeat(1, 1)
                judge_phases.append(read_json(self.enrichment_manifest)["progress"]["phase"])
                return mapped_judge.return_value

            mapped_judge.side_effect = judge_progress
            status, _, raw = self.request("GET", "/?stage=enrich")
            self.assertEqual(status, 200)
            page = raw.decode()
            # Cached research can still need a paid Sol identity decision.
            self.assertIn("Approve $", page)
            self.assertEqual(reconcile.call_count, 0)
            status, payload = self.json_request("POST", "/approve-enrichment", {})
            self.assertEqual(status, 200)
            # The POST response is built from a fresh read after the pipeline
            # thread is spawned, so it carries the then-current stage — not
            # the one-shot approval block. The accepted continuation is proven
            # by the receipt the job writes and the reconcile call below.
            self.assertIs(payload["ok"], True)
            self.assertIsInstance(payload["enrichment"], dict)
            self.wait_for_enrichment_job("applied")
            self.assertEqual(judge_phases, ["judging_retargets"])
            status, workflow = self.json_request("GET", "/api/status")
            self.assertEqual(workflow["stage"], "linkedin")
            self.assertEqual(workflow["next_action"], "review_linkedin")
            _, _, linkedin_page = self.request("GET", "/?stage=linkedin")
            self.assertNotIn(b"Enrich Contacts<small>", linkedin_page)
            status, _, raw = self.request("GET", "/?stage=enrich")
            self.assertEqual(status, 200)
            page = raw.decode()
            self.assertNotIn("Prepare profiles and judge LinkedIns", page)
        self.assertEqual(reconcile.call_count, 1)
        self.assertGreaterEqual(reconcile.call_args.kwargs["budget"], 0.0)
        self.assertIs(reconcile.call_args.kwargs["approve"], True)

    def test_failed_or_blocked_research_stops_the_enrichment_chain(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        with (
            mock.patch.object(enrichment_pipeline, "ReconcileDeepResearch") as reconcile,
            mock.patch.object(enrichment_pipeline, "AssembleSyntheticProfile") as assemble,
            mock.patch.object(enrichment_pipeline, "PrefetchProfiles") as prefetch,
        ):
            prefetch.return_value.run.return_value.status = "completed"
            prefetch.return_value.run.return_value.note = None
            for research_status in ("failed", "needs_approval"):
                with self.subTest(research_status=research_status):
                    reconcile.return_value.run.return_value = ResearchOutcome(
                        ReceiptStatus(research_status),
                        ReceiptCounts(1, 0, 1, 0),
                        None,
                        0.0,
                        0,
                    )
                    status, payload = self.json_request("POST", "/approve-enrichment", {})
                    self.assertEqual(status, 200)
                    # The POST response is built from a fresh read after the
                    # pipeline thread is spawned, so it carries the running
                    # view — not the one-shot approval payload. The stable
                    # proof that approval was accepted is the receipt the job
                    # writes, asserted below.
                    self.assertIs(payload["ok"], True)
                    self.assertIsInstance(payload["enrichment"], dict)
                    receipt = self.wait_for_enrichment_job("failed")
                    self.assertIn(
                        f"research stopped with status {research_status}",
                        receipt["error"],
                    )
        assemble.return_value.run.assert_not_called()
        prefetch.return_value.run.assert_not_called()

    def test_running_enrichment_approval_is_idempotent(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        entered, release = threading.Event(), threading.Event()

        def reconcile_run():
            entered.set()
            self.assertTrue(release.wait(5))
            return ResearchOutcome(
                ReceiptStatus.RAN,
                ReceiptCounts(1, 1, 0, 0),
                None,
                0.0,
                0,
            )

        with (
            mock.patch.object(enrichment_pipeline, "ReconcileDeepResearch") as reconcile,
            mock.patch.object(enrichment_pipeline, "AssembleSyntheticProfile"),
            mock.patch.object(enrichment_pipeline, "PrefetchProfiles") as prefetch,
            mock.patch.object(enrichment_pipeline, "judge_mapped_candidates") as mapped_judge,
            mock.patch.object(enrichment_pipeline, "ReviewRelationships") as relationships,
        ):
            reconcile.return_value.run.side_effect = reconcile_run
            prefetch.return_value.run.return_value.status = "completed"
            prefetch.return_value.run.return_value.note = None
            relationships.return_value.run.side_effect = self.finish_questions
            mapped_judge.return_value.judge_errors = 0
            first_status, first = self.json_request("POST", "/approve-enrichment", {})
            self.assertEqual(first_status, 200)
            # This reconcile blocks, so the first response is deterministically
            # the running view; the approval itself is one-shot state recorded
            # by the POST, not an "approval" key in the response body.
            self.assertEqual(first["enrichment"]["status"], "running")
            self.assertTrue(entered.wait(5))
            second_status, second = self.json_request("POST", "/approve-enrichment", {})
            self.assertEqual(second_status, 200)
            self.assertIsInstance(second["enrichment"], dict)
            self.assertEqual(second["enrichment"]["status"], "running")
            release.set()
            self.wait_for_enrichment_job("applied")
            self.assertEqual(reconcile.return_value.run.call_count, 1)

    def test_live_parallel_progress_is_exposed_by_web_api(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        entered, release = threading.Event(), threading.Event()

        def reconcile_run():
            reconcile.call_args.kwargs["on_progress"](
                EnrichmentProgress(
                    "research",
                    ReceiptCounts(1, 1, 0, 0),
                    phase_done=1,
                    phase_total=1,
                )
            )
            entered.set()
            self.assertTrue(release.wait(5))
            return ResearchOutcome(
                ReceiptStatus.RAN,
                ReceiptCounts(1, 1, 0, 0),
                None,
                0.05,
                0,
            )

        with (
            mock.patch.object(enrichment_pipeline, "ReconcileDeepResearch") as reconcile,
            mock.patch.object(enrichment_pipeline, "AssembleSyntheticProfile"),
            mock.patch.object(enrichment_pipeline, "PrefetchProfiles") as prefetch,
            mock.patch.object(enrichment_pipeline, "judge_mapped_candidates") as mapped_judge,
            mock.patch.object(enrichment_pipeline, "ReviewRelationships") as relationships,
        ):
            reconcile.return_value.run.side_effect = reconcile_run
            prefetch.return_value.run.return_value.status = "completed"
            prefetch.return_value.run.return_value.note = None
            relationships.return_value.run.side_effect = self.finish_questions
            mapped_judge.return_value.judge_errors = 0

            status, _ = self.json_request("POST", "/approve-enrichment", {})
            self.assertEqual(status, 200)
            self.assertTrue(entered.wait(5))

            status, live = self.json_request("GET", "/api/enrichment")
            self.assertEqual(status, 200)
            self.assertEqual(live["status"], "running")
            # Live counts are DB-projected truth: the fake research has not
            # projected anything yet, so the subject is still pending.
            self.assertEqual(live["counts"], {"total": 1, "completed": 0, "pending": 1})

            release.set()
            self.wait_for_enrichment_job("applied")
            # The SSE job payload (the pipeline's last receipt write) carried
            # the in-flight phase progress the bar animates from.
            final = self.json_request("GET", "/api/enrichment")[1]
            # The fake research projected nothing, so the plan honestly
            # re-offers the subject.
            self.assertEqual(final["status"], "needs_approval")

    def test_running_receipt_file_is_never_read_for_render_state(self) -> None:
        # The manifest is write-only observability: no receipt content —
        # stale, matching, or otherwise — can flip render state. Only the
        # local pipeline lock says "running".
        self.db.decide_worth("worth-parent", "yes")
        base = self.adapter().enrichment()
        EnrichmentReceipt(self.enrichment_manifest).write({
            "stage": "enrich",
            "status": "running",
            "request_fingerprint": base.request_fingerprint,
            "counts": {"total": 9, "completed": 9, "pending": 0, "failed": 0},
            "approved_budget_usd": base.estimated_usd,
        })

        stale = self.adapter().enrichment()
        live = self.adapter().enrichment(enrichment_running=True)

        self.assertEqual(stale.status, "needs_approval")
        self.assertEqual(stale.counts.total, 1)
        self.assertEqual((live.status, live.state), ("running", "running"))
        self.assertEqual(live.counts.total, 1)

    def test_changed_dossier_drifts_the_plan_not_the_render(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        plan = select_research(
            self.db,
            processor="core2x",
        )
        self.assertEqual(len(plan.pending), 1)
        matching = self.adapter().enrichment(enrichment_running=True)
        changed_queue = [replace(plan.pending[0], bio="A newly changed relationship dossier")]

        with mock.patch.object(
            review_enrichment.research_selection,
            "build_queue",
            return_value=changed_queue,
        ):
            changed = self.adapter().enrichment(enrichment_running=True)

        # Counts come from the plan, never a receipt: while running they are
        # the DB-projected truth (nothing projected yet -> still pending).
        self.assertEqual(matching.counts.total, 1)
        self.assertNotEqual(changed.request_fingerprint, plan.request_fingerprint)
        # While the local thread runs, it owns the render; the drifted plan
        # surfaces as needs_approval only once the lock releases.
        self.assertEqual(changed.status, "running")
        self.assertEqual(changed.counts.total, 1)

    def test_applied_retarget_does_not_invalidate_its_enrichment_receipt(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        before = select_research(
            self.db,
            processor="core2x",
        )
        self.assertEqual(len(before.eligible), 1)

        self.db.project_rows(
            (
                IdentityMachineProjection(
                    "candidate:email:casey@example.com",
                    machine_action="retarget",
                    machine_approved="auto",
                    machine_proposed_url="https://www.linkedin.com/in/casey-delta",
                    machine_proposed_public_identifier="casey-delta",
                    source=WriterSource.DEEP_RESEARCH.value,
                ),
            )
        )
        after = select_research(
            self.db,
            processor="core2x",
        )

        self.assertEqual(len(after.eligible), 0)
        self.assertNotEqual(after.request_fingerprint, before.request_fingerprint)

    def test_unlaunched_enrichment_approval_returns_json_view(self) -> None:
        self.db.decide_worth("worth-parent", "yes")
        with mock.patch.object(
            enrichment_pipeline.EnrichmentPipeline,
            "start",
            return_value=False,
        ) as start:
            http = InProcessHttpClient(
                review_server.make_handler(
                    confirm_threshold=0.7,
                    run_jobs=True,
                    guided_retargets=self.queue,
                    db=self.db,
                )
            )
            status, content_type, body, _ = http.request(
                "POST",
                "/approve-enrichment",
                {},
            )

        self.assertEqual((status, content_type), (200, "application/json; charset=utf-8"))
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertIsInstance(payload["enrichment"], dict)
        start.assert_called_once()

    def test_arbitrary_guidance_is_durably_queued_in_sqlite(self) -> None:
        # URL-less guidance only saves for message-derived people (the intake
        # gate); give the target a contact identifier like every real subject.
        replace_person_identifiers(
            self.db,
            "linkedin-person",
            (PersonIdentifierRow("linkedin-person", "email", "casey@example.com"),),
        )
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
        guidance = self.wait_for_guidance_done()
        self.assertEqual(guidance[0]["guidance"], "Find the synthetic operator I met through Casey.")
        self.assertEqual(guidance[0]["state"], "failed")
        self.assertEqual(json.loads(guidance[0]["detail_json"])["detail"], JUDGE_REJECTS["reason"])

    def test_urlless_guidance_without_contact_identifier_is_rejected_at_intake(self) -> None:
        """A research subject exists because a message channel discovered it, so
        URL-less guidance for a parent with no email/phone on file is refused at
        the save; the same parent still accepts a pasted-URL guidance directly."""
        status, content_type, body = self.request(
            "POST",
            "/retarget",
            {
                "pub": "jordan-bravo",
                "parent_slug": "jordan-bravo",
                "guidance": "Find the synthetic operator I met through Casey.",
            },
        )
        self.assertEqual((status, content_type), (409, "text/plain"))
        self.assertIn(b"no email or phone on file", body)
        self.assertIn(b"paste a LinkedIn URL instead", body)
        self.assertEqual(query(self.db, "SELECT * FROM guidance"), [])

        with mock.patch.object(review_server, "build_feedback_request", side_effect=SystemExit("disabled")):
            status, payload = self.json_request(
                "POST",
                "/retarget",
                {
                    "pub": "jordan-bravo",
                    "parent_slug": "jordan-bravo",
                    "guidance": "Use https://www.linkedin.com/in/jordan-bravo-correct",
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["item"]["state"], "applied")

    def test_pasted_linkedin_applies_directly_without_research(self) -> None:
        worker = GuidedRetargetWorker(
            self.db,
            runner=lambda _: self.fail("direct pasted URL must not run paid research"),
        )
        with mock.patch.object(
            projection,
            "hydrate_profiles",
            side_effect=AssertionError("a human URL decision must remain paid-free"),
        ):
            item = worker.submit(
                GuidanceRequest(
                    "jordan-bravo",
                    "jordan-bravo",
                    "Jordan Bravo",
                    "Use https://www.linkedin.com/in/jordan-bravo-correct",
                    person_ids=("linkedin-person",),
                    submitted_at="2026-08-05T00:00:00Z",
                )
            )
        self.assertEqual(item.state, "applied")
        row = query(
            self.db,
            "SELECT decision_action, replacement_public_identifier, decision_source "
            "FROM links WHERE row_key='jordan-bravo'",
        )[0]
        self.assertEqual(
            tuple(row),
            ("retarget", "jordan-bravo-correct", "user-guidance"),
        )

    def test_review_fix_records_human_retarget_without_paid_hydration(self) -> None:
        with mock.patch.object(
            projection,
            "hydrate_profiles",
            side_effect=AssertionError("a human URL decision must remain paid-free"),
        ):
            result = self.adapter().decide(
                "jordan-bravo",
                "fix",
                "https://www.linkedin.com/in/jordan-bravo-correct",
            )

        self.assertIsInstance(result, DecisionResult)
        self.assertEqual(result.action, "retarget")
        link = query(
            self.db,
            "SELECT decision_action, replacement_public_identifier FROM links WHERE row_key='jordan-bravo'",
        )[0]
        self.assertEqual(tuple(link), ("retarget", "jordan-bravo-correct"))

    def test_http_boundary_resolves_public_identifier_to_row_key_once(self) -> None:
        seed_identity(
            self.db,
            parent_id="opaque-parent",
            person_id="opaque-person",
            row_key="identity-row-42",
            public_identifier="public-jordan",
            name="Jordan Opaque",
            machine_worth="yes",
            display_slug="jordan-opaque",
            linkedin_url="https://www.linkedin.com/in/public-jordan",
        )

        status, payload = self.json_request(
            "POST",
            "/decide",
            {
                "pub": "public-jordan",
                "parent_slug": "jordan-opaque",
                "decision": "detach",
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["pub"], "identity-row-42")
        row = query(
            self.db,
            "SELECT decision_action FROM links WHERE row_key='identity-row-42'",
        )[0]
        self.assertEqual(row["decision_action"], "detach")

    def test_http_guided_research_uses_bare_person_row_key_without_candidate(self) -> None:
        seed_identity(
            self.db,
            parent_id="message-parent",
            person_id="message-person",
            row_key="unused",
            name="Morgan Echo",
            machine_worth="yes",
            display_slug="morgan-echo",
            include_link=False,
        )
        guided = mock.Mock()
        guided.submit.side_effect = lambda request: GuidanceOutcome(
            slug=request.slug,
            row_key=request.row_key,
            name=request.name,
            guidance=request.guidance,
            state="queued",
            detail="",
            submitted_at=request.submitted_at,
            updated_at=request.submitted_at,
        )
        http = InProcessHttpClient(
            review_server.make_handler(
                confirm_threshold=0.7,
                run_jobs=True,
                guided_retargets=guided,
                db=self.db,
            )
        )

        status, content_type, body, _ = http.request(
            "POST",
            "/retarget",
            {
                "pub": "",
                "parent_slug": "morgan-echo",
                "guidance": "Find the founder profile",
            },
        )

        self.assertEqual((status, content_type), (200, "application/json; charset=utf-8"))
        self.assertEqual(json.loads(body)["item"]["row_key"], "message-person")
        request = guided.submit.call_args.args[0]
        self.assertEqual(request.row_key, "message-person")
        self.assertEqual(request.linkedin_url, "")

    def test_guided_research_uses_canonical_dossier_and_reuse_home(self) -> None:
        queue_dir = self.root / "guided"
        research_dir = self.root / "deep-research"
        captured: ResearchQueueRow | None = None

        def run_research(params):
            nonlocal captured
            self.assertEqual(params.output_dir, research_dir)
            self.assertIs(params.db, self.db)
            row = params.rows[0]
            captured = row
            self.db.project_rows(
                (
                    ResearchRow(
                        row.handle,
                        "linkedin-parent",
                        ResearchStatus.COMPLETE.value,
                        candidate_key="jordan-bravo",
                        result_json=guided_result(
                            "https://www.linkedin.com/in/jordan-bravo-correct",
                        ).output.model_dump_json(exclude_none=True),
                    ),
                )
            )
            return ResearchRunResult(1, completed=1)

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
            match_emails=("jordan@example.com",),
            match_phones=("+15550100",),
        )
        with mock.patch(
            "packs.ingestion.primitives.deep_context.enrich.parallel_research.driver.run_research",
            side_effect=run_research,
        ):
            result = worker.service.research(request)
        expected = build_queue(
            [
                EnrichmentQueueRow(
                    "linkedin-parent",
                    "jordan-bravo",
                    "Jordan Bravo",
                    ("linkedin-person",),
                    "jordan-bravo",
                    True,
                    "https://www.linkedin.com/in/jordan-bravo",
                    "",
                    "",
                    ("jordan@example.com",),
                    ("+15550100",),
                    False,
                )
            ],
            self.db,
            guidance="Find the operator I met through Casey.",
        )[0]
        self.assertEqual(
            result.linkedin_url,
            "https://www.linkedin.com/in/jordan-bravo-correct",
        )
        self.assertEqual(result.reason, "deep research: matched the dossier")
        self.assertEqual(captured, expected)
        self.assertIsNotNone(captured)
        self.assertEqual(
            build_input(captured),
            build_input(expected),
        )
        self.assertFalse((queue_dir / "manifest.json").exists())

    def test_guided_provider_result_below_threshold_is_not_applied(self) -> None:
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
        )
        result = guided_result(
            "https://www.linkedin.com/in/jordan-bravo-wrong",
            reason="best guess only",
        )
        with (
            mock.patch(
                "packs.ingestion.primitives.deep_context.enrich.profiles.projection.hydrate_profiles",
                return_value={"ok": 0, "failed": 0},
            ),
            stub_identity_judge(JUDGE_REJECTS),
        ):
            item = worker.service.apply_provider_result(
                "linkedin-parent", person_detail(self.db, "linkedin-parent"), request, result
            )

        self.assertEqual(item.state, "no_match")
        self.assertEqual(item.detail, JUDGE_REJECTS["reason"])
        link = query(
            self.db,
            "SELECT decision_action, replacement_url, machine_action, machine_approved, "
            "machine_judgment, machine_proposed_url FROM links WHERE row_key='jordan-bravo'",
        )[0]
        self.assertEqual((link["decision_action"], link["replacement_url"]), (None, None))
        self.assertEqual(
            (link["machine_action"], link["machine_approved"], link["machine_judgment"]),
            ("retarget", None, "wrong_person"),
        )
        self.assertEqual(
            link["machine_proposed_url"],
            "https://www.linkedin.com/in/jordan-bravo-wrong",
        )

    def test_guided_result_surfaces_judge_reject_reason(self) -> None:
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
        )
        result = guided_result(
            "https://www.linkedin.com/in/jordan-bravo-wrong",
        )
        with (
            mock.patch(
                "packs.ingestion.primitives.deep_context.enrich.profiles.projection.hydrate_profiles",
                return_value={"ok": 0, "failed": 0},
            ),
            stub_identity_judge(JUDGE_REJECTS),
        ):
            item = worker.service.apply_provider_result(
                "linkedin-parent",
                person_detail(self.db, "linkedin-parent"),
                request,
                result,
            )

        expected = JUDGE_REJECTS["reason"]
        self.assertEqual(item.state, "no_match")
        self.assertEqual(item.detail, expected)
        stored = query(
            self.db,
            "SELECT machine_reason FROM links WHERE row_key='jordan-bravo'",
        )[0]
        self.assertEqual(stored["machine_reason"], expected)

    def test_missing_parent_after_guided_research_records_no_match(self) -> None:
        worker = GuidedRetargetWorker(self.db)
        parent = person_detail(self.db, "linkedin-parent")
        request = GuidanceRequest(
            "jordan-bravo",
            "jordan-bravo",
            "Jordan Bravo",
            "Find the operator I met through Casey.",
            person_ids=("linkedin-person",),
        )
        result = guided_result("https://www.linkedin.com/in/jordan-bravo-correct")
        with (
            mock.patch("packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guided.propose_retargets"),
            mock.patch(
                "packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guided.person_detail",
                return_value=None,
            ),
        ):
            item = worker.service.apply_provider_result(
                "linkedin-parent",
                parent,
                request,
                result,
            )

        self.assertEqual(item.state, "no_match")
        self.assertEqual(
            item.detail,
            "research result could not be attached to this person",
        )
        self.assertEqual(
            item.candidate_url,
            "https://www.linkedin.com/in/jordan-bravo-correct",
        )

    def test_missing_candidate_after_guided_research_records_no_match(self) -> None:
        worker = GuidedRetargetWorker(self.db)
        parent = person_detail(self.db, "linkedin-parent")
        request = GuidanceRequest(
            "jordan-bravo",
            "jordan-bravo",
            "Jordan Bravo",
            "Find the operator I met through Casey.",
            person_ids=("linkedin-person",),
        )
        result = guided_result("https://www.linkedin.com/in/jordan-bravo-correct")
        with (
            mock.patch("packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guided.propose_retargets"),
            mock.patch(
                "packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guided.person_detail",
                return_value=replace(parent, candidates=()),
            ),
        ):
            item = worker.service.apply_provider_result(
                "linkedin-parent",
                parent,
                request,
                result,
            )

        self.assertEqual(item.state, "no_match")
        self.assertEqual(
            item.detail,
            "research result could not be attached to this person",
        )
        self.assertEqual(
            item.candidate_url,
            "https://www.linkedin.com/in/jordan-bravo-correct",
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
        )
        result = guided_result(
            "https://www.linkedin.com/in/jordan-bravo-correct",
            reason="employer and relationship corroborated",
        )
        verdict = judge_result(
            "confirmed",
            0.91,
            "employer and relationship corroborated",
        )
        with (
            mock.patch(
                "packs.ingestion.primitives.deep_context.enrich.profiles.projection.hydrate_profiles",
                return_value={"ok": 0, "failed": 0},
            ),
            mock.patch(
                "packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge.judge_batch",
                side_effect=lambda tasks, **_: [verdict for _ in tasks],
            ),
        ):
            item = worker.service.apply_provider_result(
                "linkedin-parent", person_detail(self.db, "linkedin-parent"), request, result
            )

        self.assertEqual(item.state, "applied")
        link = query(
            self.db,
            "SELECT decision_action, machine_action, machine_approved, machine_confidence, "
            "machine_proposed_url FROM links WHERE row_key='jordan-bravo'",
        )[0]
        self.assertIsNone(link["decision_action"])
        self.assertEqual(link["machine_action"], "retarget")
        self.assertEqual(link["machine_approved"], "auto")
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
        )
        result = guided_result(
            "https://www.linkedin.com/in/jordan-bravo-correct",
            reason="matched employer",
        )
        verdict = judge_result("confirmed", 0.91, "matched employer")
        with (
            mock.patch(
                "packs.ingestion.primitives.deep_context.enrich.profiles.projection.hydrate_profiles",
                return_value={"ok": 0, "failed": 0},
            ),
            mock.patch(
                "packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge.judge_batch",
                side_effect=lambda tasks, **_: [verdict for _ in tasks],
            ) as judge,
        ):
            first = worker.service.apply_provider_result(
                "linkedin-parent", person_detail(self.db, "linkedin-parent"), request, result
            )
            second = worker.service.apply_provider_result(
                "linkedin-parent", person_detail(self.db, "linkedin-parent"), request, result
            )

        self.assertEqual((first.state, second.state), ("applied", "applied"))
        self.assertEqual(judge.call_count, 1)
        fingerprint = query(
            self.db,
            "SELECT judgment_fingerprint FROM links WHERE row_key='jordan-bravo'",
        )[0]["judgment_fingerprint"]
        self.assertTrue(fingerprint)

    def test_pending_guided_job_resumes_from_sqlite(self) -> None:
        # URL-less guidance only saves for message-derived people (the intake
        # gate); give the target a contact identifier like every real subject.
        replace_person_identifiers(
            self.db,
            "linkedin-person",
            (PersonIdentifierRow("linkedin-person", "email", "casey@example.com"),),
        )
        release = threading.Event()
        request = GuidanceRequest(
            "jordan-bravo",
            "jordan-bravo",
            "Jordan Bravo",
            "Find the synthetic operator from Casey.",
            person_ids=("linkedin-person",),
            submitted_at="2026-08-05T00:00:00Z",
        )
        accepted = judge_result("confirmed", 0.9, "corroborated")
        with (
            mock.patch(
                "packs.ingestion.primitives.deep_context.enrich.profiles.projection.hydrate_profiles",
                return_value={"ok": 0, "failed": 0},
            ),
            mock.patch(
                "packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge.judge_batch",
                side_effect=lambda tasks, **_: [accepted for _ in tasks],
            ),
        ):
            first = GuidedRetargetWorker(
                self.db,
                runner=lambda _: (
                    (release.wait(5) and guided_result("https://www.linkedin.com/in/jordan-bravo-correct")) or {}
                ),
            )
            self.assertEqual(first.submit(request).state, "queued")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if query(self.db, "SELECT state FROM guidance")[0]["state"] == "running":
                    break
                time.sleep(0.01)
            resumed = GuidedRetargetWorker(
                self.db,
                runner=lambda _: guided_result(
                    "https://www.linkedin.com/in/jordan-bravo-correct",
                    reason="resumed result",
                ),
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


class SynthesisPendingWebTests(unittest.TestCase):
    """A collected store whose synthesis never ran is not a finished review."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "deep-context.sqlite"
        self.db = Db(self.db_path)
        self.db.project_rows(
            (
                ParentRow("collected", "collected", "Casey Delta", "casey-delta"),
                PersonRow("collected-person", "collected", display_name="Casey Delta"),
                ArtifactRow(
                    "source_bundle:collected",
                    ArtifactKind.SOURCE_BUNDLE.value,
                    "collected",
                    "/raw/collected.json",
                    "sha-collected",
                    ProjectionStatus.PROJECTED.value,
                ),
            )
        )
        self.http = InProcessHttpClient(review_server.make_handler(db=self.db))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def page(self, path: str) -> str:
        status, _, body, _ = self.http.request("GET", path, None)
        self.assertEqual(status, 200)
        return body.decode()

    def test_status_names_synthesize(self) -> None:
        payload = json.loads(self.page("/api/status"))
        self.assertEqual((payload["stage"], payload["next_action"]), ("worth", "synthesize"))

        with mock.patch.object(review_cli, "CANONICAL_DB", self.db_path):
            status = review_cli.workflow_status()
        self.assertEqual(status["command"], "bin/deep-context dry")

    def test_wait_returns_at_once_on_synthesize(self) -> None:
        with (
            mock.patch.object(review_cli, "CANONICAL_DB", self.db_path),
            mock.patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            review_cli.main(["status", "--wait", "--timeout", "3"])
        payload = json.loads(out.getvalue())
        self.assertEqual((payload["next_action"], payload["status"], payload["waited_seconds"]), ("synthesize", "ok", 0))

    def test_every_stage_says_synthesis_has_not_run(self) -> None:
        for path in ("/?stage=worth", "/api/worth-card", "/?stage=enrich", "/?stage=linkedin", "/?stage=done"):
            with self.subTest(path=path):
                page = self.page(path)
                self.assertIn("Synthesis has not run", page)
                self.assertIn("bin/deep-context dry", page)
                for claim in ("Decisions ready", "Contacts enriched", "Review complete", "All set", "✓"):
                    self.assertNotIn(claim, page)


class DeepContextLinkedInPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Db(Path(self.tmp.name) / "deep-context.sqlite")
        for number, name in enumerate(("Casey Delta", "Jordan Bravo", "Morgan Echo")):
            seed_identity(self.db, parent_id=f"parent-{number}", person_id=f"person-{number}",
                row_key=f"candidate-{number}", name=name, machine_worth="yes",
                display_slug=f"contact-{number}", linkedin_url=f"https://www.linkedin.com/in/contact-{number}",
                link_updates={"paid_profile": 1})

    def test_fact_ranking_carries_keys_and_keeps_selected_fact_payload(self):
        from packs.ingestion.primitives.deep_context.db._view_sql import WORTH_CTE
        ranked = self.db.query(WORTH_CTE + "SELECT * FROM ranked_facts")
        self.assertEqual(set(ranked[0].keys()), {"subject_key", "parent_id", "worth_rank"})
        worth = self.db.query(WORTH_CTE + "SELECT parent_id, machine_facts_json FROM worth ORDER BY parent_id")
        facts = self.db.query("SELECT parent_id, facts_json FROM facts ORDER BY parent_id")
        self.assertEqual([tuple(row) for row in worth], [tuple(row) for row in facts])


if __name__ == "__main__":
    unittest.main()
