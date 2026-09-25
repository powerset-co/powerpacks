"""Share labels follow the parent's saved JEV labels, not the child's own row."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from deep_context_sqlite_test_helpers import seed_identity
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.shared.csv_io import CsvIO

PEOPLE_COLUMNS = [
    "id",
    "public_identifier",
    "full_name",
    "source_channels",
    "interaction_counts",
    "last_interaction",
    "superseded_person_ids",
]


def _write_people(root: Path, rows: list[dict[str, str]]) -> Path:
    path = root / "people.csv"
    CsvIO.write_dict_rows(path, PEOPLE_COLUMNS, rows)
    return path


class ShareEvidenceLabelsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = Db(self.root / "deep-context.sqlite")

    def _load(self) -> list:
        return ShareEvidence(self.db, people_csv=_write_people(self.root, self._people)).load()

    def test_a_persons_facts_and_labels_resolve_through_the_parent(self) -> None:
        self._people = [{"id": "person-a", "public_identifier": "jordan-bravo", "full_name": "Jordan Bravo"}]
        seed_identity(
            self.db,
            parent_id="parent-a",
            person_id="person-a",
            row_key="jordan-a",
            name="Jordan Bravo",
            machine_worth="yes",
            public_identifier="jordan-bravo",
            labels={"relationship_kind": "colleague"},
        )
        person = self._load()[0]
        self.assertFalse(person.linkedin_only)
        self.assertEqual(person.network_worth, "yes")
        self.assertEqual(person.facts["labels"]["relationship_kind"], "colleague")

    def test_a_realized_row_inherits_context_through_a_superseded_identity(self) -> None:
        self._people = [{
            "id": "realized-person",
            "public_identifier": "jordan-bravo",
            "full_name": "Jordan Bravo",
            "superseded_person_ids": json.dumps(["person-a"]),
        }]
        seed_identity(
            self.db,
            parent_id="parent-a",
            person_id="person-a",
            row_key="jordan-a",
            name="Jordan Bravo",
            machine_worth="yes",
            public_identifier="jordan-bravo",
            labels={"relationship_kind": "service_provider"},
        )
        person = self._load()[0]
        self.assertFalse(person.linkedin_only)
        self.assertEqual(person.facts["labels"]["relationship_kind"], "service_provider")

    def test_parent_owned_facts_remain_readable(self) -> None:
        self._people = [{"id": "person-a", "public_identifier": "jordan-bravo", "full_name": "Jordan Bravo"}]
        seed_identity(
            self.db,
            parent_id="parent-a",
            person_id="person-a",
            row_key="jordan-a",
            name="Jordan Bravo",
            machine_worth="yes",
            public_identifier="jordan-bravo",
            labels={"relationship_kind": "parent"},
        )
        person = self._load()[0]
        self.assertEqual(person.facts["labels"]["relationship_kind"], "parent")
        self.assertEqual(person.network_worth, "yes")

    def test_human_worth_does_not_change_which_machine_labels_are_read(self) -> None:
        self._people = [{"id": "person-a", "public_identifier": "jordan-bravo", "full_name": "Jordan Bravo"}]
        seed_identity(
            self.db,
            parent_id="parent-a",
            person_id="person-a",
            row_key="jordan-a",
            name="Jordan Bravo",
            machine_worth="yes",
            public_identifier="jordan-bravo",
            human_worth="no",
            labels={"relationship_kind": "colleague"},
        )
        person = self._load()[0]
        self.assertEqual(person.network_worth, "no")
        self.assertEqual(person.facts["labels"]["relationship_kind"], "colleague")

    def test_a_child_owned_facts_row_is_not_the_shared_facts(self) -> None:
        # Synthesize writes facts on the parent; a facts row owned by the child
        # alone (no saved labels) leaves the person LinkedIn-only.
        self._people = [{"id": "person-a", "public_identifier": "jordan-bravo", "full_name": "Jordan Bravo"}]
        seed_identity(
            self.db,
            parent_id="parent-a",
            person_id="person-a",
            row_key="jordan-a",
            name="Jordan Bravo",
            machine_worth="yes",
            public_identifier="jordan-bravo",
        )
        person = self._load()[0]
        self.assertIsNone(person.facts)
        self.assertTrue(person.linkedin_only)
        self.assertEqual(person.network_worth, "maybe")


if __name__ == "__main__":
    unittest.main()
