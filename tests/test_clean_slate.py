"""clean_slate: dry run touches nothing; --apply moves derived state to the
backup (outside the root) and leaves every preserved paid artifact in place.
Created: 2026-07-18
"""
import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    FactRow,
    GuidanceRow,
    JobRow,
    LinkRow,
    MergeVerdictRow,
    ParentRow,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.setup import clean_slate


def build_state(root: Path) -> None:
    files = {
        # scrub targets
        "network-import/merged/people.csv": "id\n1\n",
        "network-import/directory.csv": "source\nx\n",
        "network-import/overrides/review.csv": "public_identifier\np\n",
        "network-import/import/gmail/candidates.csv": "candidate_key\nk\n",
        "network-import/import/messages/candidates.csv": "candidate_key\nk\n",
        "network-import/import/messages.bkup-20260101/candidates.csv": "candidate_key\nk\n",
        "deep-context/parents/a.md": "# a",
        "deep-context/dossiers/a.md": "# a",
        "deep-context/raw/parent-one.json": "{}",
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
    db = Db(root / "deep-context/deep-context.sqlite")
    db.project_rows((
        ParentRow("parent-one", "parent-one"),
        PersonRow("person-a", "parent-one"),
        PersonRow("person-b", "parent-one"),
        LinkRow("candidate-one", "parent-one", "candidate-one", "pub"),
        ArtifactRow(
            "facts:parent-one", "facts", "parent-one",
            str(root / "deep-context/facts/candidate:email:a@example.com.jsonl"),
            "1" * 64, "projected", payload_json="{}",
        ),
        FactRow(
            "parent-one", "parent-one", "facts:parent-one",
            machine_worth="yes", facts_json="{}",
        ),
        ArtifactRow(
            "dossier:parent-one", "dossier", "parent-one",
            str(root / "deep-context/dossiers/a.md"), "2" * 64, "projected",
        ),
        ArtifactRow(
            "source-bundle:parent-one", "source_bundle", "parent-one",
            str(root / "deep-context/raw/parent-one.json"),
            "3" * 64, "projected", payload_json="{}",
        ),
        GuidanceRow(
            "parent-one", "parent-one", "find them", candidate_key="candidate-one",
        ),
        JobRow("enrichment", "enrichment", "running"),
    ))
    db.replace_merge_verdicts((MergeVerdictRow(
        "person-a", "person-b", "a", "b", "sig", "judge",
        1, 0.99, 1, accepted=1,
    ),))
    db.decide_worth("parent-one", "yes", note="keep me")
    db.decide_identity("candidate-one", "detach", note="also keep me")


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
                    "deep-context/parents",
                    "deep-context/dossiers", "deep-context/review-8765.log",
                    "deep-context/reconcile/summary.md"):
            self.assertFalse((self.root / rel).exists(), rel)
            self.assertTrue((self.backup / rel).exists(), rel)
        # paid artifacts untouched
        for rel in ("deep-context/facts/candidate:email:a@example.com.jsonl",
                    "deep-context/deep-context.sqlite",
                    "deep-context/reconcile/verdicts.jsonl",
                    "deep-context/reconcile/deep-research/slug/01_research_parallel.json",
                    "network-import/import/linkedin/people.csv",
                    "network-import/profile_cache_v2/pub.json"):
            self.assertTrue((self.root / rel).exists(), rel)
        db = Db(self.root / "deep-context/deep-context.sqlite")
        self.assertEqual(
            [row["artifact_key"] for row in db.query(
                "SELECT artifact_key FROM artifacts ORDER BY artifact_key"
            )],
            ["facts:parent-one"],
        )
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM facts")[0]["n"], 1)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM merge_verdicts")[0]["n"], 1)
        parent = db.query(
            "SELECT human_worth, human_worth_note FROM parents WHERE parent_id='parent-one'"
        )[0]
        self.assertEqual(tuple(parent), ("yes", "keep me"))
        link = db.query(
            "SELECT decision_action, decision_note FROM links WHERE row_key='candidate-one'"
        )[0]
        self.assertEqual(tuple(link), ("detach", "also keep me"))
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"], 0)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM guidance")[0]["n"], 0)
        snapshot = Db(
            self.backup / "deep-context/deep-context.sqlite.before-clean-slate"
        )
        self.assertEqual(snapshot.query("SELECT COUNT(*) AS n FROM artifacts")[0]["n"], 3)
        self.assertEqual(snapshot.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"], 1)
        self.assertEqual(snapshot.query("SELECT COUNT(*) AS n FROM guidance")[0]["n"], 1)
        manifest = json.loads(
            (self.backup / "clean-slate-manifest.json").read_text())
        self.assertIn("network-import/merged", manifest["moved"])
        self.assertIn("deep-context/facts", manifest["preserved"])
        self.assertIn("deep-context/deep-context.sqlite", manifest["preserved"])
        self.assertEqual(
            manifest["sqlite_snapshot"],
            "deep-context/deep-context.sqlite.before-clean-slate",
        )

    def test_backup_inside_root_is_refused(self):
        rc = clean_slate.main(["--root", str(self.root),
                               "--backup-dir", str(self.root / "bk"), "--apply"])
        self.assertEqual(rc, 2)
        self.assertTrue((self.root / "network-import/merged/people.csv").exists())


if __name__ == "__main__":
    unittest.main()
