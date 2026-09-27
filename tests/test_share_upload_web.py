"""Mock-only People upload route and status contract tests."""

from __future__ import annotations

import io
import json
import urllib.parse
from dataclasses import replace
from unittest import mock

from packs.indexing.primitives.upload_powerset.manifest import (
    CHANGED_CHECK, CHECK_FAILED, CHECK_FIRST, UploadManifest, share_digest,
)
from packs.ingestion.primitives.deep_context.db.share_views import share_decisions
from packs.ingestion.primitives.deep_context.db.store import open_existing_db
from packs.ingestion.primitives.share.web.upload import ShareUpload
from packs.ingestion.primitives.share.web.server import share_routes
from test_share_web import ShareWebFixture


class Handler:
    def __init__(self, origin: str | None = None, host: str = "127.0.0.1:8797", body: bytes = b"{}"):
        self.headers = {"Host": host, "Content-Length": str(len(body))}
        if origin is not None:
            self.headers["Origin"] = origin
        self.rfile = io.BytesIO(body)
        self.connection = object()
        self.wfile = io.BytesIO()
        self.response_status = None

    def send_response(self, status):
        self.response_status = status

    def send_header(self, name, value):
        pass

    def end_headers(self):
        pass


class UploadRoutesTests(ShareWebFixture):
    def setUp(self) -> None:
        super().setUp()
        self.upload_dir = self.root / "upload-powerset"
        self.routes = share_routes(self.db, self.people_csv,
                                   upload_db=self.root / "local-search.duckdb", upload_dir=self.upload_dir)

    def _post(self, path="/api/people/upload", origin=None, body=b"{}", host="127.0.0.1:8797"):
        handler = Handler(origin, host, body)
        self.assertTrue(self.routes.post(handler, urllib.parse.urlparse(path)))
        return handler.response_status, json.loads(handler.wfile.getvalue()) if handler.wfile.getvalue().startswith(b"{") else None

    def _get(self):
        handler = Handler()
        self.assertTrue(self.routes.get(handler, urllib.parse.urlparse("/api/people/upload")))
        self.assertEqual(handler.response_status, 200)
        return json.loads(handler.wfile.getvalue())

    def test_idle_status_has_every_contract_field(self):
        self.assertEqual(self._get(), {
            "status": "idle", "stage": None, "message": None,
            "progress": {"total": 0, "uploaded": 0, "skipped": 0, "namespaces": {}},
            "plan": None, "checked": None, "last_upload": None, "failed_action": None, "error": None,
        })

    def test_upload_requires_completed_check(self):
        code, body = self._post()
        self.assertEqual((code, body), (409, {"error": CHECK_FIRST}))

    def test_confirm_rejects_changed_share_decisions(self):
        replace(UploadManifest(), status="completed", dry_run=True,
                plan={"marked_share": 1}, share_digest="old").write(self.routes.upload.manifest_path)
        code, body = self._post(body=json.dumps({"checked": "old"}).encode())
        self.assertEqual((code, body), (409, {"error": CHANGED_CHECK}))

    def _checked(self) -> str:
        digest = share_digest(share_decisions(open_existing_db(self.db.db_path)))
        replace(UploadManifest(), status="completed", dry_run=True,
                plan={"marked_share": 1}, share_digest=digest).write(self.routes.upload.manifest_path)
        return digest

    def test_confirm_must_carry_the_check_it_displayed(self):
        digest = self._checked()
        self.assertEqual(self._get()["checked"], digest)
        self.assertEqual(self._post(), (409, {"error": CHANGED_CHECK}))
        self.assertEqual(self._post(body=json.dumps({"checked": "another-tab"}).encode()),
                         (409, {"error": CHANGED_CHECK}))
        with mock.patch.object(ShareUpload, "_run"):
            code, _ = self._post(body=json.dumps({"checked": digest}).encode())
        self.assertEqual(code, 200)

    def test_unreadable_store_answers_the_check_failed_sentence(self):
        digest = self._checked()
        with mock.patch("packs.ingestion.primitives.share.web.upload.open_existing_db", side_effect=SystemExit(2)):
            code, body = self._post(body=json.dumps({"checked": digest}).encode())
        self.assertEqual((code, body), (409, {"error": CHECK_FAILED}))
        self.assertTrue((self.upload_dir / "errors.log").exists())

    def test_post_rejects_a_rebound_host_and_a_missing_host(self):
        self.assertEqual(self._post(origin="http://attacker.example:8797", host="attacker.example:8797")[0], 403)
        self.assertEqual(self._post(origin="http://127.0.0.1:8797", host="")[0], 403)
        with mock.patch.object(self.routes.upload, "start", return_value={"status": "checking"}):
            self.assertEqual(self._post("/api/people/upload/check", origin="http://[::1]:8797", host="[::1]:8797"),
                             (200, {"status": "checking"}))

    def test_active_run_rejects_confirm_and_joins_check(self):
        self.routes.upload._thread = mock.Mock(is_alive=mock.Mock(return_value=True))
        self.assertEqual(self._post()[0], 409)
        self.assertEqual(self._post("/api/people/upload/check")[0], 200)

    def test_post_requires_exact_origin_when_present(self):
        for origin in ("https://elsewhere.example", "http://127.0.0.1:1", "http://localhost:8797",
                       "https://127.0.0.1:8797", "http://127.0.0.1:8797/"):
            self.assertEqual(self._post(origin=origin)[0], 403)
            self.assertEqual(self._post("/api/people/upload/check", origin=origin)[0], 403)
        with mock.patch.object(self.routes.upload, "start", return_value={"status": "checking"}) as start:
            self.assertEqual(self._post("/api/people/upload/check", origin="http://127.0.0.1:8797"),
                             (200, {"status": "checking"}))
            self.assertEqual(self._post("/api/people/upload/check"), (200, {"status": "checking"}))
            self.assertEqual(start.call_count, 2)

    def test_failed_status_does_not_expose_provider_text(self):
        replace(UploadManifest(), status="failed", dry_run=False,
                error="DATABASE_URL=synthetic-secret").write(self.routes.upload.manifest_path)
        status = self._get()
        self.assertEqual(status["failed_action"], "upload")
        self.assertNotIn("synthetic-secret", json.dumps(status))

    def test_progress_exposes_only_contract_counts(self):
        replace(UploadManifest(), status="completed", dry_run=False,
                progress={"total": 2, "uploaded": 2, "skipped": 0,
                          "namespaces": {"people": {"upserted": 2, "patched": 0, "secret": "hidden"},
                                         "private": {"upserted": 99}}}).write(self.routes.upload.manifest_path)
        self.assertEqual(self._get()["progress"]["namespaces"],
                         {"people": {"upserted": 2, "patched": 0}})
