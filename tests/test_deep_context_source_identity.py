"""Source identity survives old lookup aliases and profile review."""

import tempfile
import json
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.identity_queries import links
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import project_imported_people, _imported_people
from packs.ingestion.primitives.deep_context.realize.export_people import ExportPeople
from packs.ingestion.primitives.imports.merge_people import PeopleMerge
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.shared.csv_io import CsvIO


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")

    def tearDown(self):
        self.temp.cleanup()

    def contacts(self):
        return tuple(PeopleRow(id=person_id, full_name="Jordan Bravo", primary_email=email,
                               source_channels="gmail_msgvault", public_identifier="jordan-bravo",
                               linkedin_url="https://www.linkedin.com/in/jordan-bravo")
                     for person_id, email in (("candidate:email:first@example.test", "first@example.test"),
                                               ("candidate:email:second@example.test", "second@example.test")))

    def test_manifest_sources_keep_original_ids_separate_on_cold_and_warm_import(self):
        source = self.root / "source.csv"
        CsvIO.write_dict_rows(source, PEOPLE_SCHEMA_COLUMNS, [row.to_row() for row in self.contacts()])
        merge = PeopleMerge(inputs=[source], output_dir=self.root / "merged")
        merge.run()
        for _ in range(2):
            EnsureParents(db=self.db, people_csv=merge.people_csv).run()
            people = queries.people(self.db)
            self.assertEqual({row.person_id for row in people}, {row.id for row in self.contacts()})
            self.assertEqual(len({row.parent_id for row in people}), 2)
            self.assertEqual({row.id for row in queries.imported_people(self.db)}, {row.id for row in self.contacts()})
            self.assertEqual(len(links(self.db)), 2)

    def test_old_manifest_extra_lookup_fields_still_reads_sources(self):
        source = self.root / "source.csv"
        CsvIO.write_dict_rows(source, PEOPLE_SCHEMA_COLUMNS, [row.to_row() for row in self.contacts()])
        merge = PeopleMerge(inputs=[source], output_dir=self.root / "merged")
        merge.run()
        manifest = merge.people_csv.parent / "manifest.json"
        payload = json.loads(manifest.read_text())
        payload["input"]["directory_csv"] = "old-directory.csv"
        payload["stats"]["profiles_filled"] = 1
        payload["stats"]["profiles_missing"] = 0
        manifest.write_text(json.dumps(payload))
        EnsureParents(db=self.db, people_csv=merge.people_csv).run()
        self.assertEqual({row.person_id for row in queries.people(self.db)}, {row.id for row in self.contacts()})

    def test_historical_gmail_profile_id_restores_exact_primary_contact_key(self):
        source = self.root / "gmail.csv"
        original = self.root / "gmail_contacts_aggregated.csv"
        CsvIO.write_dict_rows(original, ["email", "display_name"],
                              [{"email": "first@example.test", "display_name": "Jordan Original"}])
        row = self.contacts()[0].model_copy(update={"id": "legacy-profile-id",
            "all_emails": '["first@example.test","other@example.test"]',
            "primary_phone": "+15550100", "headline": "Wrong lookup title",
            "full_name": "Wrong Profile Name", "source_artifacts": f'["{original}"]',
            "superseded_person_ids": '["old-profile-alias"]'})
        project_imported_people(self.db, _imported_people((row,)))
        original_parent = queries.people(self.db)[0].parent_id
        CsvIO.write_dict_rows(source, PEOPLE_SCHEMA_COLUMNS, [row.to_row()])
        merge = PeopleMerge(inputs=[source], output_dir=self.root / "merged")
        merge.run()
        EnsureParents(db=self.db, people_csv=merge.people_csv).run()
        person = next(row for row in queries.people(self.db) if row.person_id == "candidate:email:first@example.test")
        (roster,) = queries.imported_people(self.db)
        self.assertEqual(person.person_id, "candidate:email:first@example.test")
        self.assertEqual(person.parent_id, original_parent)
        self.assertEqual(len(queries.parents(self.db)), 1)
        self.assertEqual(len(queries.people(self.db)), 1)
        self.assertEqual(person.display_name, "Jordan Original")
        self.assertEqual({(row.kind, row.normalized_value) for row in queries.identifiers(self.db)},
                         {("email", "first@example.test")})
        self.assertEqual((roster.headline, roster.superseded_person_ids), ("", ""))
        self.assertEqual(links(self.db)[0].public_identifier, "jordan-bravo")
        self.assertIsNone(next(row.decision_action for row in links(self.db) if row.parent_id == person.parent_id))
        self.assertEqual(CsvIO.read_dict_rows(source)[0]["id"], "legacy-profile-id")

    def test_secondary_email_is_a_separate_contact_only_when_original_source_proves_it(self):
        source = self.root / "gmail.csv"
        original = self.root / "gmail_contacts_aggregated.csv"
        CsvIO.write_dict_rows(original, ["email", "display_name", "total_messages", "last_interaction"], [
            {"email": "first@example.test", "display_name": "Jordan Original", "total_messages": "3"},
            {"email": "second@example.test", "display_name": "Jordan Other", "total_messages": "7"},
        ])
        row = self.contacts()[0].model_copy(update={"id": "legacy-profile-id",
            "all_emails": '["first@example.test","second@example.test","unproven@example.test"]',
            "source_artifacts": f'["{original}"]', "interaction_counts": '{"gmail":999}'})
        CsvIO.write_dict_rows(source, PEOPLE_SCHEMA_COLUMNS, [row.to_row()])
        merge = PeopleMerge(inputs=[source], output_dir=self.root / "merged")
        merge.run()
        EnsureParents(db=self.db, people_csv=merge.people_csv).run()
        roster = {row.id: row for row in queries.imported_people(self.db)}
        self.assertEqual(set(roster), {"candidate:email:first@example.test", "candidate:email:second@example.test"})
        self.assertEqual(roster["candidate:email:second@example.test"].full_name, "Jordan Other")
        self.assertEqual(json.loads(roster["candidate:email:second@example.test"].interaction_counts), {"gmail": 7})
        self.assertEqual(roster["candidate:email:second@example.test"].public_identifier, "")
        self.assertEqual(len({row.parent_id for row in queries.people(self.db)}), 2)
        self.assertEqual(queries.facts(self.db), ())
        ExportPeople(db=self.db, out_dir=merge.people_csv.parent).run()
        EnsureParents(db=self.db, people_csv=merge.people_csv).run()
        self.assertEqual({row.id for row in queries.imported_people(self.db)}, set(roster))

    def test_superseded_aliases_do_not_merge_source_contacts(self):
        project_imported_people(self.db, _imported_people(self.contacts()))
        before = {row.person_id: row.parent_id for row in queries.people(self.db)}
        aggregate = self.contacts()[0].model_copy(update={"id": "old-aggregate",
            "superseded_person_ids": '["candidate:email:first@example.test","historical-person-2"]'})
        project_imported_people(self.db, _imported_people((aggregate,)))
        after = {row.person_id: row.parent_id for row in queries.people(self.db)}
        self.assertEqual({key: after[key] for key in before}, before)
        self.assertEqual(len(set(after.values())), 3)

    def test_live_source_replaces_old_aggregate_identifiers_on_the_same_id(self):
        first, second = self.contacts()
        old = first.model_copy(update={"source_channels": "gmail_msgvault,imessage", "full_name": "Wrong Profile Name", "all_emails": '["first@example.test","second@example.test"]',
            "superseded_person_ids": '["candidate:email:second@example.test"]'})
        project_imported_people(self.db, _imported_people((old,)))
        source = self.root / "source.csv"
        CsvIO.write_dict_rows(source, PEOPLE_SCHEMA_COLUMNS, [row.to_row() for row in (first, second)])
        merge = PeopleMerge(inputs=[source], output_dir=self.root / "merged")
        merge.run()
        EnsureParents(db=self.db, people_csv=merge.people_csv).run()
        self.assertEqual({row.normalized_value for row in queries.identifiers(self.db) if row.person_id == first.id},
                         {"first@example.test"})
        self.assertEqual(next(row.display_name for row in queries.people(self.db) if row.person_id == first.id),
                         first.full_name)
        roster = {row.id: row for row in queries.imported_people(self.db)}
        self.assertEqual(roster[first.id].all_emails, first.all_emails)
        self.assertEqual(roster[first.id].superseded_person_ids, first.superseded_person_ids)
        self.assertEqual({row.source for row in queries.sources(self.db, person_id=first.id)}, {"gmail_msgvault"})

    def test_restored_gmail_keys_remove_copied_endpoints_from_old_source_ids(self):
        first, second = self.contacts()
        first = first.model_copy(update={"id": "source-a"})
        second = second.model_copy(update={"id": "source-b"})
        old = first.model_copy(update={"all_emails": '["first@example.test","second@example.test"]',
            "superseded_person_ids": '["source-b"]'})
        project_imported_people(self.db, _imported_people((old,)))
        source = self.root / "source.csv"
        CsvIO.write_dict_rows(source, PEOPLE_SCHEMA_COLUMNS, [row.to_row() for row in (first, second)])
        merge = PeopleMerge(inputs=[source], output_dir=self.root / "merged")
        merge.run()
        EnsureParents(db=self.db, people_csv=merge.people_csv).run()
        owned = {(row.person_id, row.normalized_value) for row in queries.identifiers(self.db)}
        self.assertEqual(owned, {("candidate:email:first@example.test", "first@example.test"),
                                 ("candidate:email:second@example.test", "second@example.test")})
        self.assertEqual({row.id for row in queries.imported_people(self.db)},
                         {"candidate:email:first@example.test", "candidate:email:second@example.test"})
        ExportPeople(db=self.db, out_dir=merge.people_csv.parent).run()
        EnsureParents(db=self.db, people_csv=merge.people_csv).run()
        self.assertEqual({(row.person_id, row.normalized_value) for row in queries.identifiers(self.db)}, owned)


    def test_judged_family_export_rerun_keeps_original_contact_ownership(self):
        source = self.root / "source.csv"
        first, second = self.contacts()
        CsvIO.write_dict_rows(source, PEOPLE_SCHEMA_COLUMNS, [row.to_row() for row in (first, second)])
        merge = PeopleMerge(inputs=[source], output_dir=self.root / "merged")
        merge.run()
        EnsureParents(db=self.db, people_csv=merge.people_csv).run()
        parents = {row.person_id: row.parent_id for row in queries.people(self.db)}
        self.db.merge_parents(parents[first.id], parents[second.id])
        owned = {(row.person_id, row.normalized_value) for row in queries.identifiers(self.db)}
        result = ExportPeople(db=self.db, out_dir=merge.people_csv.parent).run()
        self.assertEqual(result["rows"], 1)
        EnsureParents(db=self.db, people_csv=merge.people_csv).run()
        self.assertEqual({(row.person_id, row.normalized_value) for row in queries.identifiers(self.db)}, owned)
        self.assertEqual({row.id for row in queries.imported_people(self.db)}, {first.id, second.id})

    def test_profile_approval_does_not_expand_to_another_parent(self):
        project_imported_people(self.db, _imported_people(self.contacts()[:1]))
        self.db.decide_identity("jordan-bravo", "verify", approved="yes")
        project_imported_people(self.db, _imported_people(self.contacts()[1:]))
        self.assertEqual(len({row.parent_id for row in queries.people(self.db)}), 2)

    def test_export_does_not_merge_two_parents_with_same_unreviewed_profile(self):
        project_imported_people(self.db, _imported_people(self.contacts()))
        before = {row.person_id: row.parent_id for row in queries.people(self.db)}
        result = ExportPeople(db=self.db, out_dir=self.root / "export").run()
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["parents_merged"], 0)
        self.assertEqual({row.person_id: row.parent_id for row in queries.people(self.db)}, before)
        self.assertEqual({row.id for row in queries.imported_people(self.db)}, set(before))
        self.assertFalse(any(row.public_identifier for row in queries.imported_people(self.db)))

    def test_retarget_onto_same_profile_does_not_merge_another_parent(self):
        first, second = self.contacts()
        first = first.model_copy(update={"public_identifier": "jordan-old", "linkedin_url": "https://www.linkedin.com/in/jordan-old"})
        project_imported_people(self.db, _imported_people((first, second)))
        self.db.decide_identity("jordan-old", "retarget", approved="yes",
                                replacement_url="https://www.linkedin.com/in/jordan-bravo",
                                replacement_public_identifier="jordan-bravo")
        self.db.decide_identity("jordan-bravo", "verify", approved="yes")
        result = ExportPeople(db=self.db, out_dir=self.root / "export").run()
        self.assertEqual((result["rows"], result["parents_merged"]), (2, 0))
        self.assertEqual({row.id for row in queries.imported_people(self.db)}, {first.id, second.id})
        self.assertEqual({row.public_identifier for row in queries.imported_people(self.db)}, {"jordan-bravo"})
