"""The People upload route uses one job and exposes only safe progress."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from packs.ingestion.primitives.share.web.server import make_handler, share_routes
from test_share_web import ShareWebFixture


class UploadRoutesTests(ShareWebFixture):
    def setUp(self) -> None:
        super().setUp()
        self.upload_dir = self.root / "upload-powerset"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(share_routes(
            self.db, self.people_csv, upload_db=self.root / "local-search.duckdb", upload_dir=self.upload_dir)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _get(self) -> dict:
        with urllib.request.urlopen(self.base + "/api/people/upload") as response:
            return json.load(response)

    def _post(self, path: str = "/api/people/upload", *, origin: str | None = None) -> tuple[int, dict | str]:
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(self.base + path, data=b"{}",
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_repeat_post_joins_one_upload_and_reports_manifest(self) -> None:
        started = threading.Event()
        finish = threading.Event()
        calls = []
        upload_dir = self.upload_dir

        class FakeUploader:
            def __init__(self, **kwargs):
                calls.append(kwargs)

            def run(self):
                upload_dir.mkdir(exist_ok=True)
                (upload_dir / "manifest.json").write_text(json.dumps({
                    "status": "running", "stage": "people", "progress": {"total": 4, "uploaded": 1, "skipped": 2},
                    "error": "secret", "operator_id": "private",
                }))
                started.set()
                finish.wait(5)
                (upload_dir / "manifest.json").write_text(json.dumps({
                    "status": "completed", "progress": {"total": 4, "uploaded": 2, "skipped": 2},
                    "result": {"docs_upserted": {"people": 2}}, "operator_id": "private",
                }))

        with patch("packs.indexing.primitives.upload_powerset.upload_powerset.UploadPowerset", FakeUploader):
            self.upload_dir.mkdir()
            (self.upload_dir / "manifest.json").write_text(json.dumps({"status": "completed", "dry_run": True}))
            self.assertEqual(self._post()[0], 200)
            self.assertTrue(started.wait(5))
            self.assertEqual(self._post()[0], 200)
            status = self._get()
            self.assertEqual((status["status"], status["stage"], status["message"]),
                             ("running", "people", "Uploading people…"))
            self.assertEqual(status["progress"], {"total": 4, "uploaded": 1, "skipped": 2})
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["share_db"], self.db.db_path)
            self.assertEqual(calls[0]["people_csv"], self.people_csv)
            self.assertFalse(calls[0]["dry_run"])
            finish.set()
            for _ in range(100):
                if self._get()["status"] == "completed":
                    break
                threading.Event().wait(0.01)
            status = self._get()
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["progress"], {"total": 4, "uploaded": 2, "skipped": 2})
            self.assertEqual(status["result"], {"docs_upserted": {"people": 2}})

    def test_stale_running_manifest_is_interrupted_and_errors_are_sanitized(self) -> None:
        self.upload_dir.mkdir()
        manifest = self.upload_dir / "manifest.json"
        manifest.write_text(json.dumps({"status": "running", "progress": {"total": 5, "uploaded": 2}}))
        self.assertEqual(self._get()["status"], "interrupted")
        manifest.write_text(json.dumps({"status": "failed", "error": "DATABASE_URL=secret"}))
        self.assertNotIn("secret", json.dumps(self._get()))

    def test_dry_run_is_not_reported_as_an_upload(self) -> None:
        self.upload_dir.mkdir()
        (self.upload_dir / "manifest.json").write_text(json.dumps({
            "status": "completed", "dry_run": True, "progress": {"total": 3, "uploaded": 0, "skipped": 3},
        }))
        self.assertEqual(self._get()["status"], "ready")

    def test_new_upload_starts_at_zero_and_keeps_recovery_fields(self) -> None:
        self.upload_dir.mkdir()
        manifest = self.upload_dir / "manifest.json"
        previous = {"status": "completed", "dry_run": True,
                    "progress": {"total": 4, "uploaded": 4, "skipped": 0},
                    "target": {"namespaces": {"people": "aleph_people_v3"}},
                    "person_hashes": {"person-a": "hash-a"}, "owned_people": ["person-a"],
                    "pending_upserts": {"people": ["person-a"]}}
        manifest.write_text(json.dumps(previous))
        finish = threading.Event()

        class FakeUploader:
            def __init__(self, **kwargs):
                pass

            def run(self):
                finish.wait(5)

        with patch("packs.indexing.primitives.upload_powerset.upload_powerset.UploadPowerset", FakeUploader):
            try:
                code, started = self._post()
                self.assertEqual(code, 200)
                self.assertEqual(started["status"], "running")
                self.assertEqual(started["progress"], {"total": 0, "uploaded": 0, "skipped": 0})
                saved = json.loads(manifest.read_text())
                for key in ("target", "person_hashes", "owned_people", "pending_upserts"):
                    self.assertEqual(saved[key], previous[key])
            finally:
                finish.set()

    def test_failure_before_uploader_manifest_cannot_leave_old_success(self) -> None:
        self.upload_dir.mkdir()
        manifest = self.upload_dir / "manifest.json"
        previous = {"status": "completed", "dry_run": True,
                    "target": {"namespaces": {"people": "aleph_people_v3"}},
                    "person_hashes": {"person-a": "hash-a"}, "owned_people": ["person-a"],
                    "pending_upserts": {"people": ["person-a"]}}
        manifest.write_text(json.dumps(previous))
        with patch("packs.indexing.primitives.upload_powerset.upload_powerset.UploadPowerset",
                   side_effect=RuntimeError("DATABASE_URL=secret")):
            self.assertEqual(self._post()[0], 200)
            for _ in range(100):
                if self._get()["status"] == "failed":
                    break
                threading.Event().wait(0.01)
        status = self._get()
        self.assertEqual(status["status"], "failed")
        self.assertNotIn("secret", json.dumps(status))
        error_log = (self.upload_dir / "errors.log").read_text()
        self.assertIn("RuntimeError", error_log)
        self.assertIn("stage=planning", error_log)
        saved = json.loads(manifest.read_text())
        for key in ("target", "person_hashes", "owned_people", "pending_upserts"):
            self.assertEqual(saved[key], previous[key])

    def test_safe_preflight_error_is_actionable(self) -> None:
        self.upload_dir.mkdir()
        (self.upload_dir / "manifest.json").write_text(json.dumps({
            "status": "failed", "error": "Upload requires the powerset_v2 PostgreSQL login",
        }))
        self.assertEqual(self._get()["error"], "Upload requires the powerset_v2 PostgreSQL login")

    def test_progress_includes_human_stage_and_unmatched_count(self) -> None:
        self.upload_dir.mkdir()
        (self.upload_dir / "manifest.json").write_text(json.dumps({
            "status": "running", "stage": "people", "plan": {"skipped_no_linkedin": 2, "companies_skipped_no_row": 1},
        }))
        status = self._get()
        self.assertEqual(status["status"], "interrupted")
        self.assertEqual(status["skipped_no_linkedin"], 2)
        self.assertEqual(status["companies_skipped_no_row"], 1)

    def test_progress_forwards_only_numeric_document_counts(self) -> None:
        self.upload_dir.mkdir()
        (self.upload_dir / "manifest.json").write_text(json.dumps({
            "status": "completed", "progress": {
                "total": 2, "uploaded": 2, "skipped": 0,
                "namespaces": {"people": {"upserted": 2, "patched": 0, "secret": "hidden"},
                               "private": {"upserted": 99}},
            },
        }))
        status = self._get()
        self.assertEqual(status["progress"]["namespaces"], {"people": {"upserted": 2, "patched": 0}})

    def test_upload_rejects_remote_origin(self) -> None:
        status, _ = self._post(origin="https://elsewhere.example")
        self.assertEqual(status, 403)

    def test_check_runs_dry_run_and_only_confirm_applies(self) -> None:
        calls = []
        upload_dir = self.upload_dir

        class FakeUploader:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.dry_run = kwargs["dry_run"]

            def run(self):
                upload_dir.mkdir(exist_ok=True)
                (upload_dir / "manifest.json").write_text(json.dumps({
                    "status": "completed", "dry_run": self.dry_run,
                    "progress": {"total": 3, "uploaded": 0, "skipped": 0},
                }))

        with patch("packs.indexing.primitives.upload_powerset.upload_powerset.UploadPowerset", FakeUploader):
            self.assertEqual(self._post()[0], 409)
            self.assertEqual(self._post("/api/people/upload/check")[0], 200)
            for _ in range(100):
                if self._get()["status"] == "ready":
                    break
                threading.Event().wait(0.01)
            self.assertEqual(self._get()["status"], "ready")
            self.assertEqual([call["dry_run"] for call in calls], [True])
            self.assertEqual(self._post()[0], 200)
            for _ in range(100):
                if self._get()["status"] == "completed":
                    break
                threading.Event().wait(0.01)
            self.assertEqual([call["dry_run"] for call in calls], [True, False])

    def test_closing_after_check_does_not_apply_and_saved_uploads_count(self) -> None:
        self.upload_dir.mkdir()
        (self.upload_dir / "manifest.json").write_text(json.dumps({
            "status": "completed", "dry_run": True,
            "person_hashes": {"person-a": "hash-a", "person-c": "hash-c"},
            "progress": {"total": 3, "uploaded": 0, "skipped": 0},
        }))
        status = self._get()
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["previously_uploaded"], 1)

    def test_check_joins_active_apply(self) -> None:
        self.upload_dir.mkdir()
        (self.upload_dir / "manifest.json").write_text(json.dumps({"status": "completed", "dry_run": True}))
        started = threading.Event()
        finish = threading.Event()
        calls = []

        class FakeUploader:
            def __init__(self, **kwargs):
                calls.append(kwargs["dry_run"])

            def run(self):
                started.set()
                finish.wait(5)

        with patch("packs.indexing.primitives.upload_powerset.upload_powerset.UploadPowerset", FakeUploader):
            try:
                self.assertEqual(self._post()[0], 200)
                self.assertTrue(started.wait(5))
                code, status = self._post("/api/people/upload/check")
                self.assertEqual(code, 200)
                self.assertEqual(status["status"], "running")
                self.assertFalse(status["checking"])
                self.assertEqual(calls, [False])
            finally:
                finish.set()

    def test_preview_count_prefers_target_aware_plan(self) -> None:
        self.upload_dir.mkdir()
        (self.upload_dir / "manifest.json").write_text(json.dumps({
            "status": "completed", "dry_run": True,
            "person_hashes": {"person-a": "old-target"},
            "plan": {"previously_uploaded": 0},
        }))
        self.assertEqual(self._get()["previously_uploaded"], 0)
