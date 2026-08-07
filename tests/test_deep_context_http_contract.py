"""Frozen HTTP contract for the Deep Context browser application.

These are characterization tests for the transport boundary.  The SQLite
rewrite may replace every implementation behind ``make_handler``; the existing
browser must continue to receive these routes, fields, status codes, content
types, and response shapes unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
import unittest
from dataclasses import replace
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
    ReviewSource,
    RowKind,
)
from packs.ingestion.primitives.deep_context.db.identity_invariants import (
    IdentityInvariantAudit,
)
from packs.ingestion.primitives.deep_context.db.identity_views import (
    approved_identities,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.review.guided_retarget import GuidedRetargetWorker
from packs.ingestion.primitives.deep_context.review import server as review_server
from packs.ingestion.primitives.deep_context.review.models import (
    EnrichmentApproval,
    FeedbackSubmission,
)
from packs.ingestion.primitives.deep_context.review.rendering import (
    GO_BACK_HTML,
    REVIEW_JS,
    linkedin_finished_body,
    render_decision_table,
    render_enrichment,
    render_linkedin_card,
    worth_finished_body,
)
from packs.ingestion.primitives.deep_context.review.sqlite_adapter import (
    SqliteReviewAdapter,
)
from deep_context_sqlite_test_helpers import replace_candidate_people
from http_handler_test_helpers import InProcessHttpClient


class DeepContextHttpContractTests(unittest.TestCase):
    """Exercise the real handler over localhost with synthetic local artifacts."""

    PUB = "jordan-bravo"
    SLUG = "jordan-bravo-p"
    PERSON_ID = "person-jordan-bravo"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name in (
            "avatars",
            "cache",
            "dossiers",
            "facts",
            "parents",
            "research",
            "review",
        ):
            (self.root / name).mkdir()

        self.review_path = self.root / "review.csv"
        self.verdicts_path = self.root / "verdicts.jsonl"
        self.synthetic_path = self.root / "synthetic-people.csv"
        self.manifest_path = self.root / "review" / "manifest.json"
        self.enrichment_path = self.root / "research" / "manifest.json"

        self.verdicts_path.write_text(
            json.dumps(
                {
                    "parent_slug": self.SLUG,
                    "name": "Jordan Bravo",
                    "person_ids": [self.PERSON_ID],
                    "candidate_key": self.PUB,
                    "linkedin": {
                        "linkedin_url": f"https://www.linkedin.com/in/{self.PUB}",
                        "full_name": "Jordan Bravo",
                        "has_profile": True,
                    },
                    "verdict": {"verdict": "needs_review", "confidence": 0.5},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.review_path.write_text("ignored legacy state\n", encoding="utf-8")
        (self.root / "facts" / f"{self.PERSON_ID}.jsonl").write_text(
            json.dumps(
                {
                    "facts": {
                        "canonical_name": "Jordan Bravo",
                        "network_worth": {
                            "decision": "maybe",
                            "reason": "Synthetic uncertainty",
                        },
                        "summary": "A synthetic contact used for HTTP contracts.",
                    },
                    "confidence": 0.6,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (self.root / "parents" / f"{self.SLUG}.md").write_text(
            "# Jordan Bravo\n\n## Relationship\nSynthetic collaborator.\n",
            encoding="utf-8",
        )
        (self.root / "dossiers" / f"{self.PERSON_ID}.md").write_text(
            "# Jordan Bravo\n\n## Context\nSynthetic dossier.\n",
            encoding="utf-8",
        )
        (self.root / "cache" / f"{self.PUB}.json").write_text(
            json.dumps(
                {
                    "normalized_profile": {
                        "full_name": "Jordan Bravo",
                        "headline": "Synthetic operator",
                        "location": "Example City",
                    }
                }
            ),
            encoding="utf-8",
        )
        avatar_name = hashlib.sha256(self.PUB.encode()).hexdigest()[:24] + ".image"
        (self.root / "avatars" / avatar_name).write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")

        parent_id = "parent-jordan-bravo"
        fact_path = self.root / "facts" / f"{self.PERSON_ID}.jsonl"
        dossier_path = self.root / "parents" / f"{self.SLUG}.md"
        avatar_path = self.root / "avatars" / avatar_name
        self.db = Db(self.root / "deep-context.sqlite")
        self.db.project_rows(
            (
                ParentRow(
                    parent_id,
                    f"parent-worth:{parent_id}",
                    "Jordan Bravo",
                    self.SLUG,
                    "maybe",
                    "Synthetic uncertainty",
                ),
                PersonRow(self.PERSON_ID, parent_id, "jordan-bravo-child", self.SLUG, "Jordan Bravo"),
                LinkRow(
                    self.PUB,
                    parent_id,
                    self.PUB,
                    RowKind.PUB.value,
                    f"https://www.linkedin.com/in/{self.PUB}",
                    "Jordan Bravo",
                    machine_action="verify",
                    machine_confidence=0.5,
                    paid_profile=1,
                    judgment_payload_json=json.dumps(
                        {
                            "linkedin": {
                                "linkedin_url": f"https://www.linkedin.com/in/{self.PUB}",
                                "full_name": "Jordan Bravo",
                                "headline": "Synthetic operator",
                                "has_profile": True,
                            }
                        }
                    ),
                ),
            )
        )
        replace_candidate_people(self.db, self.PUB, (CandidatePersonRow(self.PUB, self.PERSON_ID, parent_id),))
        for artifact in (
            ArtifactRow(
                f"facts:{self.PERSON_ID}",
                ArtifactKind.FACTS.value,
                parent_id,
                str(fact_path.resolve()),
                hashlib.sha256(fact_path.read_bytes()).hexdigest(),
                ProjectionStatus.PROJECTED.value,
                person_id=self.PERSON_ID,
            ),
            ArtifactRow(
                f"dossier:{parent_id}",
                ArtifactKind.DOSSIER.value,
                parent_id,
                str(dossier_path.resolve()),
                hashlib.sha256(dossier_path.read_bytes()).hexdigest(),
                ProjectionStatus.PROJECTED.value,
                payload_json=json.dumps({
                    "parent_id": parent_id,
                    "name": "Jordan Bravo",
                    "path": f"parents/{self.SLUG}.md",
                    "children": ["jordan-bravo-child"],
                    "body": dossier_path.read_text(encoding="utf-8"),
                }),
            ),
            ArtifactRow(
                f"avatar:{self.PUB}",
                ArtifactKind.AVATAR.value,
                parent_id,
                str(avatar_path.resolve()),
                hashlib.sha256(avatar_path.read_bytes()).hexdigest(),
                ProjectionStatus.PROJECTED.value,
                candidate_key=self.PUB,
                payload_json=json.dumps({
                    "content_type": "image/png",
                    "base64": base64.b64encode(avatar_path.read_bytes()).decode("ascii"),
                }),
            ),
        ):
            self.db.project_rows((artifact,))
        self.db.project_rows(
            (
                FactRow(
                    self.PERSON_ID,
                    parent_id,
                    f"facts:{self.PERSON_ID}",
                    self.PERSON_ID,
                    "maybe",
                    "Synthetic uncertainty",
                    0.6,
                    facts_json=json.dumps(
                        {
                            "canonical_name": "Jordan Bravo",
                            "network_worth": {
                                "decision": "maybe",
                                "reason": "Synthetic uncertainty",
                            },
                            "summary": "A synthetic contact used for HTTP contracts.",
                        }
                    ),
                ),
            )
        )

        self.queue = GuidedRetargetWorker(
            self.db,
            runner=lambda _: {"new_url": "https://www.linkedin.com/in/jordan-bravo-correct"},
        )
        handler = review_server.make_handler(
            confirm_threshold=0.7,
            run_jobs=False,
            guided_retargets=self.queue,
            db=self.db,
        )
        self.http = InProcessHttpClient(handler)
        dossier_path.unlink()
        avatar_path.unlink()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        fields: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str, bytes, dict[str, str]]:
        return self.http.request(method, path, fields, headers)

    def json_request(
        self, method: str, path: str, fields: dict[str, str] | None = None
    ) -> tuple[int, dict[str, object]]:
        status, content_type, body, _ = self.request(method, path, fields)
        self.assertEqual(content_type, "application/json; charset=utf-8")
        return status, json.loads(body)

    def test_get_route_inventory_and_content_types(self) -> None:
        html_routes: dict[str, bytes | None] = {
            "/": b"<!doctype html>",
            "/directory?q=Jordan&worth=maybe": b"data-directory",
            f"/api/dossier?slug={self.SLUG}": b"Synthetic collaborator",
            "/api/worth-card?debug=1&index=0&exclude=not-this-person": b"worth",
            "/api/linkedin-card?debug=1&index=0&exclude=not-this-person": b"LinkedIn",
            f"/api/person?slug={self.SLUG}": b"Jordan Bravo",
        }
        for path, marker in html_routes.items():
            with self.subTest(path=path):
                status, content_type, body, headers = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(content_type, "text/html; charset=utf-8")
                if marker is not None:
                    self.assertIn(marker.lower(), body.lower())
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertEqual(headers["x-content-type-options"], "nosniff")

        status, content_type, body, _ = self.request("GET", "/healthz")
        self.assertEqual((status, content_type, body), (200, "text/plain", b"ok"))

        for path, content_type in (
            ("/assets/reconcile-review.css", "text/css; charset=utf-8"),
            ("/assets/reconcile-review.js", "text/javascript; charset=utf-8"),
        ):
            with self.subTest(path=path):
                status, actual, body, headers = self.request("GET", path)
                self.assertEqual((status, actual), (200, content_type))
                self.assertTrue(body)
                self.assertEqual(headers["cache-control"], "no-cache")

    def test_rendered_markup_covers_every_javascript_dispatch_contract(self) -> None:
        # Keep these selectors pinned to reconcile_review.js:217, 418-420,
        # 543-608, 754-777, 922-975, 1024, 1093-1103, and 1297-1325.
        surfaces = {}
        for name, path in {
            "worth": "/?stage=worth",
            "linkedin": "/?stage=linkedin",
            "linkedin_card": "/api/linkedin-card",
            "enrich": "/?stage=enrich",
            "directory": f"/directory?person={self.SLUG}",
        }.items():
            status, _, body, _ = self.request("GET", path)
            self.assertEqual(status, 200)
            surfaces[name] = body.decode()

        contracts = {
            "data-tab": "worth",
            "data-decide": "linkedin_card",
            "data-linkedin-panel": "linkedin",
            "data-directory-tab": "directory",
            "data-directory-search": "directory",
            "data-menu-toggle": "linkedin_card",
            "data-person-menu": "linkedin_card",
            "data-feedback-general": "linkedin_card",
            "identity-scroll-shell": "linkedin_card",
            "data-scroll-cue": "linkedin_card",
            "data-worth-search": "worth",
            "data-search-list": "worth",
            "data-retarget-panel": "directory",
            "data-retarget-items": "directory",
            "data-feedback-alert": "directory",
            "data-open-guidance": "linkedin_card",
            "data-open-skip": "linkedin_card",
            "data-retarget-form": "linkedin_card",
            "enrich-state": "enrich",
            "data-slug": "linkedin_card",
        }
        for selector, surface in contracts.items():
            with self.subTest(selector=selector, surface=surface):
                self.assertIn(selector, surfaces[surface])

        parent = person_detail(self.db, self.SLUG)
        self.assertIsNotNone(parent)
        assert parent is not None
        candidate = replace(
            parent.candidates[0],
            experiences=("Role one", "Role two", "Role three", "Role four"),
        )
        rich_parent = replace(parent, candidates=(candidate,))
        rich_card = render_linkedin_card(
            rich_parent,
            rich_parent.candidates,
            failure_note="synthetic provider outage",
        )
        base_enrichment = SqliteReviewAdapter(self.db).enrichment()
        running = render_enrichment(
            replace(
                base_enrichment,
                status="running",
                state="running",
                counts=replace(
                    base_enrichment.counts,
                    total=4,
                    completed=1,
                    pending=3,
                ),
            )
        )
        approval = render_enrichment(
            replace(
                base_enrichment,
                status="needs_approval",
                state="needs_approval",
                estimated_usd=0.04,
            )
        )
        progress = SqliteReviewAdapter(self.db).snapshot().progress
        extra_markup = "".join(
            (
                rich_card,
                running,
                approval,
                render_decision_table(
                    [replace(parent, worth_row=replace(parent.worth_row, effective="yes"))],
                    "yes",
                ),
                worth_finished_body(progress, auto_continue=True),
                linkedin_finished_body(progress, linkedin_complete=True),
                GO_BACK_HTML,
            )
        )
        for path in (
            "/api/worth-card?debug=1&index=0",
            "/api/linkedin-card?debug=1&index=0",
        ):
            status, _, body, _ = self.request("GET", path)
            self.assertEqual(status, 200)
            extra_markup += body.decode()

        javascript = REVIEW_JS.read_text(encoding="utf-8")

        def kebab(value: str) -> str:
            return re.sub(r"(?<!^)(?=[A-Z])", "-", value).lower()

        inventory: set[str] = set()
        selector_calls = re.findall(
            r"(?:querySelector(?:All)?|closest|matches)\(\s*([\"'`])(.+?)\1",
            javascript,
            re.DOTALL,
        )
        for _, selector in selector_calls:
            inventory.update(re.findall(r"data-[a-z0-9-]+", selector))
            inventory.update(f".{name}" for name in re.findall(r"\.([A-Za-z_][\w-]*)", selector))
            inventory.update(f"#{name}" for name in re.findall(r"#([A-Za-z_][\w-]*)", selector))
        inventory.update(
            f"#{element_id}"
            for _, element_id in re.findall(
                r"getElementById\(\s*([\"'])(.+?)\1", javascript
            )
        )
        inventory.update(
            f"data-{kebab(field)}"
            for field in re.findall(r"\.dataset\.([A-Za-z_$][\w$]*)", javascript)
        )
        inventory.update(
            attribute
            for _, attribute in re.findall(
                r"(?:hasAttribute|getAttribute)\(\s*([\"'])(data-[a-z0-9-]+)\1",
                javascript,
            )
        )

        # These selectors are created by reconcile_review.js itself, not emitted
        # by the server-side renderer.
        javascript_created = {
            ".directory-item",
            ".feedback-login",
            ".feedback-popover",
            ".feedback-send",
            ".feedback-skip",
            "data-expanded",
            "data-guidance-button",
            "data-guidance-label",
            "data-guidance-placeholder",
            "data-loaded",
            "data-mode",
            "data-wired",
        }
        markup = "".join(surfaces.values()) + extra_markup

        def missing_tokens(rendered: str) -> set[str]:
            rendered_classes = {
                name
                for value in re.findall(r"class=['\"]([^'\"]*)", rendered)
                for name in value.split()
            }
            rendered_ids = set(re.findall(r"id=['\"]([^'\"]+)", rendered))
            return {
                token
                for token in inventory - javascript_created
                if (
                    (token.startswith(".") and token[1:] not in rendered_classes)
                    or (token.startswith("#") and token[1:] not in rendered_ids)
                    or (not token.startswith((".", "#")) and token not in rendered)
                )
            }

        self.assertEqual(missing_tokens(markup), set())
        self.assertIn("data-show-more", rich_card)
        self.assertIn("data-more-item", rich_card)
        self.assertIn("class='enrich-progress'", running)
        self.assertIn("class='enrich-progress-fill'", running)
        self.assertIn(
            "class='reresearch-failed'>Re-research failed: synthetic provider outage",
            rich_card,
        )
        self.assertIn("class='worth-search-count'", surfaces["worth"])
        self.assertIn("class='worth-search-count'", surfaces["directory"])

        # Scratch mutation proof: the inventory gate catches the exact D1-1
        # data-decide -> data-decision regression without changing the tree.
        renamed = markup.replace("data-decide", "data-decision")
        self.assertIn("data-decide", missing_tokens(renamed))
        renamed_approval = markup.replace(
            "data-approve-enrichment", "data-start-enrichment"
        )
        self.assertIn(
            "data-approve-enrichment",
            missing_tokens(renamed_approval),
        )

    def test_enrichment_view_is_not_gated_by_pending_worth_rows(self) -> None:
        status, content_type, body, _ = self.request("GET", "/?stage=enrich&preview=1")
        self.assertEqual((status, content_type), (200, "text/html; charset=utf-8"))
        self.assertIn(b"Enrich Contacts", body)
        self.assertNotIn(b"Review in progress", body)

    def test_json_get_shapes_and_query_fields(self) -> None:
        status, payload = self.json_request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload),
            {"primitive", "ok", "manifest", "stage", "next_action", "state_token"},
        )
        self.assertEqual(payload["primitive"], "reconcile_review_web")
        self.assertIs(payload["ok"], True)

        status, payload = self.json_request("GET", "/api/enrichment")
        self.assertEqual(status, 200)
        self.assertIn("status", payload)
        self.assertIn("counts", payload)

        status, payload = self.json_request("GET", "/api/retargets")
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"items", "enabled", "estimated_cost_usd", "feedback_alert"})
        self.assertIs(payload["enabled"], True)
        self.assertEqual(payload["items"], [])

    def test_get_not_found_and_field_errors(self) -> None:
        cases = (
            ("/missing", 404, "text/plain", b"not found"),
            ("/api/person?slug=missing", 404, "text/plain", b"not found"),
            ("/api/avatar?pub=missing", 404, "text/plain", b"not found"),
            ("/api/worth-card?pick=missing", 404, "text/plain; charset=utf-8", b"gone"),
        )
        for path, expected_status, expected_type, expected_body in cases:
            with self.subTest(path=path):
                status, content_type, body, _ = self.request("GET", path)
                self.assertEqual((status, content_type, body), (expected_status, expected_type, expected_body))

    def test_avatar_contract_uses_local_bytes_and_private_cache(self) -> None:
        status, content_type, body, headers = self.request("GET", f"/api/avatar?pub={self.PUB}")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertEqual(headers["cache-control"], "private, max-age=86400")

    def test_avatar_rejects_an_ambiguous_public_identifier_cleanly(self) -> None:
        self.db.project_rows(
            tuple(
                LinkRow(
                    f"ambiguous-row-{index}",
                    "parent-jordan-bravo",
                    "ambiguous-public-identifier",
                    RowKind.PUB.value,
                    f"https://www.linkedin.com/in/ambiguous-row-{index}",
                    f"Ambiguous {index}",
                )
                for index in (1, 2)
            )
        )

        status, content_type, body, _ = self.request(
            "GET", "/api/avatar?pub=ambiguous-public-identifier"
        )

        self.assertEqual(status, 400)
        self.assertEqual(content_type, "text/plain; charset=utf-8")
        self.assertEqual(
            body,
            b"ambiguous identity candidate: ambiguous-public-identifier",
        )

    def test_sse_route_headers_and_initial_event(self) -> None:
        response = self.http.read_until("/api/events", b"data: ")
        self.assertIn(b"HTTP/1.0 200 OK", response)
        self.assertIn(b"Content-Type: text/event-stream", response)
        self.assertIn(b"Cache-Control: no-store", response)
        self.assertIn(b"retry: 2000", response)
        self.assertIn(b"data: ", response)

    def test_post_route_inventory_validation_and_local_origin_guard(self) -> None:
        status, content_type, body, _ = self.request("POST", "/decide", {}, {"Origin": "https://example.test"})
        self.assertEqual((status, content_type, body), (403, "text/plain", b"cross-origin request rejected"))

        cases = (
            ("/missing", {}, 404, b"not found"),
            ("/decide", {}, 400, b"bad request"),
            ("/worth", {"worth": "maybe"}, 400, b"worth must be yes, no, or restore"),
            ("/complete", {}, 409, b"unknown review stage: "),
            ("/retarget", {"guidance": ""}, 400, b"guidance must be 1-2000 characters"),
            ("/feedback", {"comment": "", "action": "general"}, 400, b"comment must be 1-4000 characters"),
            ("/feedback", {"comment": "hello", "action": "unknown"}, 400, b"unknown feedback action"),
        )
        for path, fields, expected_status, marker in cases:
            with self.subTest(path=path, fields=fields):
                status, content_type, body, _ = self.request("POST", path, fields)
                self.assertEqual(status, expected_status)
                self.assertTrue(content_type.startswith("text/plain"))
                self.assertEqual(body, marker)

        status, payload = self.json_request("POST", "/approve-enrichment")
        self.assertEqual(status, 200)
        self.assertEqual(payload["enrichment"]["status"], "completed")

    def test_disabled_jobs_reject_computed_enrichment_approval(self) -> None:
        base = SqliteReviewAdapter(self.db).enrichment()
        enrichment = replace(
            base,
            status="needs_approval",
            approval=EnrichmentApproval(
                status="approved",
                approved_at="2026-08-06T00:00:00Z",
                approved_budget_usd=0.05,
                estimated_usd=0.05,
                would_submit=1,
                selection_fingerprint="selection-one",
                review_revision="",
            ),
        )
        with mock.patch.object(
            review_server.SqliteReviewAdapter,
            "approve_enrichment",
            return_value=enrichment,
        ):
            status, content_type, body, _ = self.request(
                "POST", "/approve-enrichment", {}
            )

        self.assertEqual(status, 409)
        self.assertTrue(content_type.startswith("text/plain"))
        self.assertEqual(body, b"enrichment job execution is disabled")
        self.assertEqual(self.db.query("SELECT count(*) FROM jobs")[0][0], 0)

    def test_complete_accepts_stage_and_returns_manifest_progress(self) -> None:
        status, payload = self.json_request("POST", "/complete", {"stage": "worth"})
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"ok", "manifest", "progress"})
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["manifest"]["stage"], "worth")
        self.assertEqual(payload["manifest"]["status"], "completed")

    def test_auth_login_form_route_json_shape(self) -> None:
        with mock.patch.object(review_server, "start_auth_login", return_value="login_started"):
            status, payload = self.json_request("POST", "/auth/login", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "status": "login_started"})

    def test_feedback_accepts_comment_action_pub_and_parent_slug(self) -> None:
        submitted = FeedbackSubmission.from_payload(
            {"status": "submitted", "feedback_id": "feedback-synthetic"}
        )
        with mock.patch.object(review_server, "submit_directory_feedback", return_value=submitted):
            status, payload = self.json_request(
                "POST",
                "/feedback",
                {
                    "comment": "Synthetic correction",
                    "action": "general",
                    "pub": self.PUB,
                    "parent_slug": self.SLUG,
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {"ok": True, "status": "submitted", "feedback_id": "feedback-synthetic"},
        )

    def test_feedback_accepts_parent_worth_key(self) -> None:
        submitted = FeedbackSubmission.from_payload(
            {"status": "submitted", "feedback_id": "feedback-worth"}
        )
        with mock.patch.object(
            review_server, "submit_directory_feedback", return_value=submitted
        ) as submit:
            status, payload = self.json_request(
                "POST",
                "/feedback",
                {
                    "comment": "Worth working on",
                    "action": "worth_yes",
                    "pub": "parent-worth:parent-jordan-bravo",
                    "parent_slug": self.SLUG,
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {"ok": True, "status": "submitted", "feedback_id": "feedback-worth"},
        )
        request = submit.call_args.args[0]
        self.assertEqual(request.metadata["parent_slug"], self.SLUG)
        self.assertNotIn("public_identifier", request.metadata)

    def test_retarget_accepts_guidance_pub_and_parent_slug(self) -> None:
        with mock.patch.object(review_server, "build_feedback_request", side_effect=SystemExit("disabled")):
            status, payload = self.json_request(
                "POST",
                "/retarget",
                {
                    "guidance": ("Use https://www.linkedin.com/in/jordan-bravo-correct instead"),
                    "pub": self.PUB,
                    "parent_slug": self.SLUG,
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"ok", "item", "estimated_cost_usd"})
        self.assertIs(payload["ok"], True)
        item = payload["item"]
        self.assertEqual(item["row_key"], self.PUB)
        self.assertEqual(item["slug"], self.SLUG)
        self.assertEqual(item["state"], "applied")

    def test_http_retarget_replaces_prior_human_winner_across_family(self) -> None:
        alternate = "aaa-jordan-bravo-alternate"
        replacement_url = "https://www.linkedin.com/in/jordan-bravo-correct"
        self.db.project_rows(
            (
                LinkRow(
                    alternate,
                    "parent-jordan-bravo",
                    alternate,
                    RowKind.PUB.value,
                    f"https://www.linkedin.com/in/{alternate}",
                    "Jordan Bravo Alternate",
                    machine_action="verify",
                    machine_confidence=0.5,
                    paid_profile=1,
                ),
            )
        )
        replace_candidate_people(
            self.db,
            alternate,
            (
                CandidatePersonRow(
                    alternate,
                    self.PERSON_ID,
                    "parent-jordan-bravo",
                ),
            ),
        )

        with mock.patch.object(
            review_server,
            "build_feedback_request",
            side_effect=SystemExit("disabled"),
        ):
            status, first = self.json_request(
                "POST",
                "/decide",
                {
                    "pub": self.PUB,
                    "decision": "keep",
                    "parent_slug": self.SLUG,
                    "note": "Keep this candidate until a better URL arrives",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(first["action"], "verify")

            status, second = self.json_request(
                "POST",
                "/retarget",
                {
                    "guidance": f"Use {replacement_url} instead",
                    "pub": alternate,
                    "parent_slug": self.SLUG,
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(second["item"]["state"], "applied")
        decisions = {
            row["row_key"]: row
            for row in self.db.query(
                "SELECT row_key, decision_action, decision_approved, "
                "decision_source, decision_note, replacement_url, "
                "replacement_public_identifier FROM links "
                "WHERE parent_id=? ORDER BY row_key",
                ("parent-jordan-bravo",),
            )
        }
        self.assertEqual(
            (
                decisions[alternate]["decision_action"],
                decisions[alternate]["decision_approved"],
                decisions[alternate]["decision_source"],
                decisions[alternate]["replacement_url"],
                decisions[alternate]["replacement_public_identifier"],
            ),
            (
                "retarget",
                "yes",
                ReviewSource.USER_GUIDANCE.value,
                replacement_url,
                "jordan-bravo-correct",
            ),
        )
        self.assertEqual(
            (
                decisions[self.PUB]["decision_action"],
                decisions[self.PUB]["decision_approved"],
                decisions[self.PUB]["decision_source"],
                decisions[self.PUB]["decision_note"],
            ),
            (
                "detach",
                "yes",
                ReviewSource.SIBLING_SETTLE.value,
                None,
            ),
        )
        approved_count = self.db.query(
            "SELECT count(*) FROM links WHERE parent_id=? "
            "AND decision_action IN ('verify', 'retarget') "
            "AND decision_approved='yes'",
            ("parent-jordan-bravo",),
        )[0][0]
        self.assertEqual(approved_count, 1)
        self.assertEqual(IdentityInvariantAudit(self.db).run().issues, ())
        exported = approved_identities(self.db)
        self.assertEqual(len(exported), 1)
        self.assertEqual(
            (
                exported[0].row_key,
                exported[0].person_id,
                exported[0].linkedin_url,
            ),
            (alternate, self.PERSON_ID, replacement_url),
        )

    def test_decide_accepts_decision_new_url_parent_slug_and_note(self) -> None:
        with mock.patch.object(review_server, "build_feedback_request", side_effect=SystemExit("disabled")):
            status, payload = self.json_request(
                "POST",
                "/decide",
                {
                    "pub": self.PUB,
                    "decision": "fix",
                    "new_url": "https://www.linkedin.com/in/jordan-bravo-correct",
                    "parent_slug": self.SLUG,
                    "note": "Synthetic correction",
                },
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["pub"], self.PUB)
        self.assertEqual(payload["action"], "retarget")
        self.assertEqual(payload["new_url"], "https://www.linkedin.com/in/jordan-bravo-correct")
        note = self.db.query(
            "SELECT decision_note FROM links WHERE row_key=?", (self.PUB,)
        )[0]["decision_note"]
        self.assertEqual(note, "Synthetic correction")
        for key in ("counts", "progress", "resolved_pubs", "state_token"):
            self.assertIn(key, payload)

    def test_worth_accepts_worth_pub_parent_slug_and_note(self) -> None:
        with mock.patch.object(review_server, "build_feedback_request", side_effect=SystemExit("disabled")):
            status, payload = self.json_request(
                "POST",
                "/worth",
                {
                    "pub": "parent-worth:parent-jordan-bravo",
                    "worth": "yes",
                    "parent_slug": self.SLUG,
                    "note": "Synthetic worth note",
                },
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["effective"], "yes")
        self.assertEqual(payload["source"], "user")
        for key in (
            "counts",
            "progress",
            "review_manifest",
            "next_stage",
            "state_token",
        ):
            self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
