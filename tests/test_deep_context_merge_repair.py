import json
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import mint_parent_id
from packs.ingestion.primitives.deep_context.db.merge_repair import repair_merged_parents


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
        report = repair_merged_parents(self.db)
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
        self.assertEqual(repair_merged_parents(self.db).repaired, ())

    def test_unowned_fact_prevents_partial_repair(self):
        with self.db.transaction() as c:
            c.execute("UPDATE facts SET subject_key='unknown-original-parent' WHERE subject_key=?", (self.parents['person-b'],))
        before = [tuple(r) for r in self.db.query('SELECT * FROM people')]
        report = repair_merged_parents(self.db)
        self.assertEqual(report.repaired, ())
        self.assertEqual(len(report.unresolved), 1)
        self.assertEqual(before, [tuple(r) for r in self.db.query('SELECT * FROM people')])
        self.assertTrue(Path(report.backup_path).is_file())
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'")[0]['value'], '1')
        self.assertEqual(repair_merged_parents(self.db).unresolved, ())

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
                repair_merged_parents(self.db)
        self.assertEqual(before, [tuple(r) for r in self.db.query('SELECT * FROM people')])
        self.assertEqual(self.db.query("SELECT value FROM meta WHERE key='data_migration_version'"), [])

    def test_link_without_candidate_people_uses_exact_identifier(self):
        with self.db.transaction() as c:
            c.execute("DELETE FROM candidate_people WHERE row_key='candidate:phone:+15550100100'")
        report = repair_merged_parents(self.db)
        self.assertEqual(len(report.repaired), 1)
        self.assertEqual(report.unresolved, ())

    def test_mixed_facts_written_after_merge_remain_untouched(self):
        with self.db.transaction() as c:
            c.execute("UPDATE facts SET projected_at='2026-01-03T00:00:00Z'")
        report = repair_merged_parents(self.db)
        self.assertEqual(report.repaired, ())
        self.assertIn('predate', report.unresolved[0][1])

    def test_child_without_facts_gets_independent_parent(self):
        with self.db.transaction() as c:
            c.execute("INSERT INTO people(person_id,parent_id,display_name) VALUES ('person-unread',?,'Casey Bravo')", (self.parents['person-a'],))
            c.execute("INSERT INTO links(row_key,parent_id,public_identifier,kind,source) VALUES ('casey-bravo',?,'casey-bravo','pub','legacy-migration')", (self.parents['person-a'],))
            c.execute("INSERT INTO candidate_people VALUES ('casey-bravo','person-unread',?)", (self.parents['person-a'],))
        report = repair_merged_parents(self.db)
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
        report = repair_merged_parents(self.db)
        self.assertEqual(report.unresolved, ())
        self.assertEqual(self.db.query('SELECT count(*) n FROM parents')[0]['n'], 9)
        self.assertEqual(self.db.query('SELECT count(*) n FROM merge_verdicts WHERE accepted=1')[0]['n'], 0)

    def test_merged_context_machine_confirmation_is_invalidated(self):
        with self.db.transaction() as c:
            c.execute("UPDATE links SET decision_action=NULL,decision_approved=NULL,decision_source=NULL,machine_action='verify',machine_approved='auto',machine_judgment='confirmed',judgment_fingerprint='old-context',judgment_payload_json='{}',paid_profile=1 WHERE row_key='person-b'")
        report = repair_merged_parents(self.db)
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
        report = repair_merged_parents(self.db)
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
        report = repair_merged_parents(self.db)
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
        self.assertIn('predate', repair_merged_parents(self.db).unresolved[0][1])
