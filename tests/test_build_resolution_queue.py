import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/build_resolution_queue/build_resolution_queue.py"
spec = importlib.util.spec_from_file_location("build_resolution_queue", MODULE_PATH)
brq = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = brq
spec.loader.exec_module(brq)


def _rec(email, full_name, markers, linkedin_query="", canonical=""):
    return {
        "email": email, "full_name": full_name, "primary_email_type": "work",
        "markers": {"canonical_name": canonical, "markers": markers,
                    "linkedin_query": linkedin_query, "overall_confidence": 0.9},
    }


class ComposeContextTests(unittest.TestCase):
    def test_orders_categories_and_appends_query(self):
        ctx = brq.compose_context({
            "markers": [
                {"category": "employers", "value": "Roblox (current)"},
                {"category": "school", "value": "USC"},
            ],
            "linkedin_query": "Jane Doe Roblox",
        })
        self.assertEqual(ctx, "employers: Roblox (current) | school: USC | search hint: Jane Doe Roblox")

    def test_empty_when_no_markers_or_query(self):
        self.assertEqual(brq.compose_context({"markers": [], "linkedin_query": ""}), "")

    def test_multiple_employers_joined(self):
        ctx = brq.compose_context({
            "markers": [
                {"category": "employers", "value": "A (current)"},
                {"category": "employers", "value": "B (past)"},
            ], "linkedin_query": ""})
        self.assertEqual(ctx, "employers: A (current); B (past)")


class BuildQueuesTests(unittest.TestCase):
    def _write_markers(self, d, recs):
        p = Path(d) / "markers.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
        return p

    def test_writes_paired_queues_skipping_empty(self):
        with tempfile.TemporaryDirectory() as d:
            recs = [
                _rec("a@x.com", "A Person", [{"category": "employers", "value": "Acme (current)"}], "A Person Acme"),
                _rec("noise@x.com", "Noise", [], ""),  # no markers -> skipped
                _rec("b@x.com", "B", [{"category": "school", "value": "MIT"}], "B MIT", canonical="Bob B"),
            ]
            self._write_markers(d, recs)
            args = brq.build_parser().parse_args(["--markers", str(Path(d) / "markers.jsonl"), "--out-dir", d])
            manifest = brq.build_queues(args)
            self.assertEqual(manifest["contacts_queued"], 2)  # noise skipped

            ctrl = list(csv.DictReader(open(Path(d) / "queue_control.csv")))
            ctx = list(csv.DictReader(open(Path(d) / "queue_context.csv")))
            self.assertEqual([r["email"] for r in ctrl], ["a@x.com", "b@x.com"])
            # same rows, but control context is blank, treatment is filled
            self.assertTrue(all(r["context"] == "" for r in ctrl))
            self.assertTrue(all(r["context"] for r in ctx))
            # canonical_name overrides display name
            self.assertEqual([r["full_name"] for r in ctx], ["A Person", "Bob B"])

    def test_aborts_on_missing_markers(self):
        with tempfile.TemporaryDirectory() as d:
            args = brq.build_parser().parse_args(["--markers", str(Path(d) / "nope.jsonl"), "--out-dir", d])
            with self.assertRaises(SystemExit):
                brq.build_queues(args)

    def test_aborts_when_no_resolvable_contacts(self):
        with tempfile.TemporaryDirectory() as d:
            self._write_markers(d, [_rec("noise@x.com", "Noise", [], "")])
            args = brq.build_parser().parse_args(["--markers", str(Path(d) / "markers.jsonl"), "--out-dir", d])
            with self.assertRaises(SystemExit):
                brq.build_queues(args)


if __name__ == "__main__":
    unittest.main()
