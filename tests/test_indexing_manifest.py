import tempfile
import unittest
from pathlib import Path

from packs.indexing.lib.fingerprints import build_fingerprints, operator_scope_slug
from packs.indexing.lib.manifest import build_manifest, manifest_ready, write_manifest, read_manifest, duckdb_checksum_file

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PEOPLE = ROOT / "tests/fixtures/indexing/people.csv"


class IndexingManifestTests(unittest.TestCase):
    def test_operator_scope_slug_is_path_safe_and_manifest_ready_requires_validation(self) -> None:
        slug = operator_scope_slug({"operator_id": "arthur@example.com/../bad", "default_operator_id": "arthur@example.com"})
        self.assertNotIn("@", slug)
        self.assertNotIn("/", slug)
        self.assertNotIn("..", slug)
        fps = build_fingerprints(FIXTURE_PEOPLE, operator_id="arthur@example.com/../bad")
        self.assertIn("combined", fps)
        with tempfile.TemporaryDirectory() as td:
            manifest = build_manifest(
                run_id="manifest",
                run_dir=td,
                input_path=FIXTURE_PEOPLE,
                operator_id="arthur@example.com/../bad",
                status="ready",
                validation={"contracts_ok": True, "duckdb_opened": True, "namespace_probes_ok": True, "hydration_parity_ok": True},
            )
            path = write_manifest(td, manifest)
            self.assertTrue(path.exists())
            self.assertTrue(manifest_ready(read_manifest(td)))
            manifest["validation"]["hydration_parity_ok"] = False
            self.assertFalse(manifest_ready(manifest))

    def test_manifest_ready_with_artifact_checks_requires_db_checksum_match(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = build_manifest(
                run_id="artifact-ready",
                run_dir=root,
                input_path=FIXTURE_PEOPLE,
                status="ready",
                validation={"contracts_ok": True, "duckdb_opened": True, "namespace_probes_ok": True, "hydration_parity_ok": True},
            )
            self.assertFalse(manifest_ready(manifest, root))
            db = root / "local-search.duckdb"
            db.write_bytes(b"duckdb-ish")
            self.assertFalse(manifest_ready(manifest, root))
            duckdb_checksum_file(db)
            self.assertTrue(manifest_ready(manifest, root))
            (root / "local-search.duckdb.sha256").write_text("bad\n", encoding="utf-8")
            self.assertFalse(manifest_ready(manifest, root))


if __name__ == "__main__":
    unittest.main()
