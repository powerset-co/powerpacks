import argparse
import asyncio
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
        block = ilm.owner_identity_block({"name": "Test Contact", "emails": ["a@x.co", "a@y.co"]})
        self.assertIn("Test Contact", block)
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
            owner = {"name": "Test Contact", "emails": ["a@x.co"]}
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


class Phase1GateTests(unittest.TestCase):
    """Step 2 must refuse to run (before any paid call) on a failed/empty step 1."""

    def _args(self, context, out_dir):
        return argparse.Namespace(
            context=str(context), out_dir=str(out_dir), model="gpt-5.2",
            sample_work=0, sample_personal=0, all=False, limit=500, exclude=[],
            concurrency=0, max_retries=8, owner_context="", force=False,
            open=False, timeout=60, open_flag=False,
        )

    def setUp(self):
        import os
        os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-used")  # pass the key check; gate fires before any call

    def test_missing_context_file_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            args = self._args(Path(d) / "email_context.jsonl", d)
            with self.assertRaises(SystemExit):
                asyncio.run(ilm.run_async(args))

    def test_empty_context_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = Path(d) / "email_context.jsonl"
            ctx.write_text("", encoding="utf-8")
            with self.assertRaises(SystemExit):
                asyncio.run(ilm.run_async(self._args(ctx, d)))

    def test_incomplete_step1_manifest_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = Path(d) / "email_context.jsonl"
            ctx.write_text(json.dumps({"email": "a@b.co", "recent_emails": []}) + "\n", encoding="utf-8")
            (Path(d) / "manifest.json").write_text(json.dumps({"status": "failed"}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                asyncio.run(ilm.run_async(self._args(ctx, d)))

    def test_zero_context_people_aborts(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = Path(d) / "email_context.jsonl"
            ctx.write_text(json.dumps({"email": "a@b.co", "recent_emails": []}) + "\n", encoding="utf-8")
            (Path(d) / "manifest.json").write_text(
                json.dumps({"status": "completed", "people_with_context": 0}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                asyncio.run(ilm.run_async(self._args(ctx, d)))


if __name__ == "__main__":
    unittest.main()
