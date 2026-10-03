"""Read-only identity findings preserve uncertainty and original fact ownership."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow, CandidatePeopleProjection, CandidatePersonRow, FactRow,
    LinkRow, MergeVerdictRow, ParentRow, PersonRow, PersonIdentifierRow,
    PersonIdentifiersProjection, PersonSourceRow, PersonSourcesProjection,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import mint_parent_id
from packs.ingestion.primitives.deep_context.db.audit_identity import IdentityAudit, main
from packs.ingestion.primitives.pipeline.contract import PeopleRow


class IdentityAuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'deep-context.sqlite'
        self.db = Db(self.path)
        self.parent = mint_parent_id(('person-a',))
        self.db.project_rows((
            ParentRow(self.parent, self.parent, display_name='Jordan Bravo'),
            PersonRow('person-a', self.parent, display_name='Jordan Bravo'),
            PersonIdentifiersProjection('person-a', (
                PersonIdentifierRow('person-a', 'email', 'jordan@example.com'),)),
            PersonSourcesProjection('person-a', (PersonSourceRow('person-a', 'imessage'),)),
        ))

    def tearDown(self):
        self.temp.cleanup()

    def findings(self):
        return [finding for parent in IdentityAudit(db_path=self.path).run().parents
                for finding in parent.findings]

    def categories(self):
        return {finding.category.value for finding in self.findings()}

    def add_child(self, person='person-b', name='Casey Delta'):
        email = person.removeprefix('candidate:email:')
        if '@' not in email:
            email += '@example.com'
        self.db.project_rows((
            PersonRow(person, self.parent, display_name=name),
            PersonIdentifiersProjection(person, (
                PersonIdentifierRow(person, 'email', email),)),
            PersonSourcesProjection(person, (PersonSourceRow(person, 'imessage'),)),
        ))

    def fact(self, subject, *, person=None, at='2026-01-01T00:00:00Z'):
        key = 'facts:' + subject
        self.db.project_rows((
            ArtifactRow(key, 'facts', self.parent, '/synthetic/' + subject,
                        'fingerprint', 'projected', person_id=person,
                        payload_json=json.dumps({'updated_at': at}), projected_at=at),
            FactRow(subject, self.parent, key, person_id=person,
                    facts_json=json.dumps({'canonical_name': 'Jordan Bravo'}), projected_at=at),
        ))

    def verdict(self, a, b, *, same=True, accepted=True, at='2026-02-01T00:00:00Z'):
        self.db.replace_merge_verdicts((
            MergeVerdictRow(a, b, a, b, 'signature', 'llm', same, .9,
                            True, 'synthetic judgment', accepted, at),
        ))

    def test_missing_and_ambiguous_link_owners(self):
        self.add_child()
        self.db.project_rows((
            LinkRow('missing', self.parent, 'missing', 'pub', source='legacy-migration'),
            LinkRow('ambiguous', self.parent, 'ambiguous', 'pub', source='legacy-migration'),
            CandidatePeopleProjection('ambiguous', (
                CandidatePersonRow('ambiguous', 'person-a', self.parent),
                CandidatePersonRow('ambiguous', 'person-b', self.parent),
            )),
        ))
        self.assertTrue({'missing_link_owner', 'ambiguous_link_owner'} <= self.categories())

    def test_corrupt_candidate_fact_and_artifact_ownership(self):
        self.fact('person-a', person='person-a')
        self.db.project_rows((
            ParentRow('other', 'other'),
            LinkRow('candidate', self.parent, 'candidate', 'pub', source='legacy-migration'),
            CandidatePeopleProjection('candidate', (
                CandidatePersonRow('candidate', 'person-a', self.parent),
            )),
        ))
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE candidate_people SET parent_id='other'")
            conn.execute("UPDATE artifacts SET parent_id='other'")
        structural = {finding.category.value for finding in self.findings()
                      if finding.classification.value == 'structural_defect'}
        self.assertTrue({'candidate_owner_mismatch', 'fact_owner_mismatch',
                         'artifact_owner_mismatch'} <= structural)

    def test_negative_verdict_conflicts_with_transitive_accepted_merge(self):
        self.add_child()
        self.add_child('person-c', 'Taylor Echo')
        self.verdict('person-a', 'person-b')
        self.verdict('person-b', 'person-c')
        self.verdict('person-a', 'person-c', same=False, accepted=False)
        conflict = next(f for f in self.findings()
                        if f.category.value == 'accepted_merge_conflicts_with_negative')
        self.assertEqual(conflict.classification.value, 'needs_identity_review')
        self.assertEqual(conflict.child_ids, ('person-a', 'person-c'))

    def test_multiple_contacts_need_independent_facts_with_only_one_profile(self):
        self.add_child()
        self.db.project_rows((LinkRow('profile', self.parent, 'profile', 'pub',
                                     source='legacy-migration'),))
        self.assertIn('missing_contact_facts', self.categories())
        self.fact(self.parent)
        self.assertIn('mixed_contact_facts', self.categories())
        self.assertNotIn('missing_contact_facts', self.categories())

    def test_person_owned_facts_cover_each_child(self):
        self.add_child(name='Jordan Bravo')
        self.fact('person-a', person='person-a')
        self.fact('person-b', person='person-b')
        self.assertFalse({'missing_contact_facts', 'mixed_contact_facts'} & self.categories())

    def test_singleton_original_facts_require_premerge_synthesis_timestamp(self):
        self.add_child(name='Jordan Bravo')
        self.verdict('person-a', 'person-b')
        self.fact(self.parent)
        self.fact(mint_parent_id(('person-b',)))
        self.assertNotIn('mixed_contact_facts', self.categories())
        self.fact(mint_parent_id(('person-b',)), at='2026-03-01T00:00:00Z')
        mixed = next(f for f in self.findings() if f.category.value == 'mixed_contact_facts')
        self.assertEqual(mixed.child_ids, ('person-b',))

    def test_original_key_without_merge_time_does_not_prove_coverage(self):
        self.add_child(name='Jordan Bravo')
        self.fact(self.parent)
        self.fact(mint_parent_id(('person-b',)))
        self.assertIn('mixed_contact_facts', self.categories())

    def test_undated_accepted_merge_cannot_prove_original_coverage(self):
        self.add_child(name='Jordan Bravo')
        self.add_child('person-c', 'Jordan Bravo')
        self.verdict('person-a', 'person-b')
        self.verdict('person-a', 'person-c', at=None)
        self.fact(self.parent)
        mixed = next(f for f in self.findings() if f.category.value == 'mixed_contact_facts')
        self.assertIn('person-a', mixed.child_ids)

    def test_audit_reads_committed_uncheckpointed_wal(self):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("INSERT INTO people(person_id,parent_id,display_name) VALUES (?,?,?)",
                         ('person-b', self.parent, 'Casey Delta'))
            conn.execute("INSERT INTO person_identifiers(person_id,kind,normalized_value) VALUES (?,?,?)",
                         ('person-b', 'email', 'casey@example.com'))
            conn.execute("INSERT INTO person_sources(person_id,source) VALUES (?,?)", ('person-b', 'imessage'))
            conn.commit()
            self.assertGreater(Path(str(self.path) + '-wal').stat().st_size, 0)
            self.assertIn('missing_contact_facts', self.categories())
        finally:
            conn.close()

    def test_names_are_only_a_review_signal(self):
        self.add_child()
        signal = next(f for f in self.findings() if f.category.value == 'incompatible_child_names')
        self.assertEqual(signal.classification.value, 'needs_identity_review')
        self.assertEqual(signal.next_action.value, 'review_identity')

    def history(self, key, records, *, person=None, status='projected'):
        payload = records[0] if len(records) == 1 else {'records': records}
        self.db.project_rows((ArtifactRow(
            key, 'facts', self.parent, '/synthetic/' + key, 'fingerprint', status,
            person_id=person, payload_json=json.dumps(payload)),))

    def record(self, *, messages=(), title='Engineer', at='2026-01-01'):
        return {'facts': {'canonical_name': 'Jordan Bravo', 'title': title},
                'updated_at': at, 'messages': [
                    {'fingerprint': fingerprint, 'channel': 'imessage',
                     'at': at, 'direction': 'from_them'} for fingerprint in messages]}

    def test_contact_histories_and_derived_parent_have_no_unassigned_history(self):
        self.add_child(name='Jordan Bravo')
        self.fact('person-a', person='person-a')
        self.fact('person-b', person='person-b')
        self.history('facts:person-a', [self.record(messages=('message-a',))], person='person-a')
        self.history('facts:person-b', [self.record(messages=('message-b',))], person='person-b')
        self.history('parent-facts:' + self.parent, [self.record(messages=('message-a', 'message-b'))])
        self.assertNotIn('unassigned_parent_history', self.categories())

    def test_newer_preserved_mixed_history_remains_visible_after_contact_recovery(self):
        self.add_child(name='Jordan Bravo')
        self.fact('person-a', person='person-a')
        self.fact('person-b', person='person-b')
        old = self.record(messages=('message-a',))
        newer = self.record(messages=('message-new',), title='Founder', at='2026-03-01')
        self.history('facts:person-a', [old], person='person-a')
        self.history('facts:person-b', [self.record(messages=('message-b',))], person='person-b')
        key = 'facts:' + self.parent + ':pre-merge-repair'
        self.history(key, [old, newer], status='failed')
        self.history('parent-facts:' + self.parent, [old])
        found = next(f for f in self.findings() if f.category.value == 'unassigned_parent_history')
        self.assertEqual(found.classification.value, 'needs_identity_review')
        self.assertEqual(found.row_keys, (key,))
        self.assertEqual(found.unassigned_messages, 1)
        self.assertNotIn('lost', found.detail)

    def test_identical_original_singleton_history_is_already_assigned(self):
        self.fact('person-a', person='person-a')
        original = self.record()
        self.history('facts:person-a', [original], person='person-a')
        self.history('facts:' + self.parent, [original])
        self.assertNotIn('unassigned_parent_history', self.categories())

    def test_legacy_record_without_message_metadata_reports_uncertainty(self):
        self.fact('person-a', person='person-a')
        self.history('facts:person-a', [self.record()], person='person-a')
        self.history('facts:' + self.parent, [self.record(title='Founder', at='2026-03-01')])
        found = next(f for f in self.findings() if f.category.value == 'unassigned_parent_history')
        self.assertEqual(found.unassigned_records, 1)
        self.assertEqual(found.unassigned_messages, 0)
        self.assertIn('unproved', found.detail)

    def test_failed_extraction_does_not_claim_unassigned_success(self):
        self.fact('person-a', person='person-a')
        self.history('facts:person-a', [self.record()], person='person-a')
        failed = {'facts': {}, 'stop_reason': 'error', 'updated_at': '2026-03-01'}
        self.history('facts:' + self.parent, [failed], status='failed')
        self.assertNotIn('unassigned_parent_history', self.categories())

    def test_retired_metadata_aggregate_history_is_unassigned_contact_evidence(self):
        original = 'candidate:email:jordan@example.com'
        self.add_child(original, name='Jordan Bravo')
        self.db.project_rows((PersonIdentifiersProjection('person-a', ()),))
        self.db.replace_imported_people((PeopleRow(
            id='person-a', superseded_person_ids=json.dumps([original])),))
        self.fact(original, person=original)
        independent = self.record(messages=('original-message',))
        self.history('facts:' + original, [independent], person=original)
        self.history('facts:person-a', [self.record(messages=('mixed-message',))], person='person-a')
        self.history('parent-facts:' + self.parent, [independent])
        self.assertFalse(self.db.query("SELECT subject_key FROM facts WHERE person_id='person-a'"))
        found = next(f for f in self.findings() if f.category.value == 'unassigned_parent_history')
        self.assertEqual(found.row_keys, ('facts:person-a',))
        self.assertEqual(found.unassigned_messages, 1)

    def test_metadata_aggregate_cannot_prove_its_preserved_parent_history_coverage(self):
        original = 'candidate:email:jordan@example.com'
        self.add_child(original, name='Jordan Bravo')
        self.db.project_rows((PersonIdentifiersProjection('person-a', ()),))
        self.db.replace_imported_people((PeopleRow(
            id='person-a', superseded_person_ids=json.dumps([original])),))
        self.fact(original, person=original)
        self.history('facts:' + original, [self.record(messages=('original-message',))], person=original)
        mixed = self.record(messages=('mixed-message',))
        self.history('facts:person-a', [mixed], person='person-a')
        self.history('facts:' + self.parent, [mixed], status='failed')
        found = [f for f in self.findings() if f.category.value == 'unassigned_parent_history']
        self.assertEqual({f.row_keys for f in found}, {('facts:person-a',), ('facts:' + self.parent,)})

    def test_untouched_legacy_singleton_history_is_still_active_evidence(self):
        self.fact(self.parent)
        self.history('facts:' + self.parent, [self.record(messages=('message-a',))])
        self.assertNotIn('unassigned_parent_history', self.categories())

    def test_metadata_only_aggregate_does_not_require_contact_facts(self):
        self.db.project_rows((PersonRow('candidate:phone:+15550100', self.parent,
                                       display_name='Jordan Bravo'),))
        self.assertFalse({'missing_contact_facts', 'mixed_contact_facts'} & self.categories())

    def test_linkedin_only_child_does_not_require_message_synthesis(self):
        self.add_child(name='Jordan Bravo')
        self.db.project_rows((PersonSourcesProjection('person-b', (
            PersonSourceRow('person-b', 'linkedin_csv'),)),))
        self.assertFalse({'missing_contact_facts', 'mixed_contact_facts'} & self.categories())

    def test_command_cannot_open_writable_store_or_create_missing_database(self):
        before = self.path.read_bytes()
        with sqlite3.connect(self.path) as conn:
            before_rows = '\n'.join(conn.iterdump())
        with patch.object(Db, '__init__', side_effect=AssertionError('writable store')):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main(['--db', str(self.path)]), 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload['read_only'])
        self.assertEqual(payload['counts']['parents_scanned'], 1)
        self.assertEqual(before, self.path.read_bytes())
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(before_rows, '\n'.join(conn.iterdump()))
        missing = Path(self.temp.name) / 'missing' / 'deep-context.sqlite'
        with self.assertRaises(SystemExit):
            main(['--db', str(missing)])
        self.assertFalse(missing.parent.exists())


if __name__ == '__main__':
    unittest.main()
