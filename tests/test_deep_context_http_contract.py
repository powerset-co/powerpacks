"""The review server's HTTP contract: its own routes, and the writes the Review page makes.

These exercise the real handler in-process over one synthetic parent. What each
/api/review/ read returns is pinned in test_deep_context_review_api.py; this file holds
the server's plain routes (status, events, dossier, health), the form writes (/worth,
/complete, /retarget, /feedback, /auth/login) with their refusals, and the searches-only
server that runs before a store exists.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
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
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.identity_invariants import (
    IdentityInvariantAudit,
)
from packs.ingestion.primitives.deep_context.db.identity_views import (
    approved_identities,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.review.guided_retarget import GuidedRetargetWorker
from packs.ingestion.primitives.deep_context.review import api as review_api
from packs.ingestion.primitives.deep_context.review import auth_login
from packs.ingestion.primitives.deep_context.review import cli as review_cli
from packs.ingestion.primitives.deep_context.review import server as review_server
from packs.ingestion.primitives.deep_context.review.models import FeedbackSubmission
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

        parent_id = "parent-jordan-bravo"
        fact_path = self.root / "facts" / f"{self.PERSON_ID}.jsonl"
        dossier_path = self.root / "parents" / f"{self.SLUG}.md"
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
                    source=WriterSource.RECONCILE.value,
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
        self,
        method: str,
        path: str,
        fields: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        status, content_type, body, _ = self.request(method, path, fields, headers)
        self.assertEqual(content_type, "application/json; charset=utf-8")
        return status, json.loads(body)

    def test_people_and_searches_pages_ride_the_review_server(self) -> None:
        status, content_type, body, _ = self.request("GET", "/people")
        self.assertEqual((status, content_type), (200, "text/html; charset=utf-8"))
        self.assertIn(b"data-people", body)
        self.assertIn(b"/app/assets/app.js", body)
        status, content_type, _, _ = self.request("GET", "/app/assets/app.css")
        self.assertEqual((status, content_type), (200, "text/css; charset=utf-8"))
        status, _, _, _ = self.request("GET", "/people/assets/people.js")
        self.assertEqual(status, 404)
        # The Searches page is the same React shell; its list comes from /searches/api/catalog.
        status, content_type, body, _ = self.request("GET", "/searches")
        self.assertEqual((status, content_type), (200, "text/html; charset=utf-8"))
        self.assertIn(b"/app/assets/app.js", body)
        status, content_type, body, _ = self.request("GET", "/searches/api/catalog")
        self.assertEqual((status, content_type), (200, "application/json; charset=utf-8"))
        self.assertIn(b'"searches"', body)
        status, _, _, _ = self.request("GET", "/share")
        self.assertEqual(status, 404)

    def test_json_get_shapes_and_query_fields(self) -> None:
        status, payload = self.json_request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload),
            {"primitive", "ok", "stage", "next_action", "state_token", "pending"},
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

    def test_sse_route_headers_and_initial_event(self) -> None:
        response = self.http.read_until("/api/events", b"data: ")
        self.assertIn(b"HTTP/1.0 200 OK", response)
        self.assertIn(b"Content-Type: text/event-stream", response)
        self.assertIn(b"Cache-Control: no-store", response)
        self.assertIn(b"retry: 2000", response)
        self.assertIn(b"data: ", response)

    def test_plain_get_routes_and_content_types(self) -> None:
        status, content_type, body, _ = self.request("GET", "/healthz")
        self.assertEqual((status, content_type, body), (200, "text/plain", b"ok"))

        # The dossier is an HTML fragment read from the store (its file on disk is gone);
        # skip=1 drops the name heading the card already shows.
        fragments = {
            f"/api/dossier?slug={self.SLUG}": (
                b"<h3>Jordan Bravo</h3>\n<h4>Relationship</h4>\n<p>Synthetic collaborator.</p>\n"
            ),
            f"/api/dossier?slug={self.SLUG}&skip=1": b"<h4>Relationship</h4>\n<p>Synthetic collaborator.</p>\n",
            "/api/dossier?slug=nobody&skip=1": b"",
        }
        for path, fragment in fragments.items():
            with self.subTest(path=path):
                status, content_type, body, headers = self.request("GET", path)
                self.assertEqual((status, content_type, body), (200, "text/html; charset=utf-8", fragment))
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertEqual(headers["x-content-type-options"], "nosniff")

        for path in ("/missing", "/api/person?slug=missing"):
            with self.subTest(path=path):
                status, content_type, body, _ = self.request("GET", path)
                self.assertEqual((status, content_type, body), (404, "text/plain", b"not found"))
        status, content_type, body, _ = self.request("POST", "/missing", {})
        self.assertEqual((status, content_type, body), (404, "text/plain", b"not found"))

    def test_form_writes_refuse_a_bad_form_and_write_nothing(self) -> None:
        feedback = {"comment": "hello", "action": "general"}
        guidance = {"guidance": "Find the synthetic operator"}
        cases = (
            ("/worth", {"worth": "maybe"}, 400, "worth must be yes, no, or restore"),
            ("/worth", {"worth": "yes"}, 404, "person not found"),
            ("/worth", {"worth": "yes", "parent_slug": "nobody"}, 404, "person not found"),
            (
                "/worth",
                {"worth": "yes", "parent_slug": self.SLUG, "pub": "parent-worth:someone-else"},
                404,
                "worth row not found",
            ),
            ("/complete", {}, 409, "unknown review stage: "),
            ("/complete", {"stage": "done"}, 409, "unknown review stage: done"),
            ("/retarget", {"guidance": ""}, 400, "guidance must be 1-2000 characters"),
            ("/retarget", {"guidance": "x" * 2001}, 400, "guidance must be 1-2000 characters"),
            ("/retarget", {**guidance, "pub": "nobody"}, 404, "review row not found"),
            ("/retarget", {**guidance, "parent_slug": "nobody"}, 404, "person not found"),
            (
                "/retarget",
                {**guidance, "pub": self.PUB, "parent_slug": "someone-else"},
                400,
                "stale or mismatched person card",
            ),
            ("/feedback", {"comment": "", "action": "general"}, 400, "comment must be 1-4000 characters"),
            ("/feedback", {"comment": "x" * 4001, "action": "general"}, 400, "comment must be 1-4000 characters"),
            ("/feedback", {"comment": "hello", "action": "unknown"}, 400, "unknown feedback action"),
            ("/feedback", {**feedback, "pub": "nobody"}, 404, "review row not found"),
            ("/feedback", {**feedback, "pub": "parent-worth:nobody"}, 404, "review row not found"),
            # The worth key is the parent's id, not its slug.
            ("/feedback", {**feedback, "pub": f"parent-worth:{self.SLUG}"}, 404, "review row not found"),
            ("/feedback", {**feedback, "parent_slug": "nobody"}, 404, "person not found"),
            (
                "/feedback",
                {**feedback, "pub": self.PUB, "parent_slug": "someone-else"},
                400,
                "stale or mismatched person card",
            ),
        )
        with mock.patch.object(review_api, "submit_directory_feedback", side_effect=AssertionError("refused first")):
            for path, fields, expected_status, error in cases:
                with self.subTest(path=path, fields=fields):
                    self.assertEqual(self.json_request("POST", path, fields), (expected_status, {"error": error}))
        self.assertEqual(
            [tuple(row) for row in self.db.query("SELECT human_worth FROM parents")], [(None,)]
        )
        self.assertEqual(self.db.query("SELECT * FROM guidance"), [])

    def test_worth_writes_the_call_and_answers_with_the_counts(self) -> None:
        key = "parent-worth:parent-jordan-bravo"
        notifier = mock.Mock()
        self.http = InProcessHttpClient(
            review_server.make_handler(
                confirm_threshold=0.7, guided_retargets=self.queue, db=self.db, agent_notifier=notifier
            )
        )
        answer = self.json_request(
            "POST", "/worth", {"pub": key, "worth": "yes", "parent_slug": self.SLUG, "note": "  Synthetic worth note "}
        )
        notifier.assert_called_once_with()
        self.assertEqual(
            answer,
            (
                200,
                {
                    "ok": True,
                    "pub": key,
                    "effective": "yes",
                    "progress": {"worth_pending": 0, "worth_yes": 1, "worth_no": 0, "linkedin_pending": 1},
                    "next_stage": "enrich",
                },
            ),
        )
        saved = self.db.query("SELECT human_worth, human_worth_note FROM parents")[0]
        self.assertEqual(tuple(saved), ("yes", "Synthetic worth note"))

        # Restore takes the call back: the person is pending again, by slug alone.
        status, restored = self.json_request("POST", "/worth", {"worth": "restore", "parent_slug": self.SLUG})
        self.assertEqual((status, restored["effective"], restored["next_stage"]), (200, "maybe", "worth"))
        self.assertEqual(restored["progress"]["worth_pending"], 1)
        self.assertIsNone(self.db.query("SELECT human_worth FROM parents")[0]["human_worth"])

    def test_complete_wakes_the_agent_and_answers_with_the_manifest(self) -> None:
        notifier = mock.Mock()
        handler = review_server.make_handler(
            confirm_threshold=0.7, guided_retargets=self.queue, db=self.db, agent_notifier=notifier
        )
        status, content_type, body, _ = InProcessHttpClient(handler).request("POST", "/complete", {"stage": "worth"})
        payload = json.loads(body)
        self.assertEqual((status, content_type), (200, "application/json; charset=utf-8"))
        self.assertEqual(set(payload), {"ok", "manifest", "progress"})
        self.assertIs(payload["ok"], True)
        self.assertEqual((payload["manifest"]["stage"], payload["manifest"]["status"]), ("worth", "completed"))
        self.assertEqual(payload["progress"]["worth_pending"], 1)
        notifier.assert_called_once_with()

    def test_retarget_hands_the_worker_the_posted_candidate(self) -> None:
        worker = mock.Mock()
        worker.submit.return_value.as_dict.return_value = {"state": "queued"}
        http = InProcessHttpClient(
            review_server.make_handler(confirm_threshold=0.7, guided_retargets=worker, db=self.db)
        )
        form = {"guidance": "  Find the synthetic operator  ", "pub": self.PUB, "parent_slug": self.SLUG}
        with mock.patch.object(review_api, "build_feedback_request", side_effect=SystemExit("disabled")):
            status, _, body, _ = http.request("POST", "/retarget", form)
        self.assertEqual(
            (status, json.loads(body)), (200, {"ok": True, "item": {"state": "queued"}, "estimated_cost_usd": 0.06})
        )
        request = worker.submit.call_args.args[0]
        self.assertEqual(
            (request.slug, request.row_key, request.name, request.guidance, request.person_ids, request.linkedin_url),
            (
                self.SLUG,
                self.PUB,
                "Jordan Bravo",
                "Find the synthetic operator",
                (self.PERSON_ID,),
                f"https://www.linkedin.com/in/{self.PUB}",
            ),
        )

    def test_retarget_is_refused_on_a_server_that_runs_no_jobs(self) -> None:
        http = InProcessHttpClient(review_server.make_handler(confirm_threshold=0.7, db=self.db))
        status, _, body, _ = http.request("POST", "/retarget", {"guidance": "Find them", "pub": self.PUB})
        self.assertEqual((status, json.loads(body)), (503, {"error": "in-app jobs are disabled on this server"}))
        status, _, body, _ = http.request("GET", "/api/retargets")
        self.assertIs(json.loads(body)["enabled"], False)
        self.assertEqual(self.db.query("SELECT * FROM guidance"), [])

    def test_feedback_answers_with_powersets_reply_and_a_502_unless_submitted(self) -> None:
        signed_out = FeedbackSubmission.from_payload({"status": "needs_auth", "error": "run `$powerset login`"})
        with mock.patch.object(review_api, "submit_directory_feedback", return_value=signed_out) as submit:
            # No `pub`: the feedback is about the parent and its first candidate.
            answer = self.json_request(
                "POST", "/feedback", {"comment": "Synthetic correction", "action": "general", "parent_slug": self.SLUG}
            )
        self.assertEqual(answer, (502, {"ok": False, "status": "needs_auth", "error": "run `$powerset login`"}))
        request = submit.call_args.args[0]
        self.assertEqual(
            (request.metadata["parent_slug"], request.metadata["public_identifier"]), (self.SLUG, self.PUB)
        )

    def test_auth_login_form_route_json_shape(self) -> None:
        for state in ("login_started", "already_running"):
            with mock.patch.object(auth_login, "start_auth_login", return_value=state):
                self.assertEqual(self.json_request("POST", "/auth/login", {}), (200, {"ok": True, "status": state}))

    def test_feedback_accepts_comment_action_pub_and_parent_slug(self) -> None:
        submitted = FeedbackSubmission.from_payload(
            {"status": "submitted", "feedback_id": "feedback-synthetic"}
        )
        with mock.patch.object(review_api, "submit_directory_feedback", return_value=submitted):
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
            review_api, "submit_directory_feedback", return_value=submitted
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
        with mock.patch.object(review_api, "build_feedback_request", side_effect=SystemExit("disabled")):
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
                    source=WriterSource.RECONCILE.value,
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
            review_api,
            "build_feedback_request",
            side_effect=SystemExit("disabled"),
        ):
            status, first = self.json_request(
                "POST",
                "/api/review/decide",
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
                "Keep this candidate until a better URL arrives",
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

class SearchesOnlyServerTests(unittest.TestCase):
    """Before a deep-context store exists the same server serves the searches alone."""

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), review_cli.searches_only_handler(root))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _get(self, path: str) -> tuple[int, str, bytes]:
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(self.base + path, timeout=5) as response:
                return response.status, response.headers.get("Location", ""), response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Location", ""), error.read()

    def test_searches_answer_and_people_rows_say_what_to_run(self) -> None:
        status, location, _ = self._get("/")
        self.assertEqual((status, location), (302, "/searches"))
        status, _, body = self._get("/searches")
        self.assertEqual(status, 200)
        self.assertIn(b"/app/assets/app.js", body)
        status, _, body = self._get("/searches/api/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"searches": []})
        status, _, body = self._get("/people")
        self.assertEqual(status, 200)
        self.assertIn(b"/app/assets/app.js", body)
        status, _, _ = self._get("/app/assets/app.js")
        self.assertEqual(status, 200)
        status, _, body = self._get("/api/people/rows")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "No people yet. Run bin/deep-context to build your network, "
                                                    "then bin/deep-context review people.")
        status, _, _ = self._get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(review_cli._url("127.0.0.1", 8765, "searches", "acme-role"),
                         "http://127.0.0.1:8765/searches/run?run_id=acme-role")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


if __name__ == "__main__":
    unittest.main()
