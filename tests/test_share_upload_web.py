"""Mock-only People upload route and status contract tests."""

from __future__ import annotations

import io
import json
import urllib.parse
from dataclasses import replace
from unittest import mock

from packs.indexing.primitives.upload_powerset.manifest import CHANGED_CHECK, CHECK_FIRST, UploadManifest
from packs.ingestion.primitives.share.web.server import share_routes
from test_share_web import ShareWebFixture


class Handler:
    def __init__(self, origin: str | None = None):
        self.headers = {"Host": "127.0.0.1:8797"}
        if origin is not None:
            self.headers["Origin"] = origin
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

    def _post(self, path="/api/people/upload", origin=None):
        handler = Handler(origin)
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
            "plan": None, "last_upload": None, "failed_action": None, "error": None,
        })

    def test_upload_requires_completed_check(self):
        code, body = self._post()
        self.assertEqual((code, body), (409, {"error": CHECK_FIRST}))

    def test_confirm_rejects_changed_share_decisions(self):
        replace(UploadManifest(), status="completed", dry_run=True,
                plan={"marked_share": 1}, share_digest="old").write(self.routes.upload.manifest_path)
        code, body = self._post()
        self.assertEqual((code, body), (409, {"error": CHANGED_CHECK}))

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
