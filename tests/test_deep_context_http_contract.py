"""Frozen HTTP contract for the Deep Context browser application.

These are characterization tests for the transport boundary.  The SQLite
rewrite may replace every implementation behind ``make_handler``; the existing
browser must continue to receive these routes, fields, status codes, content
types, and response shapes unchanged.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import tempfile
import threading
import urllib.parse
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
    RowKind,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.review_web import server as review_server


class _QueuedRetargets:
    def submit(self, request: review_server.GuidanceRequest) -> dict[str, str]:
        return {
            "slug": request.slug,
            "pub": request.pub.lower(),
            "queue_slug": request.queue_slug,
            "name": request.name,
            "guidance": request.guidance,
            "state": "queued",
            "detail": "",
            "submitted_at": request.submitted_at,
            "updated_at": request.submitted_at,
        }


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
        self.enrichment_path = self.root / "enrichment-manifest.json"

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
        (self.root / "index.json").write_text(
            json.dumps(
                {
                    "parents": {
                        self.SLUG: {
                            "parent_id": "parent-jordan-bravo",
                            "children": ["jordan-bravo-child"],
                        }
                    },
                    "slugs": {
                        "jordan-bravo-child": {"person_id": self.PERSON_ID}
                    },
                    "by_email": {},
                    "by_phone": {},
                }
            ),
            encoding="utf-8",
        )
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
        (self.root / "avatars" / avatar_name).write_bytes(
            b"\x89PNG\r\n\x1a\nsynthetic"
        )

        parent_id = "parent-jordan-bravo"
        fact_path = self.root / "facts" / f"{self.PERSON_ID}.jsonl"
        dossier_path = self.root / "parents" / f"{self.SLUG}.md"
        avatar_path = self.root / "avatars" / avatar_name
        self.db = Db(self.root / "deep-context.sqlite")
        self.db.project_parent(ParentRow(
            parent_id, f"parent-worth:{parent_id}", "Jordan Bravo", self.SLUG,
            "maybe", "Synthetic uncertainty",
        ))
        self.db.project_person(PersonRow(
            self.PERSON_ID, parent_id, "jordan-bravo-child", self.SLUG, "Jordan Bravo"
        ))
        self.db.project_candidate(LinkRow(
            self.PUB, parent_id, self.PUB, RowKind.PUB.value,
            f"https://www.linkedin.com/in/{self.PUB}", "Jordan Bravo",
            machine_action="verify", machine_confidence=0.5, paid_profile=1,
            judgment_payload_json=json.dumps({
                "linkedin": {
                    "linkedin_url": f"https://www.linkedin.com/in/{self.PUB}",
                    "full_name": "Jordan Bravo",
                    "headline": "Synthetic operator",
                    "has_profile": True,
                }
            }),
        ))
        self.db.replace_candidate_people(self.PUB, (
            CandidatePersonRow(self.PUB, self.PERSON_ID, parent_id),
        ))
        for artifact in (
            ArtifactRow(
                f"facts:{self.PERSON_ID}", ArtifactKind.FACTS.value, parent_id,
                str(fact_path.resolve()), hashlib.sha256(fact_path.read_bytes()).hexdigest(),
                ProjectionStatus.PROJECTED.value, person_id=self.PERSON_ID,
            ),
            ArtifactRow(
                f"dossier:{parent_id}", ArtifactKind.DOSSIER.value, parent_id,
                str(dossier_path.resolve()), hashlib.sha256(dossier_path.read_bytes()).hexdigest(),
                ProjectionStatus.PROJECTED.value,
            ),
            ArtifactRow(
                f"avatar:{self.PUB}", ArtifactKind.AVATAR.value, parent_id,
                str(avatar_path.resolve()), hashlib.sha256(avatar_path.read_bytes()).hexdigest(),
                ProjectionStatus.PROJECTED.value, candidate_key=self.PUB,
            ),
        ):
            self.db.project_artifact(artifact)
        self.db.project_fact(FactRow(
            self.PERSON_ID, parent_id, f"facts:{self.PERSON_ID}", self.PERSON_ID,
            "maybe", "Synthetic uncertainty", 0.6,
            facts_json=json.dumps({
                "canonical_name": "Jordan Bravo",
                "network_worth": {
                    "decision": "maybe", "reason": "Synthetic uncertainty",
                },
                "summary": "A synthetic contact used for HTTP contracts.",
            }),
        ))

        self.queue = _QueuedRetargets()
        handler = review_server.make_handler(
            self.review_path,
            self.verdicts_path,
            self.root / "parents",
            self.root / "dossiers",
            0.7,
            0.85,
            synthetic_path=self.synthetic_path,
            facts_dir=self.root / "facts",
            people_csv=self.root / "people.csv",
            manifest_path=self.manifest_path,
            enrichment_manifest_path=self.enrichment_path,
            profile_cache_dir=self.root / "cache",
            avatar_dir=self.root / "avatars",
            run_jobs=False,
            guided_retargets=self.queue,
            db=self.db,
        )
        self.server = review_server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        fields: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = urllib.parse.urlencode(fields or {}) if fields is not None else None
        request_headers = dict(headers or {})
        if fields is not None:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            response_body = response.read()
            return (
                response.status,
                response.getheader("Content-Type") or "",
                response_body,
                {key.lower(): value for key, value in response.getheaders()},
            )
        finally:
            connection.close()

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
            # A parent dossier with no resolved child pointer is validly empty;
            # the route still returns an HTML fragment rather than 404/JSON.
            f"/api/dossier?slug={self.SLUG}": None,
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
        self.assertEqual(
            set(payload), {"items", "enabled", "estimated_cost_usd", "feedback_alert"}
        )
        self.assertIs(payload["enabled"], True)
        self.assertEqual(payload["items"], [])

        status, payload = self.json_request(
            "GET", "/api/decision-rows?view=yes&offset=0&limit=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"view", "total", "offset", "rows"})
        self.assertEqual(payload["view"], "yes")
        self.assertIsInstance(payload["rows"], list)

        status, payload = self.json_request(
            "GET", "/api/decision-rows?view=no&offset=bad&limit=bad"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["offset"], 0)

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
                self.assertEqual((status, content_type, body),
                                 (expected_status, expected_type, expected_body))

        status, payload = self.json_request("GET", "/api/decision-rows?view=maybe")
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": "unknown view: maybe"})

    def test_avatar_contract_uses_local_bytes_and_private_cache(self) -> None:
        status, content_type, body, headers = self.request(
            "GET", f"/api/avatar?pub={self.PUB}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertEqual(headers["cache-control"], "private, max-age=86400")

    def test_sse_route_headers_and_initial_event(self) -> None:
        client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        client.settimeout(5)
        try:
            client.sendall(
                b"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            chunks: list[bytes] = []
            while b"data: " not in b"".join(chunks):
                chunks.append(client.recv(4096))
            response = b"".join(chunks)
        finally:
            client.close()
        self.assertIn(b"HTTP/1.0 200 OK", response)
        self.assertIn(b"Content-Type: text/event-stream", response)
        self.assertIn(b"Cache-Control: no-store", response)
        self.assertIn(b"retry: 2000", response)
        self.assertIn(b"data: ", response)

    def test_post_route_inventory_validation_and_local_origin_guard(self) -> None:
        status, content_type, body, _ = self.request(
            "POST", "/decide", {}, {"Origin": "https://example.test"}
        )
        self.assertEqual((status, content_type, body),
                         (403, "text/plain", b"cross-origin request rejected"))

        cases = (
            ("/missing", {}, 404, b"not found"),
            ("/decide", {}, 400, b"bad request"),
            ("/worth", {"worth": "maybe"}, 400,
             b"worth must be yes, no, or restore"),
            ("/complete", {}, 409, b"unknown review stage: "),
            ("/approve-enrichment", {}, 409,
             b"Enrichment preview is stale; refresh the preview before approving"),
            ("/retarget", {"guidance": ""}, 400,
             b"guidance must be 1-2000 characters"),
            ("/feedback", {"comment": "", "action": "general"}, 400,
             b"comment must be 1-4000 characters"),
            ("/feedback", {"comment": "hello", "action": "unknown"}, 400,
             b"unknown feedback action"),
        )
        for path, fields, expected_status, marker in cases:
            with self.subTest(path=path, fields=fields):
                status, content_type, body, _ = self.request("POST", path, fields)
                self.assertEqual(status, expected_status)
                self.assertTrue(content_type.startswith("text/plain"))
                self.assertEqual(body, marker)

    def test_complete_accepts_stage_and_returns_manifest_progress(self) -> None:
        status, payload = self.json_request(
            "POST", "/complete", {"stage": "worth"}
        )
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
        submitted = {"status": "submitted", "feedback_id": "feedback-synthetic"}
        with mock.patch.object(
            review_server, "submit_directory_feedback", return_value=submitted
        ):
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

    def test_retarget_accepts_guidance_pub_and_parent_slug(self) -> None:
        with mock.patch.object(
            review_server, "build_feedback_request", side_effect=SystemExit("disabled")
        ):
            status, payload = self.json_request(
                "POST",
                "/retarget",
                {
                    "guidance": (
                        "Use https://www.linkedin.com/in/jordan-bravo-correct instead"
                    ),
                    "pub": self.PUB,
                    "parent_slug": self.SLUG,
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"ok", "item", "estimated_cost_usd"})
        self.assertIs(payload["ok"], True)
        item = payload["item"]
        self.assertEqual(item["pub"], self.PUB)
        self.assertEqual(item["slug"], self.SLUG)
        self.assertEqual(item["state"], "queued")

    def test_decide_accepts_decision_new_url_parent_slug_and_note(self) -> None:
        with mock.patch.object(
            review_server, "build_feedback_request", side_effect=SystemExit("disabled")
        ):
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
        self.assertEqual(
            payload["new_url"], "https://www.linkedin.com/in/jordan-bravo-correct"
        )
        for key in ("counts", "progress", "resolved_pubs", "state_token"):
            self.assertIn(key, payload)

    def test_worth_accepts_worth_pub_parent_slug_and_note(self) -> None:
        with mock.patch.object(
            review_server, "build_feedback_request", side_effect=SystemExit("disabled")
        ):
            status, payload = self.json_request(
                "POST",
                "/worth",
                {
                    "pub": self.PUB,
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
            "action",
            "approved",
            "counts",
            "progress",
            "review_manifest",
            "next_stage",
            "state_token",
        ):
            self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
