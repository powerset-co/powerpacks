"""Focused HTTP tests for the SQLite-only Deep Context review runtime."""

from __future__ import annotations

import csv
import hashlib
import http.client
import json
import tempfile
import threading
import time
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
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.parallel_research.queue import build_input
from packs.ingestion.primitives.deep_context.identity_evidence import ResearchEvaluation
from packs.ingestion.primitives.deep_context.review_web.guided_retarget import (
    GuidanceRequest,
    GuidedRetargetWorker,
)
from packs.ingestion.primitives.deep_context.review_web import server as review_server
from deep_context_sqlite_test_helpers import query, replace_candidate_people


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
            out_dir=self.root / "guided",
            use_llm=False,
        )
        handler = review_server.make_handler(
            self.review,
            self.root / "verdicts.jsonl",
            self.root / "parents",
            self.root / "dossiers",
            0.7,
            0.85,
            synthetic_path=self.root / "synthetic-people.csv",
            facts_dir=self.root / "facts",
            people_csv=self.root / "people.csv",
            manifest_path=self.root / "review" / "manifest.json",
            enrichment_manifest_path=self.root / "enrichment" / "manifest.json",
            profile_cache_dir=self.root / "profile-cache",
            run_jobs=False,
            guided_retargets=self.queue,
            db=self.db,
        )
        self.review.unlink()
        self.server = review_server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
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
                    ),
                )
            )

    def request(self, method: str, path: str, fields: dict[str, str] | None = None) -> tuple[int, str, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = urllib.parse.urlencode(fields) if fields is not None else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if fields is not None else {}
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type") or "", response.read()
        finally:
            connection.close()

    def json_request(self, method: str, path: str, fields: dict[str, str] | None = None) -> tuple[int, dict]:
        status, content_type, body = self.request(method, path, fields)
        self.assertEqual(content_type, "application/json; charset=utf-8")
        return status, json.loads(body)

    def test_handler_requires_explicit_bootstrapped_db(self) -> None:
        with self.assertRaisesRegex(StoreError, "explicit bootstrapped"):
            review_server.make_handler(
                self.root / "missing.csv",
                self.root / "missing.jsonl",
                self.root,
                self.root,
                0.7,
                0.85,
            )

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
        status, content_type, body = self.request("GET", "/api/dossier?slug=jordan-bravo")
        self.assertEqual((status, content_type), (200, "text/html; charset=utf-8"))
        self.assertIn(b"Synthetic collaborator", body)
        status, content_type, body = self.request("GET", "/api/avatar?pub=jordan-bravo")
        self.assertEqual((status, content_type), (200, "image/png"))
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_worth_and_identity_clicks_commit_domain_transactions(self) -> None:
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
            out_dir=self.root / "guided",
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
            with params.input_csv.open(newline="", encoding="utf-8") as stream:
                row = next(csv.DictReader(stream))
            captured.update(row)
            result_dir = params.output_dir / row["handle"]
            result_dir.mkdir(parents=True)
            (result_dir / "01_research_parallel.json").write_text(
                json.dumps(
                    {
                        "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo-correct"},
                        "metadata": {"research_notes": "matched the dossier"},
                    }
                ),
                encoding="utf-8",
            )
            return {"status": "completed"}

        worker = GuidedRetargetWorker(
            self.db,
            out_dir=queue_dir,
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
            "packs.ingestion.primitives.deep_context.review_web.guided_retarget.run_research",
            side_effect=run_research,
        ):
            result = worker._research(request)
        self.assertEqual(result["new_url"], "https://www.linkedin.com/in/jordan-bravo-correct")
        self.assertEqual(result["detail"], "matched the dossier")
        self.assertIn("# Jordan Bravo", captured["bio"])
        self.assertIn('"current_linkedin": "https://www.linkedin.com/in/jordan-bravo"', captured["bio"])
        self.assertIn('"emails": ["jordan@example.com"]', captured["bio"])
        self.assertEqual(captured["known_info"], "")
        self.assertEqual(captured["retarget_hint"], "Find the operator I met through Casey.")
        self.assertEqual(
            build_input(captured, captured["handle"]),
            {
                "handle": "jordan-bravo",
                "dossier": (
                    "Name: Jordan Bravo\nRelationship dossier: " + captured["bio"]
                    + "\nEmail: jordan@example.com\nPhone: +15550100"
                ),
                "guidance": "Find the operator I met through Casey.",
            },
        )
        self.assertFalse((queue_dir / "manifest.json").exists())

    def test_guided_provider_result_below_threshold_is_not_applied(self) -> None:
        worker = GuidedRetargetWorker(
            self.db,
            facts_dir=self.root,
            raw_dir=self.root,
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
            "packs.ingestion.primitives.deep_context.review_web.guided_retarget.hydrate_profiles",
            return_value={"wanted": 1, "ok": 0, "failed": 0, "skipped_no_key": 1},
        ):
            item = worker._apply_provider_result(
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
            facts_dir=self.root,
            raw_dir=self.root,
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
            "packs.ingestion.primitives.deep_context.review_web.guided_retarget.hydrate_profiles",
            return_value={"wanted": 1, "ok": 0, "failed": 0, "skipped_no_key": 1},
        ), mock.patch(
            "packs.ingestion.primitives.deep_context.identity_evidence.judge_research_proposal",
            return_value=verdict,
        ):
            item = worker._apply_provider_result(
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
        accepted = ResearchEvaluation(
            True,
            {"verdict": "confirmed", "confidence": 0.9, "reason": "corroborated"},
            {
                "llm_reject": "",
                "llm_reject_confidence": "",
                "llm_reject_reason": "",
                "confidence": "0.900",
            },
        )
        with mock.patch(
            "packs.ingestion.primitives.deep_context.review_web.guided_retarget.hydrate_profiles",
            return_value={"wanted": 1, "ok": 0, "failed": 0, "skipped_no_key": 1},
        ), mock.patch(
            "packs.ingestion.primitives.deep_context.review_web.guided_retarget.evaluate_research_candidate",
            return_value=accepted,
        ):
            first = GuidedRetargetWorker(
                self.db,
                runner=lambda _: (
                    (release.wait(5) and {"new_url": "https://www.linkedin.com/in/jordan-bravo-correct"}) or {}
                ),
                out_dir=self.root / "guided",
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
                out_dir=self.root / "guided",
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
