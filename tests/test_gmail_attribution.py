"""Per-message Gmail attribution from synthetic msgvault rows through both prompts."""
from __future__ import annotations

import json
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context_v2.collect import gmail
from packs.ingestion.primitives.deep_context_v2.collect.bundle import (
    CollectionBundle, EmailHeaders, MessageChannel, MessageEntry, Person,
)
from packs.ingestion.primitives.deep_context_v2.collect.readers import ContextSources
from packs.ingestion.primitives.deep_context_v2.synthesize.prompt import batches, render_batch


class GmailAttributionTests(unittest.TestCase):
    def archive(self, offset: int = 0) -> sqlite3.Connection:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        con.executescript("""
            CREATE TABLE sources (identifier TEXT);
            INSERT INTO sources VALUES ('owner@example.com');
            CREATE TABLE participants (id INTEGER, email_address TEXT, display_name TEXT);
            INSERT INTO participants VALUES
                (1, 'candidate@example.com', 'Casey Candidate'),
                (2, 'owner@example.com', 'Jordan Owner'),
                (3, 'other@example.com', 'Taylor Other');
            CREATE TABLE conversations (id INTEGER, source_conversation_id TEXT);
            CREATE TABLE messages (id INTEGER, sender_id INTEGER, sent_at TEXT, received_at TEXT,
                internal_date TEXT, conversation_id INTEGER, subject TEXT, snippet TEXT,
                message_type TEXT, deleted_at TEXT, deleted_from_source_at TEXT, source_message_id TEXT);
            CREATE TABLE message_bodies (message_id INTEGER, body_text TEXT);
            CREATE TABLE message_recipients (message_id INTEGER, participant_id INTEGER,
                recipient_type TEXT, display_name TEXT);
        """)
        for mid, sender, recipient, thread in [(1, 2, 3, 'source-thread-a'),
                                               (2, 2, 1, 'source-thread-b'),
                                               (3, 3, 1, 'source-thread-a'),
                                               (4, 1, 3, 'source-thread-c')]:
            con.execute("INSERT INTO conversations VALUES (?, ?)", (mid + offset, thread))
            con.execute("INSERT INTO messages VALUES (?, ?, '2026-10-01', NULL, NULL, ?, 'Project', ?, 'email', NULL, NULL, '')",
                        (mid + offset, sender, mid + offset, 'Same human message about project and plans.'))
            con.execute("INSERT INTO message_recipients VALUES (?, ?, 'to', '')", (mid + offset, recipient))
            con.execute("INSERT INTO message_recipients VALUES (?, 2, 'bcc', '')", (mid + offset,))
            if mid == 1:
                con.execute("INSERT INTO message_recipients VALUES (?, 1, 'cc', '')", (mid + offset,))
        return con

    def source(self, con: sqlite3.Connection) -> ContextSources:
        with patch.object(gmail, 'open_msgvault', return_value=con):
            return ContextSources(channels={'gmail_msgvault'}, msgvault_db=Path('synthetic.sqlite'),
                                  chat_db=Path('unused'), wacli_db=Path('unused'), deep_cap=20, max_group_size=8)

    def test_headers_preserve_addressing_without_adding_third_party_messages(self) -> None:
        messages = self.source(self.archive())._read_gmail(
            Person('candidate', 'Casey Candidate', ('candidate@example.com',), (), ('gmail_msgvault',)))
        self.assertEqual(len(messages), 3, 'same bodies with different headers must survive deduplication')
        headers = {message.email_headers.thread_id: message.email_headers for message in messages}
        owner_to_other = headers['source-thread-a']
        self.assertEqual(owner_to_other.sender, 'Jordan Owner <owner@example.com>')
        self.assertEqual(owner_to_other.to, ('Taylor Other <other@example.com>',))
        self.assertEqual(owner_to_other.cc, ('Casey Candidate <candidate@example.com>',))
        self.assertEqual(owner_to_other.bcc, ('Jordan Owner <owner@example.com>',))
        self.assertEqual(owner_to_other.account_emails, ('owner@example.com',))
        self.assertTrue(all('other@example.com' not in h.sender for h in headers.values()))
        self.assertEqual(self.source(self.archive(1000))._read_gmail(
            Person('candidate', 'Casey Candidate', ('candidate@example.com',), (), ('gmail_msgvault',))), messages)

    def test_payload_roundtrip_and_legacy_phone_bytes(self) -> None:
        old = {'channel': 'imessage', 'at': '2026-10-01', 'direction': 'from_me', 'subject': '', 'text': 'Hello'}
        phone = MessageEntry.from_payload(old)
        self.assertEqual(phone.to_payload(), old)
        headers = EmailHeaders('owner@example.com', ('other@example.com',), ('candidate@example.com',), (),
                               'stable-thread', ('owner@example.com',))
        email = replace(phone, channel=MessageChannel.GMAIL, email_headers=headers)
        self.assertEqual(MessageEntry.from_payload(email.to_payload()), email)
        self.assertEqual(json.dumps(phone.to_payload()), json.dumps(old))
        person = CollectionBundle.of(Person('c', 'Casey', (), (), ('imessage',)), messages=[phone], groups=[],
                                     thread_participants=(), available=1)
        self.assertEqual(render_batch(person, [phone]),
                         'CONTACT: Casey\nKnown emails: (none)\nKnown phones: (none)\nChannels: imessage\n\n'
                         'MESSAGES (most relevant, chronological):\n[imessage 2026-10-01 ME]: Hello')

    def test_large_recipient_lists_are_bounded_and_keep_owner_and_candidate(self) -> None:
        con = self.archive()
        for number in range(300):
            con.execute("INSERT INTO participants VALUES (?, ?, '')", (100 + number, f'a{number:03}@example.com'))
            con.execute("INSERT INTO message_recipients VALUES (1, ?, 'to', '')", (100 + number,))
        messages = gmail.recent_emails_for(con, 'candidate@example.com', 20, {'owner@example.com'})
        headers = next(message.email_headers for message in messages if message.email_headers.thread_id == 'source-thread-a')
        self.assertTrue(headers.recipients_omitted)
        self.assertEqual(len(headers.to) + len(headers.cc) + len(headers.bcc), gmail.MAX_MESSAGE_RECIPIENTS)
        self.assertEqual(headers.cc, ('Casey Candidate <candidate@example.com>',))
        self.assertEqual(headers.bcc, ('Jordan Owner <owner@example.com>',))
        self.assertIn('Additional recipients omitted', headers.render())

    def test_prompt_uses_headers_and_budgets_them(self) -> None:
        headers = EmailHeaders('owner@example.com', ('other@example.com',), ('candidate@example.com',), (),
                               'stable-thread', ('owner@example.com',))
        message = MessageEntry(MessageChannel.GMAIL, '2026-10-01', 'from_me', 'Project', 'Hello', headers)
        person = CollectionBundle.of(Person('c', 'Casey', ('candidate@example.com',), (), ('gmail_msgvault',)),
                                     messages=[message], groups=[], thread_participants=(), available=1)
        prompt = render_batch(person, [message], direction_labels={'from_me': 'from Owner to Casey'})
        for expected in ('From: owner@example.com', 'To: other@example.com', 'Cc: candidate@example.com',
                         'Thread: stable-thread', 'Account owner addresses: owner@example.com'):
            self.assertIn(expected, prompt)
        self.assertNotIn('from Owner to Casey', prompt)
        self.assertIn('Cc does not imply', prompt)
        self.assertIn('quoted', prompt)
        self.assertEqual(batches([message, message], chunk_chars=20, max_batches=5), [[message], [message]])
        self.assertNotEqual(message.content_order_key(), replace(message, email_headers=replace(headers, cc=())).content_order_key())
        legacy = replace(message, email_headers=None)
        prompt = render_batch(person, [legacy], direction_labels={'from_me': 'from Owner to Casey'})
        self.assertNotIn('from Owner to Casey', prompt)
        self.assertIn('recipients unavailable', prompt)


if __name__ == '__main__':
    unittest.main()
