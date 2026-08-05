from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.common.legacy import MESSAGE_LINKEDIN_PREFIX
from packs.ingestion.primitives.deep_context.db import batons
from packs.ingestion.primitives.deep_context.db.legacy import LegacyImportError, import_legacy
from packs.ingestion.primitives.deep_context.db.projectors import ProjectionError, project_manifest
from packs.ingestion.primitives.deep_context.db.schema import ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.views import worth_counts
from packs.ingestion.schemas.people_schema import generate_person_id, legacy_message_linkedin_id


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ProjectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        with self.db.connect() as conn:
            self.db.project_parent(ParentRow("parent-1", "parent-worth:parent-1", "Jordan Bravo"), conn=conn)
            self.db.project_person(PersonRow("person-a", "parent-1", display_name="Jordan Bravo"), conn=conn)
            self.db.project_person(PersonRow("person-b", "parent-1", display_name="Jordan B."), conn=conn)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_manifest(self, artifacts: list[dict], **extra: object) -> Path:
        path = self.root / "manifest.json"
        payload = {
            "stage": "enrich", "status": "completed",
            "selection": {"fingerprint": "selection-1"},
            "counts": {"total": len(artifacts), "completed": len(artifacts), "failed": 0},
            "artifacts": artifacts, "completed_at": "2026-08-05T00:00:00Z",
            **extra,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _research(self, *, suffix: str = "one") -> tuple[Path, dict]:
        directory = self.root / "subject"
        directory.mkdir(exist_ok=True)
        raw = directory / "00_parallel_raw.json"
        result = directory / "01_research_parallel.json"
        raw.write_text(json.dumps({"provider": suffix}), encoding="utf-8")
        result.write_text(json.dumps({
            "person": {"full_name": "Jordan Bravo", "confidence": .91},
            "social": {"linkedin_url": f"https://www.linkedin.com/in/jordan-{suffix}"},
            "metadata": {"research_notes": "synthetic fixture"},
        }), encoding="utf-8")
        return result, {
            "kind": "research", "artifact_key": "research:subject",
            "parent_id": "parent-1", "candidate_key": "candidate:email:jordan",
            "handle": "subject", "person_ids": ["person-a", "person-b"],
            "public_identifier": "candidate:email:jordan",
            "path": "subject/01_research_parallel.json", "sha256": _sha(result),
            "raw_path": "subject/00_parallel_raw.json", "raw_sha256": _sha(raw),
        }

    def test_research_projects_candidate_memberships_job_and_is_idempotent(self) -> None:
        _, entry = self._research()
        manifest = self._write_manifest([entry])
        first = project_manifest(self.db, manifest)
        second = project_manifest(self.db, manifest)
        self.assertEqual((first.artifacts, first.projected), (2, 2))
        self.assertEqual(second.projected, 0)
        candidate = self.db.query("SELECT * FROM links")[0]
        self.assertEqual(candidate["machine_action"], "retarget")
        self.assertEqual(candidate["machine_proposed_public_identifier"], "jordan-one")
        self.assertEqual(len(self.db.query("SELECT * FROM candidate_people")), 2)
        self.assertEqual(self.db.query("SELECT status FROM jobs")[0]["status"], "applied")
        self.assertEqual(self.db.query("SELECT status FROM stage_state")[0]["status"], "complete")

    def test_changed_research_updates_machine_fields_not_human_decision(self) -> None:
        _, entry = self._research()
        manifest = self._write_manifest([entry])
        project_manifest(self.db, manifest)
        self.db.settle_identity("candidate:email:jordan", "verify")
        _, changed = self._research(suffix="two")
        self._write_manifest([changed])
        self.assertEqual(project_manifest(self.db, manifest).projected, 2)
        row = self.db.query("SELECT * FROM links")[0]
        self.assertEqual(row["machine_proposed_public_identifier"], "jordan-two")
        self.assertEqual((row["decision_action"], row["decision_approved"]), ("verify", "yes"))

    def test_multiline_facts_jsonl_projects_last_worth(self) -> None:
        path = self.root / "person-a.jsonl"
        path.write_text(
            json.dumps({"canonical_name": "Jordan Bravo", "network_worth": {"decision": "no"}})
            + "\n" + json.dumps({"facts": {"network_worth": {
                "decision": "yes", "reason": "Known collaborator"}, "confidence": .8}}) + "\n",
            encoding="utf-8",
        )
        manifest = self._write_manifest([{
            "kind": "facts", "parent_id": "parent-1", "person_id": "person-a",
            "subject_key": "person-a", "path": "person-a.jsonl", "sha256": _sha(path),
        }])
        project_manifest(self.db, manifest)
        row = self.db.query("SELECT * FROM facts")[0]
        self.assertEqual((row["machine_worth"], row["machine_worth_reason"]),
                         ("yes", "Known collaborator"))

    def test_synthetic_projection_preserves_human_gate_on_refresh(self) -> None:
        path = self.root / "synthetic.json"
        path.write_text(json.dumps({
            "public_identifier": "synth-jordan", "full_name": "Jordan Bravo",
        }), encoding="utf-8")
        entry = {
            "kind": "synthetic", "parent_id": "parent-1",
            "candidate_key": "synthetic:synth-jordan", "public_identifier": "synth-jordan",
            "person_ids": ["person-a"], "approved": "auto",
            "path": "synthetic.json", "sha256": _sha(path),
        }
        manifest = self._write_manifest([entry])
        project_manifest(self.db, manifest)
        self.db.settle_identity("synthetic:synth-jordan", "detach")
        path.write_text(json.dumps({
            "public_identifier": "synth-jordan", "full_name": "Jordan Bravo Updated",
        }), encoding="utf-8")
        entry["sha256"] = _sha(path)
        self._write_manifest([entry])
        project_manifest(self.db, manifest)
        row = self.db.query("SELECT * FROM links")[0]
        self.assertEqual((row["decision_action"], row["decision_approved"]), ("detach", "yes"))
        profile = json.loads(self.db.query("SELECT profile_json FROM synthetic_profiles")[0][0])
        self.assertEqual(profile["full_name"], "Jordan Bravo Updated")

    def test_hash_path_and_malformed_fail_before_any_write(self) -> None:
        facts = self.root / "facts.jsonl"
        facts.write_text(json.dumps({"network_worth": {"decision": "yes"}}), encoding="utf-8")
        invalid = {
            "kind": "facts", "parent_id": "parent-1", "person_id": "person-a",
            "path": "facts.jsonl", "sha256": "0" * 64,
        }
        with self.assertRaises(ProjectionError):
            project_manifest(self.db, self._write_manifest([invalid]))
        self.assertFalse(self.db.query("SELECT 1 FROM artifacts"))

        invalid["path"], invalid["sha256"] = "../escape.json", _sha(facts)
        with self.assertRaises(ProjectionError):
            project_manifest(self.db, self._write_manifest([invalid]))
        facts.write_text("not-json", encoding="utf-8")
        invalid["path"], invalid["sha256"] = "facts.jsonl", _sha(facts)
        with self.assertRaises(ProjectionError):
            project_manifest(self.db, self._write_manifest([invalid]))
        self.assertFalse(self.db.query("SELECT 1 FROM artifacts"))

    def test_terminal_manifest_requires_inventory(self) -> None:
        path = self.root / "manifest.json"
        path.write_text(json.dumps({"stage": "enrich", "status": "completed"}), encoding="utf-8")
        with self.assertRaisesRegex(ProjectionError, "artifacts inventory"):
            project_manifest(self.db, path)


class LegacyProjectorTest(unittest.TestCase):
    def test_direct_enveloped_alias_owner_and_latest_child_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts_dir = root / "facts"
            facts_dir.mkdir()
            pub = "jordan-bravo"
            durable = generate_person_id(pub)
            retired = legacy_message_linkedin_id(pub)
            self.assertTrue(retired.startswith(MESSAGE_LINKEDIN_PREFIX))
            parent_id = "parent-jordan"
            (root / "index.json").write_text(json.dumps({
                "slugs": {"jordan": {"person_id": durable}},
                "parents": {"jordan": {
                    "parent_id": parent_id, "name": "Jordan Bravo", "children": ["jordan"]}},
                "by_email": {"jordan@example.com": ["jordan"]},
                "by_phone": {"+15550100": ["jordan"]},
            }), encoding="utf-8")
            (facts_dir / f"{retired}.jsonl").write_text(
                json.dumps({"canonical_name": "Jordan Bravo", "network_worth": {"decision": "no"}})
                + "\n" + json.dumps({"facts": {"network_worth": {
                    "decision": "yes", "reason": "Worked together"}}}) + "\n",
                encoding="utf-8",
            )
            owner = "owner-person"
            (facts_dir / f"{owner}.jsonl").write_text(json.dumps({
                "canonical_name": "Mailbox Owner", "is_owner": True,
                "network_worth": {"decision": "yes"},
            }), encoding="utf-8")
            review = root / "review.csv"
            row = {column: "" for column in batons.OVERRIDE_COLUMNS}
            row.update({
                "public_identifier": pub, "person_id": durable,
                "network_worth": "no", "updated_at": "2026-08-05T01:00:00Z",
            })
            batons.write_override_rows(review, {pub: row})
            db = Db(root / "canonical.sqlite")
            result = import_legacy(db, review_csv=review, index_json=root / "index.json",
                                   facts_dir=facts_dir)
            self.assertEqual(len(db.query("PRAGMA foreign_key_check")), 0)
            self.assertEqual(db.query(
                "SELECT parent_id FROM people WHERE person_id=?", (retired,)
            )[0][0], parent_id)
            parent = db.query("SELECT * FROM parents WHERE parent_id=?", (parent_id,))[0]
            self.assertEqual((parent["machine_worth"], parent["human_worth"]), ("yes", "no"))
            self.assertEqual(result["facts"], 2)
            self.assertEqual(db.query(
                "SELECT count(*) FROM person_identifiers WHERE person_id=?", (durable,)
            )[0][0], 2)
            with self.assertRaises(LegacyImportError):
                import_legacy(db, review_csv=review, index_json=root / "index.json",
                              facts_dir=facts_dir)

    def test_real_mirror_worth_and_foreign_keys_when_present(self) -> None:
        root = Path("/Users/arthur/workspace/powerpacks-jake-mirror/.powerpacks")
        if not root.exists():
            self.skipTest("diagnostic mirror is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            dc = root / "deep-context"
            review = root / "network-import/overrides/review.csv"
            db = Db(Path(tmp) / "canonical.sqlite")
            import_legacy(
                db, review_csv=review, synthetic_csv=review.parent / "synthetic-people.csv",
                index_json=dc / "index.json", facts_dir=dc / "facts",
                verdicts_jsonl=dc / "reconcile/verdicts.jsonl",
                research_dir=dc / "reconcile/deep-research",
            )
            self.assertEqual(len(db.query("PRAGMA foreign_key_check")), 0)
            self.assertEqual(worth_counts(db), {
                "total": 5379, "pending": 61, "yes": 4169, "no": 1149,
            })


if __name__ == "__main__":
    unittest.main()
