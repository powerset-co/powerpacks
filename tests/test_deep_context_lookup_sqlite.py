"""Person lookup reads canonical SQLite and projected dossier paths only."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    FactRow,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
    PersonSourceRow,
    PersonSourcesProjection,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.people_views import person_detail, person_lookup
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import ParentLookupRow
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import (
    ImportedPerson,
    project_imported_people,
)
from packs.ingestion.primitives.deep_context.merge_candidates.build_parents import BuildParents
from packs.ingestion.primitives.deep_context.shared.lookup_person import PersonLookup, main
from packs.ingestion.primitives.pipeline.contract import PeopleRow


class PersonLookupSqliteTest(unittest.TestCase):
    def test_unavailable_exact_name_does_not_hide_available_partial_name_dossiers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / 'deep-context.sqlite')
            db.project_rows((
                ParentRow('unavailable-parent', 'parent-worth:unavailable-parent', 'Jordan Bravo', 'unavailable'),
                PersonRow('unavailable-person', 'unavailable-parent', 'unavailable-child', 'unavailable', 'Jordan Bravo'),
                ParentRow('available-parent', 'parent-worth:available-parent', 'Jordan Bravo Jr.', 'available'),
                PersonRow('available-person', 'available-parent', 'available-child', 'available', 'Jordan Bravo Jr.'),
                ArtifactRow('dossier:available-parent', 'dossier', 'available-parent', 'available.md', 'fixture', 'projected',
                            payload_json=json.dumps({'body': '# Jordan Bravo Jr.\nKnown context.'})),
            ))
            result = PersonLookup(db=db, name='Jordan Bravo').run()
            self.assertEqual(result.status, 'found')
            self.assertEqual([match.slug for match in result.matches], ['available'])
            db.project_rows((
                ParentRow('other-parent', 'parent-worth:other-parent', 'Jordan Bravo Sr.', 'other'),
                PersonRow('other-person', 'other-parent', 'other-child', 'other', 'Jordan Bravo Sr.'),
                ArtifactRow('dossier:other-parent', 'dossier', 'other-parent', 'other.md', 'fixture', 'projected',
                            payload_json=json.dumps({'body': '# Jordan Bravo Sr.\nDistinct context.'})),
            ))
            self.assertEqual({match.slug for match in PersonLookup(db=db, name='Jordan Bravo').run().matches}, {'available', 'other'})
            self.assertEqual(PersonLookup(db=db, email='unavailable@example.com').run().status, 'no_match')

    def test_build_parents_dossiers_reach_lookup_cli_and_detail(self) -> None:
        for children in (1, 2):
            with self.subTest(children=children), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                db = Db(root / "deep-context.sqlite")
                facts_json = json.dumps({
                    "canonical_name": "Casey Delta", "title": "Engineer",
                    "topics": ["Distributed systems"],
                    "relationship_to_owner": "Worked together on Example launch.",
                })
                db.project_rows((
                    ParentRow("parent-a", "parent-worth:parent-a", "Casey Delta", "casey-parent"),
                    *(PersonRow(f"person-{i}", "parent-a", f"jordan-{i}", "casey-parent", "Jordan Bravo")
                      for i in range(children)),
                    ArtifactRow("facts:parent-a", "facts", "parent-a", str(root / "facts.jsonl"),
                                "facts-fingerprint", "projected"),
                    FactRow("parent-a", "parent-a", "facts:parent-a", facts_json=facts_json),
                ))
                db.replace_imported_people(tuple(
                    PeopleRow(id=f"person-{i}", full_name="Casey Delta") for i in range(children)
                ))
                built = BuildParents(db=db, parents_dir=root / "parents").execute()
                self.assertEqual(built.parents_changed, 1)
                dossier = db.query("SELECT path, payload_json FROM artifacts WHERE kind='dossier'")[0]
                body = json.loads(dossier["payload_json"])["body"]
                self.assertIn("Distributed systems", body)
                self.assertIn("Worked together on Example launch.", body)
                self.assertNotIn("Full context in [[", body)
                if children == 1:
                    self.assertNotIn("LLM-judged same person", body)
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["--name", "Jordan", "--db", str(db.db_path)]), 0)
                self.assertEqual(output.getvalue(), body + "\n")
                detail = person_detail(db, "parent-a")
                self.assertIsNotNone(detail)
                self.assertEqual(detail.dossier_body, body)
                self.assertEqual(detail.dossier_path, dossier["path"])

    def test_latest_canonical_parent_dossier_is_used_by_lookup_and_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-a", "parent-worth:parent-a", "Casey Delta", "casey-parent"),
                PersonRow("person-a", "parent-a", "jordan-child", "casey-parent", "Jordan Bravo"),
                ArtifactRow("facts:parent-a", "facts", "parent-a", "facts.jsonl", "facts", "projected"),
                FactRow("parent-a", "parent-a", "facts:parent-a", facts_json='{"canonical_name":"Casey Delta"}'),
                ArtifactRow("dossier-other:parent-a", "dossier", "parent-a", "other.md", "other", "projected",
                            payload_json=json.dumps({"body": "unrelated"}), projected_at="2099-01-01T00:00:00Z"),
            ))
            for composed_at, parent_at, expected in (
                ("2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", "parent"),
                ("2026-10-02T00:00:00Z", "2026-10-02T00:00:00Z", "composed"),
                ("2026-10-03T00:00:00Z", "2026-10-02T00:00:00Z", "composed"),
            ):
                with self.subTest(expected=expected, composed_at=composed_at):
                    db.project_rows(tuple(
                        ArtifactRow(key, "dossier", "parent-a", f"{body}.md", body, "projected",
                                    input_fingerprint=timestamp,
                                    payload_json=json.dumps({"body": body}), projected_at=timestamp)
                        for key, body, timestamp in (
                            ("dossier:parent-a", "composed", composed_at),
                            ("dossier-parent:parent-a", "parent", parent_at),
                        )
                    ))
                    matches = person_lookup(db, name="Casey Delta")
                    self.assertEqual(len(matches), 1)
                    self.assertEqual(matches[0].dossier_body, expected)
                    self.assertEqual(person_detail(db, "parent-a").dossier_body, expected)

    def test_child_name_without_dossier_returns_parent_identity_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            parent_path = root / "casey-parent.md"
            parent_body = "# Casey Delta\nParent dossier.\n"
            parent_path.write_text(parent_body, encoding="utf-8")
            db.project_rows(
                (
                    ParentRow("parent-a", "parent-worth:parent-a", "Casey Delta", "casey-parent"),
                    PersonRow("person-a", "parent-a", "jordan-child", "casey-parent", "Jordan A. Bravo"),
                    PersonRow("person-b", "parent-a", "jordy-child", "casey-parent", "Jordy Bravo"),
                    PersonIdentifiersProjection(
                        "person-a",
                        (PersonIdentifierRow("person-a", "email", "jordan@example.com"),),
                    ),
                    ArtifactRow(
                        "dossier:parent-a", "dossier", "parent-a", str(parent_path),
                        hashlib.sha256(parent_path.read_bytes()).hexdigest(), "projected",
                        payload_json=json.dumps({"body": parent_body}),
                    ),
                )
            )

            for query in (
                {"name": "Jordan A. Bravo"},
                {"name": "Jordan"},
                {"name": "Jordy"},
                {"name": "Bravo"},
                {"name": "Casey Delta"},
                {"email": "JORDAN@example.com"},
                {"name": "Bravo", "email": "jordan@example.com"},
            ):
                with self.subTest(query=query):
                    rows = person_lookup(db, **query)
                    self.assertEqual(len(rows), 1)
                    parent = rows[0]
                    self.assertIsInstance(parent, ParentLookupRow)
                    self.assertEqual(parent.name, "Casey Delta")
                    self.assertEqual(parent.full_name, "Casey Delta")
                    self.assertEqual(parent.slug, "casey-parent")
                    self.assertEqual(parent.children, ("jordan-child", "jordy-child"))
                    result = PersonLookup(db=db, **query).run()
                    self.assertEqual(result.status, "found")
                    self.assertEqual(len(result.matches), 1)
                    self.assertEqual(result.matches[0].as_dict(), {"slug": "casey-parent"})
                    self.assertEqual(result.matches[0].dossier_body, parent_body)

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--name", "Jordan", "--db", str(root / "deep-context.sqlite")]), 0)
            self.assertEqual(output.getvalue(), parent_body + "\n")

    def test_phone_lookup_matches_e164_storage_by_digit_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            project_imported_people(
                db,
                (
                    ImportedPerson(
                        "person-phone",
                        "Jordan Bravo",
                        (),
                        ("+1 415-555-0100",),
                        ("imessage",),
                        (),
                        PeopleRow(id="person-phone", full_name="Jordan Bravo",
                                  primary_phone="+1 415-555-0100", source_channels="imessage"),
                    ),
                ),
            )
            snapshot = canonical_snapshot(db)
            person = snapshot.people[0]
            child_path = root / "child.md"
            parent_path = root / "parent.md"
            child_path.write_text("# Child dossier\n", encoding="utf-8")
            parent_path.write_text("# Parent dossier\n", encoding="utf-8")
            db.project_rows(
                (
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
                )
            )

            stored_phone = db.query(
                "SELECT normalized_value FROM person_identifiers WHERE person_id=? AND kind='phone'",
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
            db.project_rows(
                (
                    ParentRow("parent-a", "parent-worth:parent-a", "Jordan Bravo", "jordan-parent"),
                    PersonRow("person-a", "parent-a", "jordan-child", "jordan-parent", "Jordan A. Bravo"),
                    PersonIdentifiersProjection(
                        "person-a",
                        (
                            PersonIdentifierRow("person-a", "email", "jordan@example.com", "Jordan@Example.com"),
                            PersonIdentifierRow("person-a", "phone", "+14155550100", "+1 415 555 0100"),
                        ),
                    ),
                    PersonSourcesProjection("person-a", (PersonSourceRow("person-a", "imessage"),)),
                    ArtifactRow(
                        "dossier-person:person-a",
                        "dossier",
                        "parent-a",
                        str(child_path),
                        hashlib.sha256(child_path.read_bytes()).hexdigest(),
                        "projected",
                        person_id="person-a",
                        payload_json=json.dumps(child_payload),
                    ),
                    ArtifactRow(
                        "dossier:parent-a",
                        "dossier",
                        "parent-a",
                        str(parent_path),
                        hashlib.sha256(parent_path.read_bytes()).hexdigest(),
                        "projected",
                        payload_json=json.dumps(parent_payload),
                    ),
                )
            )
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
            self.assertEqual(
                list(matches[0].as_dict()),
                [
                    "person_id",
                    "name",
                    "path",
                    "headline",
                    "full_name",
                    "emails",
                    "phones",
                    "slug",
                ],
            )
            self.assertEqual(matches[1].as_dict(), {"slug": "jordan-parent"})
            self.assertEqual(matches[1].dossier_body, "# Parent dossier\n")
            dossiers = {row.slug: row for row in canonical_snapshot(db).dossiers}
            self.assertEqual(dossiers["jordan-child"].emails, ("Jordan@Example.com",))
            self.assertEqual(dossiers["jordan-child"].phones, ("+1 415 555 0100",))
            self.assertEqual(dossiers["jordan-child"].source_channels, ("imessage",))
            self.assertEqual(dossiers["jordan-parent"].children, ("jordan-child",))
            self.assertEqual(dossiers["jordan-parent"].source_channels, ("imessage",))

    def test_missing_database_exits_without_creating_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(SystemExit, "database is missing"):
                main(["--name", "Jordan", "--db", str(root / "missing.sqlite")])
            self.assertFalse((root / "missing.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
