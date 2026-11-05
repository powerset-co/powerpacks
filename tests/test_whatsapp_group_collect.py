"""Small group collection from synthetic, local message stores."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_logbook_build import make_chat_db, make_wacli, add_whatsapp
from packs.ingestion.primitives.deep_context_v2.collect.bundle import MessageChannel, MessageDirection, Person
from packs.ingestion.primitives.deep_context_v2.collect.collect import Collect, MAX_GROUP_SIZE
from packs.ingestion.primitives.deep_context_v2.collect.readers import ContextSources, WHATSAPP_MAX_GROUP_SIZE
from packs.ingestion.primitives.deep_context_v2.db import queries
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.synthesize.prompt import render_batch


class GroupCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wacli = make_wacli(self.root / 'messages/wacli')
        self.chat = self.root / 'chat.db'
        make_chat_db(self.chat)
        self.person = Person('candidate:phone:+15550100', 'Jordan Bravo', (), ('+15550100',), ('whatsapp',))
        self.source = ContextSources(channels={'whatsapp'}, msgvault_db=self.root / 'absent', chat_db=self.chat,
                                     wacli_db=self.wacli, deep_cap=1600, max_group_size=MAX_GROUP_SIZE,
                                     owner_name='Alex Owner', contact_names={'+15550101': 'Casey Delta'})
        with sqlite3.connect(self.wacli.parent / 'session.db') as con:
            con.execute('CREATE TABLE whatsmeow_lid_map (lid TEXT, pn TEXT)')
            con.execute("INSERT INTO whatsmeow_lid_map VALUES ('190001000000001', '15550100')")
            con.execute("INSERT INTO whatsmeow_lid_map VALUES ('190001000000002', '15550101')")

    def group(self, jid, size, *, member='15550100@s.whatsapp.net'):
        with sqlite3.connect(self.wacli) as con:
            con.execute('INSERT INTO groups VALUES (?, ?, NULL)', (jid, 'Study ' + jid))
            members = ['15550999@s.whatsapp.net', member]
            members.extend(f'155502{n:02}@s.whatsapp.net' for n in range(size - 2))
            con.executemany('INSERT INTO group_participants VALUES (?, ?)', [(jid, m) for m in members])

    def message(self, jid, text, sender, *, from_me=False, name=None, ts=1790000000):
        add_whatsapp(self.wacli, jid, text, ts, text, sender_jid=sender)
        with sqlite3.connect(self.wacli) as con:
            con.execute('UPDATE messages SET from_me=?, sender_name=? WHERE msg_id=?', (from_me, name, text))

    def test_message_membership_and_ten_including_owner_eleven_excluded(self):
        self.assertEqual((WHATSAPP_MAX_GROUP_SIZE, MAX_GROUP_SIZE), (10, 25))
        self.group('ten@g.us', 10, member='15550888@s.whatsapp.net')
        self.group('eleven@g.us', 11)
        self.message('ten@g.us', 'eligible', '15550100@s.whatsapp.net')
        self.message('eleven@g.us', 'too large', '15550100@s.whatsapp.net')
        messages, _ = self.source.collect_person(self.person)
        self.assertEqual([m.text for m in messages], ['eligible'])
        self.assertEqual(messages[0].sender, 'Jordan Bravo')

    def test_silent_lid_membership_names_directions_mentions_and_text_only(self):
        self.group('silent@g.us', 4, member='190001000000001@lid')
        with sqlite3.connect(self.wacli) as con:
            con.execute("INSERT INTO contacts (jid, phone) VALUES ('190001000000001@lid', '190001000000001')")
        self.message('silent@g.us', 'owner', '15550999@s.whatsapp.net', from_me=True)
        self.message('silent@g.us', 'candidate', '190001000000001@lid', name='Wrong old name')
        self.message('silent@g.us', 'hi @190001000000001 and @190001000000002 and @88888', '190001000000002@lid', name='Taylor Echo')
        self.message('silent@g.us', 'unknown', '15550888@s.whatsapp.net')
        self.message('silent@g.us', '', '190001000000001@lid')
        with sqlite3.connect(self.wacli) as con:
            con.execute("INSERT INTO contacts (jid, full_name) VALUES ('15550101@s.whatsapp.net','Casey Delta')")
            con.execute("UPDATE messages SET media_caption='caption', media_type='image' WHERE text=''")
        messages, available = self.source.collect_person(self.person)
        by_text = {m.text: m for m in messages}
        self.assertEqual(available, 4)
        self.assertEqual((by_text['owner'].direction, by_text['owner'].sender), (MessageDirection.FROM_ME, 'Alex Owner'))
        self.assertEqual((by_text['candidate'].direction, by_text['candidate'].sender), (MessageDirection.FROM_THEM, 'Jordan Bravo'))
        other = by_text['hi @Jordan Bravo and @Casey Delta and @88888']
        self.assertEqual((other.direction, other.sender), (MessageDirection.FROM_OTHER, 'Taylor Echo'))
        self.assertEqual(by_text['unknown'].sender, '+15550888 (no saved name)')
        self.assertTrue(all(m.channel == MessageChannel.WHATSAPP_GROUP for m in messages))
        rendered = render_batch(self.bundle(messages), messages)
        self.assertIn('[Study silent@g.us(whatsapp_group) 2026-09-21 (them) Jordan Bravo]: candidate', rendered)
        # The silent member still sees the group's other messages after their own row is removed.
        with sqlite3.connect(self.wacli) as con:
            con.execute("DELETE FROM messages WHERE text='candidate'")
        self.assertEqual(len(self.source.collect_person(self.person)[0]), 3)
        # Without sender_name, use the saved wacli contact name.
        with sqlite3.connect(self.wacli) as con:
            con.execute('UPDATE messages SET sender_name=NULL')
        named = next(m for m in self.source.collect_person(self.person)[0] if m.text.startswith('hi '))
        self.assertEqual(named.sender, 'Casey Delta')

    def bundle(self, messages):
        from packs.ingestion.primitives.deep_context_v2.collect.bundle import CollectionBundle
        return CollectionBundle.of(self.person, messages=messages, groups=[], thread_participants=(), available=len(messages))

    def test_owner_jid_and_lid_mentions_use_owner_name_instead_of_push_name(self):
        with sqlite3.connect(self.wacli.parent / 'session.db') as con:
            con.execute('CREATE TABLE whatsmeow_device (jid TEXT, lid TEXT)')
            con.execute("INSERT INTO whatsmeow_device VALUES ('15550999:50@s.whatsapp.net', '190001000000003:50@lid')")
        with sqlite3.connect(self.wacli) as con:
            con.executemany('INSERT INTO contacts (jid, push_name) VALUES (?, ?)', [
                ('15550999@s.whatsapp.net', 'alexhandle'),
                ('190001000000003@lid', 'alexhandle'),
            ])
        self.group('small@g.us', 3)
        self.message('small@g.us', 'hello', '15550999@s.whatsapp.net', from_me=True)
        self.message('small@g.us', '@190001000000003 and @15550999', '15550100@s.whatsapp.net')
        messages, _ = self.source.collect_person(self.person)
        rendered = render_batch(self.bundle(messages), messages)
        self.assertIn('[Study small@g.us(whatsapp_group) 2026-09-21 (me) Alex Owner]: hello', rendered)
        self.assertIn('[Study small@g.us(whatsapp_group) 2026-09-21 (them) Jordan Bravo]: @Alex Owner and @Alex Owner', rendered)

    def test_newest_group_cap_and_dm_precedence_at_character_cap(self):
        self.group('small@g.us', 3)
        for n in range(3):
            self.message('small@g.us', f'group {n}', '15550100@s.whatsapp.net', ts=1790000000+n)
        self.source.deep_cap = 2
        messages, _ = self.source.collect_person(self.person)
        self.assertEqual([m.text for m in messages], ['group 1', 'group 2'])
        self.message('15550100@s.whatsapp.net', 'dm first', '15550100@s.whatsapp.net')
        with patch('packs.ingestion.primitives.deep_context_v2.collect.readers.SAFETY_CHAR_CAP', 8):
            messages, _ = self.source.collect_person(self.person)
        self.assertEqual([m.text for m in messages], ['dm first'])
        self.assertNotIn('sender', messages[0].to_payload())

    def test_collect_persists_group_names_and_sender_using_store_names(self):
        con = open_store(store_path(self.root))
        self.addCleanup(con.close)
        queries.upsert_owner(con, json.dumps({'name': 'Alex Owner'}), 'owner', '2026-10-01')
        queries.upsert_candidates(con, [(self.person.person_id, self.person.full_name, 0, '{}', '2026-10-01')])
        queries.insert_candidate_identifiers(con, [(self.person.person_id, 'phone', self.person.phones[0], self.person.phones[0])])
        queries.insert_candidate_sources(con, [(self.person.person_id, 'whatsapp')])
        con.commit()
        self.group('small@g.us', 3)
        self.message('small@g.us', 'hello', '15550999@s.whatsapp.net', from_me=True)
        result = Collect(con, self.root, 100, self.chat).run()
        self.assertEqual(result.counts['messages_whatsapp_group'], 1)
        payload = json.loads(con.execute('SELECT payload_json FROM bundles').fetchone()[0])
        self.assertEqual(payload['groups'], ['Study small@g.us'])
        self.assertEqual(payload['messages'][0]['sender'], 'Alex Owner')

    def test_imessage_twenty_five_cap_and_named_other(self):
        with sqlite3.connect(self.chat) as con:
            con.executemany('INSERT INTO handle VALUES (?, ?)', [(n, f'+155501{n-1:02}') for n in range(1,27)])
            for cid, size in ((1,25), (2,26)):
                con.execute('INSERT INTO chat VALUES (?, ?, ?, ?, ?)', (cid, f'guid{cid}', f'chat{cid}', 'Study', 'Study'))
                con.executemany('INSERT INTO chat_handle_join VALUES (?, ?)', [(cid,n) for n in range(1,size+1)])
                con.execute('INSERT INTO message VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                            (cid, f'msg{cid}', 2, 599616000000000000, 0, 0, f'size {size}', None))
                con.execute('INSERT INTO chat_message_join VALUES (?, ?)', (cid,cid))
        person = Person('c', 'Jordan Bravo', (), ('+15550100',), ('imessage',))
        messages, _ = self.source.collect_person(person)
        self.assertEqual([m.text for m in messages], ['size 25'])
        self.assertEqual((messages[0].direction, messages[0].sender), (MessageDirection.FROM_OTHER, 'Casey Delta'))
        self.assertIn('[Study(imessage_group) 2020-01-02 (other) Casey Delta]: size 25', render_batch(self.bundle(messages), messages))
        self.source.contact_names.clear()
        self.assertEqual(self.source.collect_person(person)[0][0].sender, '+15550101 (no saved name)')

