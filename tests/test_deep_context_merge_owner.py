import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.merge_queries import merge_people
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.merge_candidates.candidate_pairs import generate_pairs


class MergeOwnerIdentifiersTests(unittest.TestCase):
    def test_owner_context_identifiers_never_bridge_contacts_without_owner_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Db(Path(temporary) / 'deep-context.sqlite')
            with db.transaction() as conn:
                conn.execute("INSERT INTO owner_context(context_key,payload_json,path,content_fingerprint) VALUES ('owner',?,'/owner.json','owner-hash')", (json.dumps({'name':'Jordan Bravo','emails':['OWNER@example.com'],'phones':['+15550100200']}),))
                for person, name in [('person-a','Casey Charlie'), ('person-b','Taylor Delta')]:
                    conn.execute('INSERT INTO parents(parent_id,public_identifier,display_name) VALUES (?,?,?)', (person,person,name))
                    conn.execute('INSERT INTO people(person_id,parent_id,display_name) VALUES (?,?,?)', (person,person,name))
                    for kind, value in [('email',person+'@work.example'),('email','owner@example.com'),('phone','+15550100200')]:
                        conn.execute('INSERT INTO person_identifiers(person_id,kind,normalized_value) VALUES (?,?,?)', (person,kind,value))
                    conn.execute("INSERT INTO artifacts(artifact_key,kind,parent_id,path,content_fingerprint,status) VALUES (?,'facts',?,'/facts.jsonl','paid-hash','projected')", ('facts:'+person,person))
                    payload = json.dumps({'canonical_name':name,'owned_identifiers':{'emails':['owner@example.com'],'phones':['(555) 010-0200']}})
                    conn.execute('INSERT INTO facts(subject_key,parent_id,artifact_key,facts_json) VALUES (?,?,?,?)', (person,person,'facts:'+person,payload))
            self.assertEqual(db.query('SELECT count(*) n FROM people WHERE is_owner=1')[0]['n'], 0)
            people = merge_people(db)
            self.assertEqual(len(people), 2)
            self.assertEqual(generate_pairs(people), [])
            for person in people:
                self.assertNotIn('owner@example.com', person.all_emails)
                self.assertNotIn('5550100200', person.all_phones)
