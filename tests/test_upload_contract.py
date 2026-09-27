"""Mock-only checks for People upload confirmation and status."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import duckdb

from packs.indexing.primitives.upload_powerset import local_index, upload_powerset
from packs.indexing.primitives.upload_powerset.manifest import (
    CHANGED_CHECK, CHECK_FAILED, INTERRUPTED, UPLOAD_FAILED, Stage, UploadManifest,
)
from packs.ingestion.primitives.share.web.upload import ShareUpload


class UploadContractTests(unittest.TestCase):
    def test_confirm_rejects_changed_share_decisions_before_thread_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upload = ShareUpload(root / "share.sqlite", root / "people.csv", out_dir=root)
            replace(UploadManifest(), status="completed", dry_run=True,
                    plan={"marked_share": 1}, share_digest="checked").write(upload.manifest_path)
            with mock.patch.object(upload, "_current_share_digest", return_value="changed"):
                with self.assertRaisesRegex(ValueError, CHANGED_CHECK):
                    upload.start(dry_run=False)
            self.assertIsNone(upload._thread)

    def test_status_reports_contract_states_and_safe_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upload = ShareUpload(root / "share.sqlite", root / "people.csv", out_dir=root)
            plan = dict.fromkeys(("marked_share", "with_linkedin", "without_linkedin", "new_to_cloud",
                                  "changed", "already_shared", "losing_access", "companies_missing"), 1)
            cases = (
                (UploadManifest(), "idle", None, None),
                (replace(UploadManifest(), status="running", dry_run=True, stage=Stage.CHECKING_ACCESS),
                 "checking", None, None),
                (replace(UploadManifest(), status="completed", dry_run=True, plan=plan, share_digest="d1"),
                 "ready", None, None),
                (replace(UploadManifest(), status="running", dry_run=False, stage=Stage.WRITING_PEOPLE,
                         plan=plan), "uploading", None, None),
                (replace(UploadManifest(), status="completed", dry_run=False, plan=plan),
                 "completed", None, None),
                (replace(UploadManifest(), status="failed", dry_run=True, error="provider secret"),
                 "failed", "check", CHECK_FAILED),
                (replace(UploadManifest(), status="failed", dry_run=False, error="provider secret"),
                 "failed", "upload", UPLOAD_FAILED),
                (replace(UploadManifest(), status="running", dry_run=False),
                 "interrupted", "upload", INTERRUPTED),
            )
            for saved, state, action, error in cases:
                saved.write(upload.manifest_path)
                live = state in {"checking", "uploading"}
                upload._thread = mock.Mock(is_alive=mock.Mock(return_value=live)) if live else None
                status = upload.status()
                self.assertEqual((status["status"], status["failed_action"], status["error"]),
                                 (state, action, error))
                self.assertEqual(set(status), {"status", "stage", "message", "progress", "plan",
                                               "checked", "last_upload", "failed_action", "error"})
                if state == "uploading":
                    self.assertEqual(status["stage"], "writing_people")
                if state == "ready":
                    self.assertEqual(status["plan"], plan)
                if state == "interrupted":
                    self.assertEqual(status["last_upload"]["status"], "interrupted")

    def test_job_systemexit_is_logged_and_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upload = ShareUpload(root / "share.sqlite", root / "people.csv", out_dir=root)
            replace(UploadManifest(), status="running", dry_run=False).write(upload.manifest_path)
            with mock.patch.object(upload_powerset, "UploadPowerset", side_effect=SystemExit("local db failed")):
                upload._run(False)
            saved = UploadManifest.read(upload.manifest_path)
            self.assertEqual((saved.status, saved.error), ("failed", UPLOAD_FAILED))
            self.assertIn("SystemExit", (root / "errors.log").read_text())

    def test_status_systemexit_becomes_failed_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upload = ShareUpload(root / "share.sqlite", root / "people.csv", out_dir=root)
            with mock.patch.object(upload, "_status", side_effect=[SystemExit("db failed"), {
                "status": "failed"}]):
                self.assertEqual(upload.status()["status"], "failed")
            self.assertEqual(UploadManifest.read(upload.manifest_path).error, CHECK_FAILED)
            self.assertIn("SystemExit", (root / "errors.log").read_text())

    def test_mixed_v3_namespace_suffixes_fail_before_client_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.duckdb").touch()
            uploader = upload_powerset.UploadPowerset(db=root / "index.duckdb",
                share_db=root / "share.sqlite", people_csv=root / "people.csv", out_dir=root)
            def name(logical, **kwargs):
                return f"aleph_{logical}_v3_share_test" if logical == "schools" else f"aleph_{logical}_v3"
            with mock.patch.object(upload_powerset.tp_backend, "namespace_name", side_effect=name), \
                 mock.patch.object(upload_powerset.turbopuffer, "Turbopuffer") as client:
                with self.assertRaisesRegex(RuntimeError, "shared TurboPuffer v3 namespaces"):
                    uploader.run()
            client.assert_not_called()

    def test_missing_school_is_counted_and_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = duckdb.connect(str(Path(tmp) / "index.duckdb"))
            con.execute("CREATE TABLE local_people_education (base_id VARCHAR, canonical_education_id VARCHAR)")
            con.execute("CREATE TABLE local_education (id VARCHAR)")
            con.execute("INSERT INTO local_people_education VALUES ('person-a', 'missing-school')")
            self.assertEqual(local_index.entity_ids_by_person(con, "schools", ["person-a"]), {})
            self.assertEqual(local_index.count_missing_schools(con, ("person-a",)), 1)
            con.close()


if __name__ == "__main__":
    unittest.main()
