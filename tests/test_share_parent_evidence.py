"""Share evidence follows SQLite parent ownership, never old alias or profile matches."""

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import ArtifactRow, FactRow, ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.ingestion.primitives.share.share_list import ShareList
from packs.ingestion.primitives.share.store import TagStore
from packs.ingestion.primitives.share.web.model import SharePeople
from packs.ingestion.primitives.share.web.server import decide_tags


class ShareParentEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Db(Path(temporary.name) / "deep-context.sqlite")
        self.db.project_rows((
            ParentRow("parent-a", "parent-a", display_name="Jordan Bravo", machine_worth="yes"),
            ParentRow("parent-b", "parent-b", display_name="Casey Delta", machine_worth="no"),
            PersonRow("jordan-email", "parent-a"), PersonRow("jordan-phone", "parent-a"),
            PersonRow("casey-email", "parent-b"),
        ))

    def _roster(self, *rows):
        self.db.replace_imported_people(tuple(PeopleRow.model_validate(row) for row in rows))

    def test_two_source_contacts_in_one_parent_emit_one_aggregated_row(self):
        self._roster(
            {"id": "jordan-email", "full_name": "Jordan Bravo", "source_channels": '["gmail_msgvault"]',
             "interaction_counts": '{"gmail": 12}', "last_interaction": "2026-09-01"},
            {"id": "jordan-phone", "full_name": "Jordan", "source_channels": "imessage",
             "interaction_counts": '{"imessage": 40, "gmail": 7}', "last_interaction": "2026-09-30"},
        )
        rows = ShareEvidence(self.db).load()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].person_id, "jordan-email")
        self.assertEqual(set(rows[0].source_channels), {"gmail_msgvault", "imessage"})
        self.assertEqual(rows[0].interaction_counts, {"gmail": 12, "imessage": 40})
        self.assertEqual(rows[0].last_interaction, "2026-09-30T00:00:00+00:00")
        self.assertIn("jordan-phone", rows[0].superseded_person_ids)
        result = ShareList(db=self.db, out_dir=self.db.db_path.parent / "share").run()
        self.assertEqual(result.people, 1)
        self.assertEqual(self.db.query("SELECT person_id FROM share")[0][0], "jordan-email")
        people = SharePeople(self.db)
        row = people.load()[0]
        self.assertEqual((row.interactions, set(row.channels)), (52, {"gmail", "imessage"}))
        self.assertIn("Jordan", people.detail("parent-a").aliases)
        decided = decide_tags(self.db, people, {"parent-a": frozenset({"share"})})
        self.assertEqual([row.person_id for row in decided["parent-a"]], ["jordan-email"])
        self.assertEqual([row[0] for row in self.db.query("SELECT person_id FROM person_tags ORDER BY person_id")],
                         ["jordan-email", "jordan-phone"])
        self.assertEqual(self.db.query("SELECT count(*) FROM share")[0][0], 1)

    def test_separate_parents_with_same_profile_and_old_alias_stay_separate(self):
        self._roster(
            {"id": "jordan-email", "full_name": "Jordan Bravo", "public_identifier": "same-profile"},
            {"id": "casey-email", "full_name": "Casey Delta", "public_identifier": "same-profile",
             "superseded_person_ids": '["jordan-email"]'},
        )
        rows = {row.person_id: row for row in ShareEvidence(self.db).load()}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows["jordan-email"].network_worth, "yes")
        self.assertEqual(rows["casey-email"].network_worth, "no")
        self.assertNotIn("jordan-email", rows["casey-email"].superseded_person_ids)
        TagStore(self.db).apply("jordan-email", add={"private"}, remove=set(), note="Jordan only")
        ShareList(db=self.db, out_dir=self.db.db_path.parent / "share").run()
        people = SharePeople(self.db)
        self.assertEqual(set(people.families()), {"parent-a", "parent-b"})
        self.assertEqual(people.detail("parent-b").note, "")
        decide_tags(self.db, people, {"parent-b": frozenset({"share"})})
        held = TagStore(self.db).load()
        self.assertEqual(held["jordan-email"].tags, frozenset({"private"}))
        self.assertIsNone(held["casey-email"].note)

    def test_parent_dossier_and_bundle_win_over_newer_child_artifacts(self):
        self._roster({"id": "jordan-email", "full_name": "Jordan Bravo"})
        for kind, parent_payload, child_payload in (
            ("dossier", {"body": "combined parent context"}, {"body": "child only"}),
            ("source_bundle", {"messages": [{"direction": "from_me"}, {"direction": "from_them"}]},
             {"messages": [{"direction": "from_them"}] * 7}),
        ):
            self.db.project_rows((
                ArtifactRow(f"{kind}:parent-a", kind, "parent-a", "/unused", "parent", "projected",
                            payload_json=json.dumps(parent_payload), projected_at="2026-09-01"),
                ArtifactRow(f"{kind}:jordan-email", kind, "parent-a", "/unused", "child", "projected",
                            person_id="jordan-email", payload_json=json.dumps(child_payload), projected_at="2026-09-30"),
            ))
        row = ShareEvidence(self.db).load()[0]
        self.assertEqual(row.dossier, "combined parent context")
        self.assertEqual((row.messages.from_me, row.messages.from_them), (1, 1))
        ShareList(db=self.db, out_dir=self.db.db_path.parent / "share").run()
        self.assertIn("combined parent context", SharePeople(self.db).detail("parent-a").dossier_html)

    def test_actual_parent_aggregate_wins_over_carried_original_parent_facts(self):
        self._roster({"id": "jordan-email", "full_name": "Jordan Bravo"})
        self.db.project_rows((
            ArtifactRow("parent-facts:parent-a", "facts", "parent-a", "/unused", "aggregate", "projected"),
            FactRow("parent-a", "parent-a", "parent-facts:parent-a", facts_json='{"labels": {"relationship_kind": "colleague"}}'),
            ArtifactRow("facts:z-original", "facts", "parent-a", "/unused", "original", "projected"),
            FactRow("z-original", "parent-a", "facts:z-original", facts_json='{"labels": {"relationship_kind": "stranger"}}'),
        ))
        self.assertEqual(ShareEvidence(self.db).load()[0].facts["labels"]["relationship_kind"], "colleague")


if __name__ == "__main__":
    unittest.main()
