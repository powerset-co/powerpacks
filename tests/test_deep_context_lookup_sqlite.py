"""Person lookup reads canonical SQLite and projected dossier paths only."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
    PersonSourceRow,
    PersonSourcesProjection,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.imported_people import (
    ImportedPerson,
    project_imported_people,
)
from packs.ingestion.primitives.deep_context.lookup_person import PersonLookup


class PersonLookupSqliteTest(unittest.TestCase):
    def test_phone_lookup_matches_e164_storage_by_digit_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            project_imported_people(db, (
                ImportedPerson(
                    "person-phone",
                    "Jordan Bravo",
                    (),
                    ("+1 415-555-0100",),
                    ("imessage",),
                    (),
                ),
            ))
            snapshot = canonical_snapshot(db)
            person = snapshot.people[0]
            child_path = root / "child.md"
            parent_path = root / "parent.md"
            child_path.write_text("# Child dossier\n", encoding="utf-8")
            parent_path.write_text("# Parent dossier\n", encoding="utf-8")
            db.project_rows((
                ArtifactRow(
                    f"dossier-person:{person.person_id}",
                    "dossier",
                    person.parent_id,
                    str(child_path),
                    hashlib.sha256(child_path.read_bytes()).hexdigest(),
                    "projected",
                    person_id=person.person_id,
                    payload_json=json.dumps({"body": "# Child dossier\n"}),
                ),
                ArtifactRow(
                    f"dossier:{person.parent_id}",
                    "dossier",
                    person.parent_id,
                    str(parent_path),
                    hashlib.sha256(parent_path.read_bytes()).hexdigest(),
                    "projected",
                    payload_json=json.dumps({"body": "# Parent dossier\n"}),
                ),
            ))

            stored_phone = db.query(
                "SELECT normalized_value FROM person_identifiers "
                "WHERE person_id=? AND kind='phone'",
                (person.person_id,),
            )
            self.assertEqual(stored_phone[0]["normalized_value"], "+14155550100")
            for phone in (
                "415-555-0100",
                "(415) 555-0100",
                "4155550100",
                "+14155550100",
            ):
                with self.subTest(phone=phone):
                    result = PersonLookup(db=db, phone=phone).run()
                    self.assertEqual(result.status, "found")
                    self.assertEqual(len(result.matches), 2)

    def test_phone_email_and_name_keep_the_existing_match_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_path = root / "jordan-child.md"
            parent_path = root / "jordan-parent.md"
            child_path.write_text("# Child dossier\n", encoding="utf-8")
            parent_path.write_text("# Parent dossier\n", encoding="utf-8")
            child_payload = {
                "person_id": "person-a",
                "name": "Jordan Bravo",
                "full_name": "Jordan A. Bravo",
                "path": "dossiers/jordan-child.md",
                "headline": "Engineer",
                "emails": ["stale@example.com"],
                "phones": ["+1 555 555 9999"],
                "body": "# Child dossier\n",
                "source_channels": ["stale"],
            }
            parent_payload = {
                "parent_id": "parent-a",
                "name": "Jordan Bravo",
                "path": "parents/jordan-parent.md",
                "children": ["stale-child"],
                "emails": ["stale@example.com"],
                "phones": ["+1 555 555 9999"],
                "body": "# Parent dossier\n",
                "source_channels": ["stale"],
            }
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-a", "parent-worth:parent-a", "Jordan Bravo", "jordan-parent"),
                PersonRow("person-a", "parent-a", "jordan-child", "jordan-parent", "Jordan A. Bravo"),
                PersonIdentifiersProjection("person-a", (
                    PersonIdentifierRow("person-a", "email", "jordan@example.com", "Jordan@Example.com"),
                    PersonIdentifierRow("person-a", "phone", "+14155550100", "+1 415 555 0100"),
                )),
                PersonSourcesProjection("person-a", (
                    PersonSourceRow("person-a", "imessage"),
                )),
                ArtifactRow(
                    "dossier-person:person-a", "dossier", "parent-a", str(child_path),
                    hashlib.sha256(child_path.read_bytes()).hexdigest(), "projected",
                    person_id="person-a", payload_json=json.dumps(child_payload),
                ),
                ArtifactRow(
                    "dossier:parent-a", "dossier", "parent-a", str(parent_path),
                    hashlib.sha256(parent_path.read_bytes()).hexdigest(), "projected",
                    payload_json=json.dumps(parent_payload),
                ),
            ))
            child_path.unlink()
            parent_path.unlink()

            def slugs(**query: str) -> list[str]:
                result = PersonLookup(db=db, **query).run()
                self.assertEqual(result.status, "found")
                return [match.slug for match in result.matches]

            expected = ["jordan-child", "jordan-parent"]
            self.assertEqual(slugs(email="JORDAN@example.com"), expected)
            self.assertEqual(slugs(phone="+1 415-555-0100"), expected)
            self.assertEqual(slugs(name="Jordan A. Bravo"), ["jordan-child"])
            self.assertEqual(slugs(name="Jordan Bravo"), ["jordan-parent"])
            self.assertEqual(slugs(name="Jordan"), expected)
            matches = PersonLookup(db=db, email="jordan@example.com").run().matches
            self.assertEqual(list(matches[0].as_dict()), [
                "person_id", "name", "path", "headline", "full_name",
                "emails", "phones", "slug",
            ])
            self.assertEqual(matches[1].as_dict(), {"slug": "jordan-parent"})
            self.assertEqual(matches[1].dossier_body, "# Parent dossier\n")
            dossiers = {row.slug: row for row in canonical_snapshot(db).dossiers}
            self.assertEqual(dossiers["jordan-child"].emails, ("Jordan@Example.com",))
            self.assertEqual(dossiers["jordan-child"].phones, ("+1 415 555 0100",))
            self.assertEqual(dossiers["jordan-child"].source_channels, ("imessage",))
            self.assertEqual(dossiers["jordan-parent"].children, ("jordan-child",))
            self.assertEqual(dossiers["jordan-parent"].source_channels, ("imessage",))

    def test_missing_database_returns_no_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = PersonLookup(
                name="Jordan", db_path=root / "missing.sqlite",
            ).run()
            self.assertEqual(result.status, "no_index")


if __name__ == "__main__":
    unittest.main()
