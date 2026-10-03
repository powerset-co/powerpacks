import json
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import mint_parent_id
from packs.ingestion.primitives.deep_context.db.merge_repair import _repair_merged_parents


class MergeRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Db(Path(self.temp.name) / 'deep-context.sqlite')
        self.ids = ('person-a', 'person-b', 'candidate:email:jordan@example.com')
        self.parents = {person: mint_parent_id((person,)) for person in self.ids}
        survivor = self.parents['person-a']
        with self.db.transaction() as c:
            c.execute("INSERT INTO parents(parent_id,public_identifier,display_name,human_worth,human_worth_source) VALUES (?,?,'Jordan Bravo','yes','user-guidance')", (survivor, survivor))
            for person in (*self.ids, 'candidate:phone:+15550100100'):
                c.execute('INSERT INTO people(person_id,parent_id,display_name) VALUES (?,?,?)', (person, survivor, person))
                c.execute("INSERT INTO links(row_key,parent_id,public_identifier,kind,source,decision_action,decision_approved,decision_source) VALUES (?,?,?,'pub','legacy-migration','verify','yes','user-guidance')", (person, survivor, person))
                c.execute('INSERT INTO candidate_people VALUES (?,?,?)', (person, person, survivor))
            c.execute("INSERT INTO person_identifiers VALUES ('candidate:phone:+15550100100','phone','15550100100','+15550100100')")
            for person, parent in self.parents.items():
                payload = json.dumps({'canonical_name': person, 'owned_identifiers': {'phones': ['+15550100100'] if person == 'person-a' else []}})
                c.execute("INSERT INTO artifacts(artifact_key,kind,parent_id,path,content_fingerprint,status,payload_json) VALUES (?,'facts',?,?,'paid-hash','projected',?)", ('facts:'+parent, survivor, '/facts/'+parent+'.jsonl', payload))
                c.execute("INSERT INTO facts(subject_key,parent_id,artifact_key,facts_json,projected_at) VALUES (?,?,?,?,'2026-01-01T00:00:00Z')", (parent, survivor, 'facts:'+parent, payload))
            for a,b,same,score,accepted in [('person-a',self.ids[2],1,.8,1),('person-b',self.ids[2],1,.7,1),('person-a','person-b',0,.9,0)]:
                a,b=sorted((a,b))
                c.execute("INSERT INTO merge_verdicts VALUES (?,?,?,?,?,'llm',?,?,1,'',?,'2026-01-02T00:00:00Z')", (a,b,a,b,'signature',same,score,accepted))

    def test_repair_preserves_child_facts_phone_and_human_decisions(self):
        before = [tuple(r) for r in self.db.query('SELECT row_key,decision_action,decision_approved,decision_source FROM links ORDER BY row_key')]
        report = _repair_merged_parents(self.db)
        self.assertEqual(len(report.repaired), 1)
        self.assertEqual(report.unresolved, ())
        owners = {r['person_id']:r['parent_id'] for r in self.db.query('SELECT person_id,parent_id FROM people')}
        self.assertEqual(owners['person-a'], owners['candidate:phone:+15550100100'])
        self.assertEqual(len({owners[person] for person in self.ids}), 3)
        self.assertEqual(self.db.query('SELECT count(*) n FROM merge_verdicts WHERE accepted=1')[0]['n'], 0)
        self.assertEqual(before, [tuple(r) for r in self.db.query('SELECT row_key,decision_action,decision_approved,decision_source FROM links ORDER BY row_key')])
        self.assertEqual(self.db.query("SELECT count(*) n FROM parents WHERE human_worth='yes'")[0]['n'],1)
        self.assertEqual(self.db.query('SELECT count(*) n FROM facts')[0]['n'],3)
        self.assertEqual(self.db.query('PRAGMA foreign_key_check'), [])
        self.assertTrue(Path(report.backup_path).is_file())
        self.assertEqual(_repair_merged_parents(self.db).repaired, ())

    def test_unowned_fact_prevents_partial_repair(self):
        with self.db.transaction() as c:
            c.execute("UPDATE facts SET subject_key='unknown-original-parent' WHERE subject_key=?", (self.parents['person-b'],))
        before = [tuple(r) for r in self.db.query('SELECT * FROM people')]
        report = _repair_merged_parents(self.db)
        self.assertEqual(report.repaired, ())
        self.assertEqual(len(report.unresolved), 1)
        self.assertEqual(before, [tuple(r) for r in self.db.query('SELECT * FROM people')])
        self.assertTrue(Path(report.backup_path).is_file())
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'")[0]['value'], '1')
        self.assertEqual(_repair_merged_parents(self.db).unresolved, ())

    def test_failed_transaction_does_not_mark_migration_or_move_children(self):
        from packs.ingestion.primitives.deep_context.db import merge_repair
        original = merge_repair._plan

        def invalid_plan(*args):
            plan = original(*args)
            updates = tuple((table, key, value, 'missing-parent') if table == 'people'
                            else (table, key, value, owner)
                            for table, key, value, owner in plan.updates)
            return replace(plan, updates=updates)

        before = [tuple(r) for r in self.db.query('SELECT * FROM people')]
        with patch.object(merge_repair, '_plan', side_effect=invalid_plan):
            with self.assertRaises(ValueError):
                _repair_merged_parents(self.db)
        self.assertEqual(before, [tuple(r) for r in self.db.query('SELECT * FROM people')])
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'"), [])

    def test_link_without_candidate_people_uses_exact_identifier(self):
        with self.db.transaction() as c:
            c.execute("DELETE FROM candidate_people WHERE row_key='candidate:phone:+15550100100'")
        report = _repair_merged_parents(self.db)
        self.assertEqual(len(report.repaired), 1)
        self.assertEqual(report.unresolved, ())

    def test_mixed_facts_written_after_merge_remain_untouched(self):
        with self.db.transaction() as c:
            c.execute("UPDATE facts SET projected_at='2026-01-03T00:00:00Z'")
        report = _repair_merged_parents(self.db)
        self.assertEqual(report.repaired, ())
        self.assertIn('predate', report.unresolved[0][1])

    def test_child_without_facts_gets_independent_parent(self):
        with self.db.transaction() as c:
            c.execute("INSERT INTO people(person_id,parent_id,display_name) VALUES ('person-unread',?,'Casey Bravo')", (self.parents['person-a'],))
            c.execute("INSERT INTO links(row_key,parent_id,public_identifier,kind,source) VALUES ('casey-bravo',?,'casey-bravo','pub','legacy-migration')", (self.parents['person-a'],))
            c.execute("INSERT INTO candidate_people VALUES ('casey-bravo','person-unread',?)", (self.parents['person-a'],))
        report = _repair_merged_parents(self.db)
        self.assertEqual(report.unresolved, ())
        self.assertEqual(self.db.query("SELECT parent_id FROM people WHERE person_id='person-unread'")[0]['parent_id'], mint_parent_id(('person-unread',)))
        self.assertEqual(self.db.query('SELECT count(*) n FROM facts')[0]['n'], 3)

    def test_eight_distinct_people_do_not_keep_the_old_generic_email_merge(self):
        survivor = self.parents['person-a']
        with self.db.transaction() as c:
            for index in range(6):
                person = f'person-extra-{index}'
                parent = mint_parent_id((person,))
                c.execute('INSERT INTO people(person_id,parent_id,display_name) VALUES (?,?,?)', (person, survivor, f'Jordan Variant {index}'))
                c.execute("INSERT INTO links(row_key,parent_id,public_identifier,kind,source) VALUES (?,?,?,'pub','legacy-migration')", (person, survivor, person))
                c.execute('INSERT INTO candidate_people VALUES (?,?,?)', (person, person, survivor))
                c.execute("INSERT INTO artifacts(artifact_key,kind,parent_id,path,content_fingerprint,status) VALUES (?,'facts',?,?,'paid-hash','projected')", ('facts:'+parent, survivor, '/facts/'+parent+'.jsonl'))
                c.execute("INSERT INTO facts(subject_key,parent_id,artifact_key,facts_json,projected_at) VALUES (?,?,?,'{}','2026-01-01T00:00:00Z')", (parent, survivor, 'facts:'+parent))
                a, b = sorted((person, self.ids[2]))
                c.execute("INSERT INTO merge_verdicts VALUES (?,?,?,?,?,'llm',1,.9,1,'',1,'2026-01-02T00:00:00Z')", (a,b,a,b,'signature'))
        report = _repair_merged_parents(self.db)
        self.assertEqual(report.unresolved, ())
        self.assertEqual(self.db.query('SELECT count(*) n FROM parents')[0]['n'], 9)
        self.assertEqual(self.db.query('SELECT count(*) n FROM merge_verdicts WHERE accepted=1')[0]['n'], 0)

    def test_merged_context_machine_confirmation_is_invalidated(self):
        with self.db.transaction() as c:
            c.execute("UPDATE links SET decision_action=NULL,decision_approved=NULL,decision_source=NULL,machine_action='verify',machine_approved='auto',machine_judgment='confirmed',judgment_fingerprint='old-context',judgment_payload_json='{}',paid_profile=1 WHERE row_key='person-b'")
        report = _repair_merged_parents(self.db)
        row = self.db.query("SELECT * FROM links WHERE row_key='person-b'")[0]
        self.assertIsNone(row['machine_judgment'])
        self.assertIsNone(row['judgment_fingerprint'])
        self.assertEqual(row['paid_profile'], 1)
        self.assertEqual(report.machine_verdicts_cleared, 1)
        self.assertEqual(self.db.query("SELECT decision_action FROM links WHERE row_key='person-a'")[0]['decision_action'], 'verify')

    def test_cross_parent_sibling_decision_is_removed_supported_one_remains(self):
        with self.db.transaction() as c:
            c.execute("UPDATE links SET decided_at='2026-01-01T00:00:00Z'")
            c.execute("UPDATE links SET decision_action='detach',decision_source='sibling-settle' WHERE row_key IN ('person-b','candidate:phone:+15550100100')")
        report = _repair_merged_parents(self.db)
        self.assertEqual(report.sibling_decisions_cleared, 1)
        self.assertIsNone(self.db.query("SELECT decision_action FROM links WHERE row_key='person-b'")[0]['decision_action'])
        self.assertEqual(self.db.query("SELECT decision_source FROM links WHERE row_key='candidate:phone:+15550100100'")[0]['decision_source'], 'sibling-settle')

    def test_paid_premerge_candidate_membership_preserves_original_join(self):
        with self.db.transaction() as c:
            c.execute("UPDATE facts SET facts_json='{}' WHERE subject_key=?", (self.parents['person-a'],))
            c.execute("INSERT INTO links(row_key,parent_id,public_identifier,kind,source) VALUES ('research:jordan',?,'jordan','research','deep-research')", (self.parents['person-a'],))
            for person in ('person-a','candidate:phone:+15550100100'):
                c.execute("INSERT INTO candidate_people VALUES ('research:jordan',?,?)", (person,self.parents['person-a']))
            c.execute("INSERT INTO artifacts(artifact_key,kind,parent_id,candidate_key,path,content_fingerprint,status,projected_at) VALUES ('research:jordan','research',?,'research:jordan','/paid/research.json','paid-hash','projected','2026-01-01T00:00:00Z')", (self.parents['person-a'],))
        report = _repair_merged_parents(self.db)
        self.assertEqual(report.unresolved, ())
        self.assertEqual(self.db.query("SELECT parent_id FROM people WHERE person_id='candidate:phone:+15550100100'")[0]['parent_id'], self.parents['person-a'])

    def test_parent_survey_does_not_restamp_internal_merged_verdicts(self):
        from packs.ingestion.primitives.deep_context.merge_candidates.receipts import survey_pairs, verdict_rows
        before = [tuple(row) for row in self.db.query('SELECT person_a,person_b,updated_at FROM merge_verdicts ORDER BY person_a,person_b')]
        survey = survey_pairs(self.db)
        self.assertEqual(len(survey.people), 1)
        self.assertEqual(survey.reused, [])
        self.assertEqual(survey.pairs, [])
        self.db.replace_merge_verdicts(verdict_rows(survey.initial_verdicts()))
        self.assertEqual(before, [tuple(row) for row in self.db.query('SELECT person_a,person_b,updated_at FROM merge_verdicts ORDER BY person_a,person_b')])
        with self.db.transaction() as c:
            c.execute("UPDATE facts SET projected_at='2026-01-03T00:00:00Z'")
        self.assertIn('predate', _repair_merged_parents(self.db).unresolved[0][1])

class CandidateMembershipRepairTests(unittest.TestCase):
    setUp = MergeRepairTests.setUp

    def test_version_three_restores_exact_person_phone_and_email_membership(self):
        from packs.ingestion.primitives.common.legacy import scrub_deep_context
        parent = self.parents['person-a']
        email = 'candidate:email:casey@example.com'
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta VALUES ('data_migration_version','3')")
            c.execute("DELETE FROM candidate_people WHERE row_key IN ('person-b','candidate:phone:+15550100100')")
            c.execute("INSERT INTO links(row_key,parent_id,public_identifier,kind,source) VALUES (?,?,'','candidate_email','legacy-migration')", (email, parent))
            c.execute("INSERT INTO person_identifiers VALUES ('person-b','email','casey@example.com','casey@example.com')")
        before = [tuple(r) for r in self.db.query('SELECT * FROM links ORDER BY row_key')]
        scrub_deep_context(self.db)
        members = {r['row_key']:r['person_id'] for r in self.db.query('SELECT * FROM candidate_people')}
        self.assertEqual(members['person-b'], 'person-b')
        self.assertEqual(members['candidate:phone:+15550100100'], 'candidate:phone:+15550100100')
        self.assertEqual(members[email], 'person-b')
        self.assertEqual(before, [tuple(r) for r in self.db.query('SELECT * FROM links ORDER BY row_key')])
        self.assertEqual(self.db.query('PRAGMA foreign_key_check'), [])
        scrub_deep_context(self.db)
        self.assertEqual(self.db.query('SELECT COUNT(*) FROM candidate_people')[0][0], 5)

    def test_ambiguous_and_foreign_family_identifiers_are_not_attached(self):
        from packs.ingestion.primitives.common.legacy import scrub_deep_context
        parent = self.parents['person-a']
        foreign = mint_parent_id(('person-foreign',))
        keys = ('candidate:phone:+15550100300', 'candidate:email:foreign@example.com', 'person-foreign')
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta VALUES ('data_migration_version','3')")
            c.execute('INSERT INTO parents(parent_id,public_identifier,display_name) VALUES (?,?,?)', (foreign, foreign, 'Jordan Foreign'))
            c.execute("INSERT INTO people(person_id,parent_id,display_name) VALUES ('person-foreign',?,'Jordan Foreign')", (foreign,))
            c.execute("INSERT INTO person_identifiers VALUES ('person-foreign','email','foreign@example.com','foreign@example.com')")
            for person in ('person-a','person-b'):
                c.execute("INSERT INTO person_identifiers VALUES (?,'phone','15550100300','+15550100300')", (person,))
            for key in keys:
                c.execute("INSERT INTO links(row_key,parent_id,public_identifier,kind,source) VALUES (?,?,'','pub','legacy-migration')", (key, parent))
        before = [tuple(r) for r in self.db.query('SELECT * FROM candidate_people ORDER BY row_key,person_id')]
        scrub_deep_context(self.db)
        self.assertEqual(before, [tuple(r) for r in self.db.query('SELECT * FROM candidate_people ORDER BY row_key,person_id')])
        self.assertEqual(self.db.query('PRAGMA foreign_key_check'), [])


class HistoricalMergeRepairTests(unittest.TestCase):
    setUp = MergeRepairTests.setUp
    def test_version_three_replays_unique_identifier_without_candidate_join(self):
        from packs.ingestion.primitives.common.legacy import _scrub_historical_merges
        directory = self.db.db_path.parent / 'facts'
        directory.mkdir()
        for person, history in self._histories().items():
            (directory / (person + '.jsonl')).write_text(json.dumps(history.payload()) + '\n')
        phone_key = 'candidate:phone:+15550100200'
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta VALUES ('data_migration_version','3')")
            c.execute("INSERT INTO links(row_key,parent_id,public_identifier,kind,source,decision_action,decision_approved,decision_source) VALUES (?,?,'','candidate_phone','legacy-migration','detach','yes','deep-context-review')", (phone_key, self.parents['person-a']))
            c.execute("INSERT INTO person_identifiers VALUES ('person-b','phone','15550100200','+15550100200')")
        decisions = [tuple(r) for r in self.db.query('SELECT row_key,decision_action,decision_approved,decision_source FROM links ORDER BY row_key')]
        report = _scrub_historical_merges(self.db)
        self.assertEqual(report.repaired, (self.parents['person-a'],))
        self.assertEqual(report.unresolved, ())
        self.assertEqual(self.db.query('SELECT parent_id FROM links WHERE row_key=?', (phone_key,))[0][0], self.parents['person-b'])
        self.assertEqual([tuple(row) for row in self.db.query('SELECT person_id,parent_id FROM candidate_people WHERE row_key=?', (phone_key,))], [('person-b', self.parents['person-b'])])
        self.assertEqual(decisions, [tuple(r) for r in self.db.query('SELECT row_key,decision_action,decision_approved,decision_source FROM links ORDER BY row_key')])
        self.assertEqual(self.db.query('PRAGMA foreign_key_check'), [])
        before = {table: [tuple(row) for row in self.db.query(f'SELECT * FROM {table} ORDER BY 1')] for table in ('people', 'facts', 'artifacts', 'links', 'candidate_people')}
        self.assertEqual(_scrub_historical_merges(self.db).repaired, ())
        self.assertEqual(before, {table: [tuple(row) for row in self.db.query(f'SELECT * FROM {table} ORDER BY 1')] for table in before})

    def test_version_three_unresolved_findings_recur_then_repair(self):
        from packs.ingestion.primitives.deep_context.db.merge_repair import _repair_historical_merges
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta VALUES ('data_migration_version','3')")
            c.execute("DELETE FROM candidate_people WHERE row_key='candidate:phone:+15550100100'")
            c.execute("UPDATE links SET row_key='candidate:phone:+15550100400' WHERE row_key='candidate:phone:+15550100100'")
            c.execute("UPDATE person_identifiers SET normalized_value='15550100400',display_value='+15550100400' WHERE person_id='candidate:phone:+15550100100'")
            c.execute("INSERT INTO person_identifiers VALUES ('person-b','phone','15550100400','+15550100400')")
        before = [tuple(row) for row in self.db.query('SELECT * FROM people')]
        first = _repair_historical_merges(self.db, self._histories())
        second = _repair_historical_merges(self.db, self._histories())
        self.assertEqual(first.unresolved, ((self.parents['person-a'], 'candidate has no unique child owner'),))
        self.assertEqual(first.unresolved, second.unresolved)
        self.assertEqual(before, [tuple(row) for row in self.db.query('SELECT * FROM people')])
        with self.db.transaction() as c:
            c.execute("DELETE FROM person_identifiers WHERE person_id='person-b' AND kind='phone'")
        self.assertEqual(_repair_historical_merges(self.db, self._histories()).repaired, (self.parents['person-a'],))

    def test_original_person_facts_and_paid_artifacts_retain_their_payloads(self):
        from packs.ingestion.primitives.deep_context.db.merge_repair import _repair_historical_merges
        parent = self.parents['person-a']
        payload = json.dumps({'canonical_name': 'Casey Delta', 'topics': ['original contact fact']})
        with self.db.transaction() as c:
            c.execute("INSERT INTO artifacts(artifact_key,kind,parent_id,person_id,path,content_fingerprint,status,payload_json) VALUES ('facts:person-b','facts',?,'person-b','/paid/person-b.jsonl','paid-hash','projected',?)", (parent, payload))
            c.execute("INSERT INTO facts(subject_key,parent_id,person_id,artifact_key,facts_json,projected_at) VALUES ('person-b',?,'person-b','facts:person-b',?,'2025-12-01')", (parent, payload))
        report = _repair_historical_merges(self.db, self._histories())
        self.assertEqual(report.unresolved, ())
        row = self.db.query("SELECT parent_id,facts_json,projected_at FROM facts WHERE subject_key='person-b'")[0]
        self.assertEqual(tuple(row), (self.parents['person-b'], payload, '2025-12-01'))
        row = self.db.query("SELECT parent_id,status,payload_json,content_fingerprint,path FROM artifacts WHERE artifact_key='facts:person-b'")[0]
        self.assertEqual(tuple(row), (self.parents['person-b'], 'projected', payload, 'paid-hash', '/paid/person-b.jsonl'))
        self.assertEqual(self.db.query('PRAGMA foreign_key_check'), [])

    def test_replay_does_not_undo_a_reassessed_merge(self):
        from packs.ingestion.primitives.deep_context.db.merge_repair import _repair_historical_merges
        _repair_historical_merges(self.db, self._histories())
        self.db.merge_parents(self.parents['person-a'], self.parents['person-b'])
        before = [tuple(row) for row in self.db.query('SELECT * FROM people')]
        report = _repair_historical_merges(self.db, self._histories())
        self.assertEqual(report.repaired, ())
        self.assertEqual(report.unresolved, ())
        self.assertEqual(before, [tuple(row) for row in self.db.query('SELECT * FROM people')])

    def test_positive_merge_without_stored_no_restores_originals(self):
        from packs.ingestion.primitives.deep_context.db.merge_repair import _repair_historical_merges
        from packs.ingestion.primitives.deep_context.synthesis.history import FactHistory
        with self.db.transaction() as c:
            c.execute('DELETE FROM merge_verdicts WHERE same_person=0')
            c.execute("INSERT INTO meta VALUES ('data_migration_version','2')")
            c.execute("UPDATE people SET display_name='Jordan Bravo' WHERE person_id='person-a'")
            c.execute("UPDATE people SET display_name='Casey Delta' WHERE person_id='person-b'")
        histories = {person: FactHistory.from_records([{'person_id':person,'updated_at':'2025-12-01','facts': {'canonical_name':person,'owned_identifiers': {'phones':['+15550100100']}}}]) for person in (*self.ids, 'candidate:phone:+15550100100')}
        before = [tuple(r) for r in self.db.query('SELECT row_key,decision_action,decision_source FROM links ORDER BY row_key')]
        report = _repair_historical_merges(self.db, histories)
        self.assertEqual(len(report.repaired), 1)
        self.assertEqual(report.unresolved, ())
        self.assertEqual(len({r['parent_id'] for r in self.db.query('SELECT parent_id FROM people')}), 4)
        self.assertEqual(before, [tuple(r) for r in self.db.query('SELECT row_key,decision_action,decision_source FROM links ORDER BY row_key')])
        self.assertEqual(self.db.query('PRAGMA foreign_key_check'), [])
        self.assertTrue(Path(report.backup_path).is_file())
        for row in self.db.query('SELECT facts_json FROM facts'):
            self.assertEqual(json.loads(row['facts_json'])['owned_identifiers']['phones'], [])
        self.assertEqual(_repair_historical_merges(self.db, histories).repaired, ())

    def _histories(self):
        from packs.ingestion.primitives.deep_context.synthesis.history import FactHistory
        return {person: FactHistory.from_records([{'person_id':person,'updated_at':'2025-12-01','facts': {'canonical_name':person}}]) for person in (*self.ids, 'candidate:phone:+15550100100')}

    def test_missing_original_facts_leaves_entire_family_unchanged(self):
        from packs.ingestion.primitives.deep_context.db.merge_repair import _repair_historical_merges
        histories = self._histories()
        del histories['person-b']
        before = [tuple(row) for row in self.db.query('SELECT * FROM people')]
        report = _repair_historical_merges(self.db, histories)
        self.assertEqual(report.repaired, ())
        self.assertEqual(report.unresolved[0][1], 'original child facts missing')
        self.assertEqual(before, [tuple(row) for row in self.db.query('SELECT * FROM people')])

    def test_unowned_candidate_leaves_entire_family_unchanged(self):
        from packs.ingestion.primitives.deep_context.db.merge_repair import _repair_historical_merges
        with self.db.transaction() as c:
            c.execute("DELETE FROM candidate_people WHERE row_key='person-b'")
            c.execute("UPDATE links SET row_key='unowned-profile' WHERE row_key='person-b'")
        report = _repair_historical_merges(self.db, self._histories())
        self.assertEqual(report.repaired, ())
        self.assertEqual(report.unresolved[0][1], 'candidate has no unique child owner')

    def test_mixed_paid_payload_retained_and_derived_sibling_detach_cleared(self):
        from packs.ingestion.primitives.deep_context.db.merge_repair import _repair_historical_merges
        with self.db.transaction() as c:
            c.execute("UPDATE links SET decided_at='2026-01-01'")
            c.execute("UPDATE links SET decision_action='detach',decision_source='sibling-settle' WHERE row_key='person-b'")
        payloads = [tuple(row) for row in self.db.query('SELECT artifact_key,payload_json FROM artifacts ORDER BY artifact_key')]
        report = _repair_historical_merges(self.db, self._histories())
        self.assertEqual(report.sibling_decisions_cleared, 1)
        self.assertIsNone(self.db.query("SELECT decision_action FROM links WHERE row_key='person-b'")[0][0])
        for key, payload in payloads:
            row = self.db.query('SELECT payload_json,status FROM artifacts WHERE artifact_key=?', (key + ':pre-merge-repair',))[0]
            self.assertEqual(row['payload_json'], payload)
            self.assertEqual(row['status'], 'failed')
        self.assertEqual(self.db.query('PRAGMA foreign_key_check'), [])

    def test_same_name_original_fact_parents_are_restored_for_reassessment(self):
        from packs.ingestion.primitives.deep_context.db.merge_repair import _repair_historical_merges
        with self.db.transaction() as c:
            c.execute("UPDATE people SET display_name='Jordan Bravo'")
            c.execute('DELETE FROM merge_verdicts WHERE same_person=0')
            c.execute("INSERT INTO meta VALUES ('data_migration_version','2')")
        report = _repair_historical_merges(self.db, self._histories())
        self.assertEqual(len(report.repaired), 1)
        self.assertEqual(report.unresolved, ())

    def test_legacy_boundary_reads_original_files_after_version_two(self):
        from packs.ingestion.primitives.common.legacy import _scrub_historical_merges
        histories = self._histories()
        directory = self.db.db_path.parent / 'facts'
        directory.mkdir()
        for person, history in histories.items():
            (directory / (person + '.jsonl')).write_text(json.dumps(history.payload()) + '\n')
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta VALUES ('data_migration_version','2')")
        report = _scrub_historical_merges(self.db)
        self.assertEqual(len(report.repaired), 1)
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'")[0][0], '3')
        for person, history in histories.items():
            self.assertEqual((directory / (person + '.jsonl')).read_text(), json.dumps(history.payload()) + '\n')

    def test_normal_merge_survey_sees_restored_original_people(self):
        from packs.ingestion.primitives.deep_context.db.merge_repair import _repair_historical_merges
        from packs.ingestion.primitives.deep_context.db.merge_queries import merge_people
        _repair_historical_merges(self.db, self._histories())
        self.assertEqual({person.parent_id for person in merge_people(self.db)}, set(self.parents.values()) | {mint_parent_id(('candidate:phone:+15550100100',))})

    def test_legacy_boundary_recovers_absorbed_parent_history(self):
        from packs.ingestion.primitives.common.legacy import _scrub_historical_merges
        directory = self.db.db_path.parent / 'facts'
        directory.mkdir()
        histories = self._histories()
        for person, history in histories.items():
            key = self.parents[person] if person == 'person-b' else person
            (directory / (key + '.jsonl')).write_text(json.dumps(history.payload()) + '\n')
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        decisions = [tuple(row) for row in self.db.query('SELECT row_key,decision_action,decision_source FROM links ORDER BY row_key')]
        report = _scrub_historical_merges(self.db)
        self.assertEqual(report.repaired, (self.parents['person-a'],))
        self.assertEqual(report.unresolved, ())
        restored = self.db.query('SELECT facts_json FROM facts WHERE parent_id=?', (self.parents['person-b'],))[0]
        self.assertEqual(json.loads(restored['facts_json'])['canonical_name'], histories['person-b'].facts.canonical_name)
        self.assertEqual(decisions, [tuple(row) for row in self.db.query('SELECT row_key,decision_action,decision_source FROM links ORDER BY row_key')])
        self.assertEqual(before, {path.name: path.read_bytes() for path in directory.iterdir()})
        self.assertEqual(_scrub_historical_merges(self.db).repaired, ())

    def test_legacy_boundary_does_not_use_current_mixed_parent_history(self):
        from packs.ingestion.primitives.common.legacy import _scrub_historical_merges
        directory = self.db.db_path.parent / 'facts'
        directory.mkdir()
        for person, history in self._histories().items():
            key = self.parents[person] if person == 'person-a' else person
            (directory / (key + '.jsonl')).write_text(json.dumps(history.payload()) + '\n')
        report = _scrub_historical_merges(self.db)
        self.assertEqual(report.repaired, ())
        self.assertEqual(report.unresolved, ((self.parents['person-a'], 'original child facts missing'),))

    def test_historical_write_failure_rolls_back_payloads_and_version(self):
        from contextlib import contextmanager
        from packs.ingestion.primitives.deep_context.db.merge_repair import _repair_historical_merges
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO meta VALUES ('data_migration_version','2')")
        tables = ('people', 'parents', 'facts', 'artifacts', 'links', 'merge_verdicts', 'meta')
        before = {table: [tuple(row) for row in self.db.query(f'SELECT * FROM {table} ORDER BY 1')] for table in tables}
        original = self.db.transaction

        @contextmanager
        def interrupted_write():
            with original() as conn:
                yield conn
                if conn.total_changes:
                    raise OSError('interrupted before commit')

        with patch.object(self.db, 'transaction', interrupted_write):
            with self.assertRaises(OSError):
                _repair_historical_merges(self.db, self._histories())
        after = {table: [tuple(row) for row in self.db.query(f'SELECT * FROM {table} ORDER BY 1')] for table in tables}
        self.assertEqual(before, after)
        self.assertEqual(len(_repair_historical_merges(self.db, self._histories()).repaired), 1)


class MigrationSequenceTests(unittest.TestCase):
    setUp = MergeRepairTests.setUp

    def test_stage_entry_runs_all_pending_repairs_in_order(self):
        from packs.ingestion.primitives.common.legacy import scrub_deep_context
        report, removed, historical = scrub_deep_context(self.db)
        self.assertEqual(len(report.repaired), 1)
        self.assertEqual(removed, 0)
        self.assertEqual(historical.unresolved, ((self.parents['person-a'], 'original child facts missing'),))
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'")[0][0], '3')
        self.assertEqual(scrub_deep_context(self.db)[0].repaired, ())

    def test_failed_second_repair_preserves_first_and_stops_the_next(self):
        from packs.ingestion.primitives.common import legacy
        with patch.object(legacy, '_scrub_harmonic_profiles', side_effect=OSError('interrupted')), patch.object(legacy, '_scrub_historical_merges') as historical:
            with self.assertRaises(OSError):
                legacy.scrub_deep_context(self.db)
            historical.assert_not_called()
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'")[0][0], '1')
        self.assertEqual(len({r['parent_id'] for r in self.db.query('SELECT parent_id FROM people')}), 3)
        legacy.scrub_deep_context(self.db)
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'")[0][0], '3')

    def test_failed_first_repair_stops_later_repairs_and_the_stage(self):
        from packs.ingestion.primitives.deep_context.db import merge_repair
        from packs.ingestion.primitives.common import legacy
        from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
        with patch.object(merge_repair, '_repair_merged_parents', side_effect=OSError('interrupted')), patch.object(legacy, '_scrub_harmonic_profiles') as harmonic, patch.object(legacy, '_scrub_historical_merges') as historical:
            with self.assertRaises(OSError):
                EnsureParents(db=self.db, people_csv=Path(self.temp.name) / 'missing.csv').run()
            harmonic.assert_not_called()
            historical.assert_not_called()
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'"), [])

    def test_ensure_parents_runs_repairs_from_sqlite_without_import_csv(self):
        from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
        node = EnsureParents(db=self.db, people_csv=Path(self.temp.name) / 'missing.csv')
        result = node.run()
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.people_projected, 0)
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'")[0][0], '3')
        before = [tuple(row) for row in self.db.query('SELECT * FROM people')]
        self.assertEqual(node.run().status, 'completed')
        self.assertEqual(before, [tuple(row) for row in self.db.query('SELECT * FROM people')])

    def test_invalid_import_preserves_database_until_valid_retry(self):
        from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
        path = Path(self.temp.name) / 'people.csv'
        raw = 'id,full_name\nperson-new,Jordan Bravo\n'
        path.write_text(raw)
        before = [tuple(row) for row in self.db.query('SELECT * FROM people ORDER BY person_id')]
        with patch('packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents.read_imported_people', side_effect=OSError('unreadable CSV')):
            with self.assertRaises(OSError):
                EnsureParents(db=self.db, people_csv=path).run()
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'"), [])
        self.assertEqual(before, [tuple(row) for row in self.db.query('SELECT * FROM people ORDER BY person_id')])
        self.assertEqual(path.read_text(), raw)
        self.assertEqual(EnsureParents(db=self.db, people_csv=path).run().status, 'completed')
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'")[0][0], '3')
