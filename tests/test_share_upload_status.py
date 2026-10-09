"""The People upload's status route reads the manifest and writes nothing; only a run the last server left
running is marked interrupted, once, when the server starts.

Changelog:
- 2026-10-09: created.
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from packs.indexing.primitives.upload_powerset.manifest import INTERRUPTED, UploadManifest
from packs.ingestion.primitives.share.web import upload
from packs.ingestion.primitives.share.web.upload import ShareUpload


class ShareUploadStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.out_dir = Path(temp.name)
        self.manifest = self.out_dir / "manifest.json"
        self.enterContext(mock.patch.object(upload, "share_rows", return_value=()))

    def driver(self) -> ShareUpload:
        return ShareUpload(Path("store.sqlite"), Path("people.csv"), out_dir=self.out_dir)

    def test_a_run_left_running_reads_interrupted_after_a_restart(self) -> None:
        running = replace(UploadManifest(), status="running", dry_run=False,
                          progress={"total": 8, "uploaded": 3, "skipped": 2, "namespaces": {}})
        running.write(self.manifest)
        status = self.driver().status()
        self.assertEqual((status["status"], status["error"]), ("interrupted", INTERRUPTED))
        self.assertEqual((status["last_upload"]["uploaded"], status["last_upload"]["skipped"]), (3, 2))

    def test_reading_the_status_writes_nothing(self) -> None:
        replace(UploadManifest(), status="completed", dry_run=True, share_digest="old").write(self.manifest)
        driver = self.driver()
        before = self.manifest.read_bytes()
        self.assertEqual(driver.status()["status"], "idle")
        self.assertEqual(self.manifest.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
