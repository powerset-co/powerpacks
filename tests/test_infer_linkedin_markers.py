import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/infer_linkedin_markers/infer_linkedin_markers.py"
spec = importlib.util.spec_from_file_location("infer_linkedin_markers", MODULE_PATH)
ilm = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = ilm
spec.loader.exec_module(ilm)


class OwnerIdentityBlockTests(unittest.TestCase):
    def test_block_names_owner_and_addresses(self):
        block = ilm.owner_identity_block({"name": "Arthur Chen", "emails": ["a@x.co", "a@y.co"]})
        self.assertIn("Arthur Chen", block)
        self.assertIn("a@x.co, a@y.co", block)
        self.assertIn("NEVER emit a marker", block)

    def test_block_empty_when_no_identity(self):
        self.assertEqual(ilm.owner_identity_block({}), "")
        self.assertEqual(ilm.owner_identity_block({"name": "", "emails": []}), "")

    def test_block_works_with_emails_only(self):
        block = ilm.owner_identity_block({"name": "", "emails": ["a@x.co"]})
        self.assertIn("the account owner", block)
        self.assertIn("a@x.co", block)


class LoadOwnerIdentityTests(unittest.TestCase):
    def test_reads_owner_from_sibling_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = Path(d) / "email_context.jsonl"
            ctx.write_text("", encoding="utf-8")
            owner = {"name": "Arthur Chen", "emails": ["a@x.co"]}
            (Path(d) / "manifest.json").write_text(json.dumps({"owner": owner}), encoding="utf-8")
            self.assertEqual(ilm.load_owner_identity(ctx), owner)

    def test_missing_manifest_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = Path(d) / "email_context.jsonl"
            ctx.write_text("", encoding="utf-8")
            self.assertEqual(ilm.load_owner_identity(ctx), {})

    def test_manifest_without_owner_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = Path(d) / "email_context.jsonl"
            ctx.write_text("", encoding="utf-8")
            (Path(d) / "manifest.json").write_text(json.dumps({"source": "x"}), encoding="utf-8")
            self.assertEqual(ilm.load_owner_identity(ctx), {})


if __name__ == "__main__":
    unittest.main()
