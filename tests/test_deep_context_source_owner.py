"""Only explicit owner endpoints classify original source contacts as owner."""

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.context_queries import collection_sources
from packs.ingestion.primitives.deep_context.db.merge_queries import merge_people
from packs.ingestion.primitives.deep_context.db.models import PersonIdentifierRow, PersonIdentifiersProjection, PersonRow
from packs.ingestion.primitives.deep_context.db.projectors import project_person_fact
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import _imported_people, project_imported_people
from packs.ingestion.primitives.deep_context.merge_candidates.build_parents import _parent_plans
from packs.ingestion.primitives.deep_context.shared.build_owner import BuildOwner
from packs.ingestion.primitives.deep_context.synthesis.normalization import normalize_parent_cache
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.shared.csv_io import CsvIO


class SourceOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Db(self.root / 'deep-context.sqlite')
        self.owner_path = self.root / 'owner.json'
        self.owner_path.write_text(json.dumps({'name': 'Jordan Bravo',
            'emails': [' Owner@Example.com ', 'second@example.com'], 'phones': ['+1 (555) 010-0123']}))

    def contacts(self):
        return (
            PeopleRow(id='candidate:email:owner@example.com', full_name='Jordan Bravo',
                      primary_email='owner@example.com', source_channels='gmail_msgvault'),
            PeopleRow(id='candidate:email:second@example.com', full_name='Jordan B.',
                      primary_email='second@example.com', source_channels='gmail_msgvault'),
            PeopleRow(id='candidate:phone:+15550100123', full_name='Mailbox Alias',
                      primary_phone='+15550100123', source_channels='imessage'),
            PeopleRow(id='candidate:email:other@example.com', full_name='Jordan Bravo',
                      primary_email='other@example.com', source_channels='gmail_msgvault'),
        )

    def flags(self):
        return {row.person_id: row.is_owner for row in queries.people(self.db)}

    def test_owner_projected_after_contacts_marks_only_exact_endpoints_and_preserves_evidence(self):
        contacts = self.contacts()
        project_imported_people(self.db, _imported_people(contacts))
        for contact in contacts:
            path = self.root / f'{contact.id}.jsonl'
            path.write_text(json.dumps({'facts': {'canonical_name': contact.full_name,
                'is_owner': True, 'owned_identifiers': {'emails': ['owner@example.com']}}}) + '\n')
            project_person_fact(self.db, path, contact.id)
        before = {table: [tuple(row) for row in self.db.query(f'SELECT * FROM {table}')]
                  for table in ('facts', 'artifacts', 'person_identifiers')}
        owner = BuildOwner(out=self.owner_path, db=self.db)
        self.assertEqual(owner.run().status, 'exists')
        self.assertEqual(self.flags(), {contact.id: contact.id != contacts[-1].id for contact in contacts})
        self.assertEqual({table: [tuple(row) for row in self.db.query(f'SELECT * FROM {table}')]
                          for table in before}, before)
        self.assertEqual(owner.run().status, 'exists')
        self.assertEqual([row.person_id for row in collection_sources(self.db)], [contacts[-1].id])
        normalize_parent_cache(self.db, raw_dir=self.root / 'raw', facts_dir=self.root / 'facts')
        self.assertEqual({person.person_id for person in merge_people(self.db)}, {contacts[-1].id})
        self.assertEqual({child.person_id for plan in _parent_plans(self.db)[0] for child in plan.confirmed},
                         {contacts[-1].id})

    def test_later_ensure_import_marks_owner_contacts_and_preserves_existing_flags(self):
        BuildOwner(out=self.owner_path, db=self.db).run()
        contacts = self.contacts()
        path = self.root / 'people.csv'
        CsvIO.write_dict_rows(path, PEOPLE_SCHEMA_COLUMNS, [row.to_row() for row in contacts])
        EnsureParents(db=self.db, people_csv=path).run()
        self.assertEqual(self.flags(), {contact.id: contact.id != contacts[-1].id for contact in contacts})
        person = queries.people(self.db, person_id=contacts[-1].id)[0]
        self.db.project_rows((PersonRow(person.person_id, person.parent_id, is_owner=True),))
        EnsureParents(db=self.db, people_csv=path).run()
        self.assertTrue(self.flags()[person.person_id])

    def test_owner_contact_does_not_classify_other_child_of_same_parent(self):
        contacts = (self.contacts()[0], self.contacts()[-1])
        project_imported_people(self.db, _imported_people(contacts))
        people = {row.person_id: row for row in queries.people(self.db)}
        self.db.merge_parents(people[contacts[0].id].parent_id, people[contacts[1].id].parent_id)
        BuildOwner(out=self.owner_path, db=self.db).run()
        self.assertEqual(self.flags(), {contacts[0].id: True, contacts[1].id: False})
        self.assertEqual([row.person_id for row in collection_sources(self.db)], [contacts[1].id])

    def test_model_claim_and_copied_identifier_without_source_match_are_not_owner(self):
        contact = self.contacts()[-1]
        project_imported_people(self.db, _imported_people((contact,)))
        self.db.project_rows((PersonIdentifiersProjection(contact.id, (
            *queries.identifiers(self.db),
            PersonIdentifierRow(contact.id, 'email', 'owner@example.com', 'owner@example.com'),
        )),))
        path = self.root / 'facts.jsonl'
        path.write_text(json.dumps({'facts': {'canonical_name': 'Jordan Bravo', 'is_owner': True,
            'owned_identifiers': {'emails': ['owner@example.com'], 'phones': ['+15550100123']}}}) + '\n')
        project_person_fact(self.db, path, contact.id)
        BuildOwner(out=self.owner_path, db=self.db).run()
        self.assertFalse(self.flags()[contact.id])
