"""Replay the shipped Gmail split through fan-in and ensure-parents."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.context_queries import person_histories
from packs.ingestion.primitives.deep_context.db.models import ArtifactRow, LinkRow, MergeVerdictRow
from packs.ingestion.primitives.deep_context.db.projectors import project_person_fact, project_person_source_bundle
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.workflow_views import synthesis_pending
from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import _imported_people, project_imported_people
from packs.ingestion.primitives.deep_context.synthesis.selection import effective_person_bundles
from packs.ingestion.primitives.deep_context.synthesis.prompting import seed_evidence_fingerprint
from packs.ingestion.primitives.imports.merge_people import PeopleMerge
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.shared.csv_io import CsvIO


class GmailRekeyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = Db(self.root / 'deep-context.sqlite')
        self.old = PeopleRow(id='old-jordan', full_name='Jordan Bravo',
                             primary_email='jordan@example.test', source_channels='gmail_msgvault')
        self.new_id = 'candidate:email:jordan@example.test'
        self.project(self.old)
        self.parent = queries.people(self.db)[0].parent_id
        self.facts_path = self.root / 'old-facts.jsonl'
        bundle_path = self.root / 'old-bundle.json'
        bundle_path.write_text(json.dumps({'person_id': self.old.id, 'messages': [
            {'channel': 'gmail', 'at': '2026-01-01', 'direction': 'from_them', 'text': 'Synthetic message'}]}))
        project_person_source_bundle(self.db, bundle_path, self.old.id)
        bundle = effective_person_bundles(self.db)[self.old.id]
        self.facts_path.write_text(json.dumps({'facts': {'canonical_name': 'Jordan Bravo'},
            'input_evidence_fingerprint': seed_evidence_fingerprint(bundle)}) + '\n')
        project_person_fact(self.db, self.facts_path, self.old.id)

    def project(self, *rows):
        project_imported_people(self.db, _imported_people(rows))

    def fan_in(self, *rows):
        source = self.root / 'gmail.csv'
        CsvIO.write_dict_rows(source, PEOPLE_SCHEMA_COLUMNS, [row.to_row() for row in rows or (self.old,)])
        merge = PeopleMerge(inputs=[source], output_dir=self.root / 'merged')
        merge.run()
        return merge.people_csv

    def ensure(self, path):
        EnsureParents(db=self.db, people_csv=path).run()

    def split(self):
        path = self.fan_in()
        with patch('packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents.repair_gmail_contact_keys'):
            self.ensure(path)
        self.assertEqual(len(queries.parents(self.db)), 2)
        self.assertEqual({row.id for row in queries.imported_people(self.db)}, {self.new_id})
        return path

    def assert_repaired(self):
        self.assertEqual([(row.person_id, row.parent_id) for row in queries.people(self.db)],
                         [(self.new_id, self.parent)])
        self.assertEqual(len(queries.parents(self.db)), 1)
        self.assertEqual({row.id for row in queries.imported_people(self.db)}, {self.new_id})
        self.assertEqual({row.person_id for row in queries.identifiers(self.db)}, {self.new_id})
        self.assertEqual(person_histories(self.db)[self.new_id].facts.canonical_name, 'Jordan Bravo')
        self.assertEqual(synthesis_pending(self.db), ())
        self.assertEqual(self.db.query('PRAGMA foreign_key_check'), [])

    def test_first_import_uses_new_key_without_new_parent_or_new_synthesis(self):
        path = self.fan_in()
        before = self.facts_path.read_bytes()
        self.ensure(path)
        self.assert_repaired()
        self.ensure(path)
        self.assert_repaired()
        self.assertEqual(self.facts_path.read_bytes(), before)
        # A later synthesis replaces the existing fact instead of making a duplicate.
        project_person_fact(self.db, self.facts_path, self.new_id)
        self.assertEqual(len(queries.facts(self.db)), 1)

    def test_refreshed_gmail_import_keeps_retained_contact_history(self):
        path = self.fan_in(self.old.model_copy(update={'id': self.new_id}))
        self.ensure(path)
        self.assert_repaired()

    def test_rekey_keeps_parent_when_another_person_has_the_same_child_slug(self):
        other = PeopleRow(id='casey', full_name='Casey Delta', primary_phone='+15550100', source_channels='imessage')
        self.project(other)
        with self.db.transaction() as conn:
            conn.execute("UPDATE people SET child_slug='shared-slug'")
        path = self.fan_in(self.old, other)
        self.ensure(path)
        self.assertEqual(next(row.parent_id for row in queries.people(self.db) if row.person_id == self.new_id), self.parent)
        self.assertEqual(len(queries.parents(self.db)), 2)
        self.assertEqual(synthesis_pending(self.db), ())

    def test_shipped_split_rejoins_empty_shell_and_keeps_paid_rows(self):
        self.db.project_rows((
            LinkRow('jordan-profile', self.parent, 'jordan-profile', 'pub', source='deep-context-reconcile',
                    machine_action='verify', judgment_fingerprint='paid-fingerprint', paid_profile=True),
            ArtifactRow('paid-profile', 'profile', self.parent, '/saved-profile.json', 'unchanged', 'projected',
                        candidate_key='jordan-profile', payload_json='{"saved":true}'),
        ))
        self.db.decide_identity('jordan-profile', 'verify', approved='yes')
        path = self.split()
        link_before = dict(self.db.query("SELECT * FROM links WHERE row_key='jordan-profile'")[0])
        profile_before = dict(self.db.query("SELECT * FROM artifacts WHERE artifact_key='paid-profile'")[0])
        for _ in range(2):
            self.ensure(path)
            self.assert_repaired()
            self.assertEqual(dict(self.db.query("SELECT * FROM links WHERE row_key='jordan-profile'")[0]), link_before)
            self.assertEqual(dict(self.db.query("SELECT * FROM artifacts WHERE artifact_key='paid-profile'")[0]), profile_before)

    def test_paid_candidate_is_not_deleted_as_an_empty_shell(self):
        path = self.split()
        candidate_parent = next(row.parent_id for row in queries.people(self.db) if row.person_id == self.new_id)
        self.db.project_rows((ArtifactRow('paid', 'profile', candidate_parent, '/saved.json', 'saved', 'projected',
                                          person_id=self.new_id, payload_json='{}'),))
        before = queries.people(self.db)
        self.ensure(path)
        self.assertEqual({row.person_id: row.parent_id for row in queries.people(self.db)},
                         {row.person_id: row.parent_id for row in before})
        self.assertIn('paid', {row.artifact_key for row in queries.artifacts(self.db)})

    def test_shared_secondary_address_is_not_a_rekey(self):
        other = PeopleRow(id='candidate:email:shared@example.test', full_name='Casey Delta',
                          primary_email='shared@example.test', source_channels='gmail_msgvault')
        self.project(other)
        other_parent = next(row.parent_id for row in queries.people(self.db) if row.person_id == other.id)
        path = self.fan_in(self.old.model_copy(update={'all_emails': '["jordan@example.test","shared@example.test"]'}), other)
        self.ensure(path)
        actual = {row.person_id: row.parent_id for row in queries.people(self.db)}
        self.assertEqual(actual, {self.new_id: self.parent, other.id: other_parent})

    def test_same_old_id_with_two_primary_addresses_is_not_guessed(self):
        path = self.fan_in(self.old, self.old.model_copy(update={'primary_email': 'other@example.test'}))
        self.ensure(path)
        self.assertEqual(next(row.parent_id for row in queries.people(self.db) if row.person_id == self.old.id), self.parent)
        self.assertEqual(next(row.person_id for row in queries.facts(self.db)), self.old.id)

    def test_id_still_owned_by_linkedin_is_not_renamed_as_a_gmail_contact(self):
        linkedin = self.old.model_copy(update={'source_channels': 'linkedin_csv', 'primary_email': '',
                                               'public_identifier': 'jordan-bravo'})
        path = self.fan_in(self.old, linkedin)
        self.ensure(path)
        self.assertEqual(next(row.parent_id for row in queries.people(self.db) if row.person_id == self.old.id), self.parent)
        self.assertEqual(next(row.person_id for row in queries.facts(self.db)), self.old.id)

    def test_merge_verdict_keeps_its_people_slugs_and_result_when_id_order_changes(self):
        other = PeopleRow(id='middle-casey', full_name='Casey Delta', primary_phone='+15550100', source_channels='imessage')
        self.project(other)
        self.db.project_rows((MergeVerdictRow(other.id, self.old.id, 'casey-slug', 'jordan-slug',
                                              'saved-signature', 'sol', False, .99, False),))
        path = self.fan_in(self.old, other)
        self.ensure(path)
        verdict = queries.merge_verdicts(self.db)[0]
        self.assertEqual((verdict.person_a, verdict.person_b), (self.new_id, other.id))
        self.assertEqual((verdict.slug_a, verdict.slug_b, verdict.signature, verdict.same_person),
                         ('jordan-slug', 'casey-slug', 'saved-signature', False))
        self.assertEqual(self.db.query('PRAGMA foreign_key_check'), [])
