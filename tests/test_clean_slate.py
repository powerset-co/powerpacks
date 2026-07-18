"""clean_slate: dry run touches nothing; --apply moves derived state to the
backup (outside the root) and leaves every preserved paid artifact in place.
Created: 2026-07-18
"""
import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.clean_slate import clean_slate


def build_state(root: Path) -> None:
    files = {
        # scrub targets
        "network-import/merged/people.csv": "id\n1\n",
        "network-import/directory.csv": "source\nx\n",
        "network-import/overrides/review.csv": "public_identifier\np\n",
        "network-import/import/gmail/candidates.csv": "candidate_key\nk\n",
        "network-import/import/messages/candidates.csv": "candidate_key\nk\n",
        "network-import/import/messages.bkup-20260101/candidates.csv": "candidate_key\nk\n",
        "deep-context/index.json": "{}",
        "deep-context/parents/a.md": "# a",
        "deep-context/dossiers/a.md": "# a",
        "deep-context/review-8765.log": "log",
        "deep-context/reconcile/summary.md": "s",
        # preserved paid artifacts
        "deep-context/facts/candidate:email:a@example.com.jsonl": "{}\n",
        "deep-context/merge-verdicts.csv": "pair\np\n",
        "deep-context/reconcile/verdicts.jsonl": "{}\n",
        "deep-context/reconcile/deep-research/slug/01_research_parallel.json": "{}",
        "network-import/import/linkedin/people.csv": "id\n1\n",
        "network-import/profile_cache_v2/pub.json": "{}",
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class TestCleanSlate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / ".powerpacks"
        self.backup = self.base / "backups" / "run1"
        build_state(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_moves_nothing(self):
        rc = clean_slate.main(["--root", str(self.root),
                               "--backup-dir", str(self.backup)])
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "network-import/merged/people.csv").exists())
        self.assertFalse(self.backup.exists())

    def test_apply_moves_derived_and_keeps_paid(self):
        rc = clean_slate.main(["--root", str(self.root),
                               "--backup-dir", str(self.backup), "--apply"])
        self.assertEqual(rc, 0)
        # derived state moved out
        for rel in ("network-import/merged", "network-import/directory.csv",
                    "network-import/overrides", "network-import/import/gmail",
                    "network-import/import/messages",
                    "network-import/import/messages.bkup-20260101",
                    "deep-context/index.json", "deep-context/parents",
                    "deep-context/dossiers", "deep-context/review-8765.log",
                    "deep-context/reconcile/summary.md"):
            self.assertFalse((self.root / rel).exists(), rel)
            self.assertTrue((self.backup / rel).exists(), rel)
        # paid artifacts untouched
        for rel in ("deep-context/facts/candidate:email:a@example.com.jsonl",
                    "deep-context/merge-verdicts.csv",
                    "deep-context/reconcile/verdicts.jsonl",
                    "deep-context/reconcile/deep-research/slug/01_research_parallel.json",
                    "network-import/import/linkedin/people.csv",
                    "network-import/profile_cache_v2/pub.json"):
            self.assertTrue((self.root / rel).exists(), rel)
        manifest = json.loads(
            (self.backup / "clean-slate-manifest.json").read_text())
        self.assertIn("network-import/merged", manifest["moved"])
        self.assertIn("deep-context/facts", manifest["preserved"])

    def test_backup_inside_root_is_refused(self):
        rc = clean_slate.main(["--root", str(self.root),
                               "--backup-dir", str(self.root / "bk"), "--apply"])
        self.assertEqual(rc, 2)
        self.assertTrue((self.root / "network-import/merged/people.csv").exists())


if __name__ == "__main__":
    unittest.main()
