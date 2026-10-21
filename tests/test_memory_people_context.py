"""People evidence expansion is local, bounded and identity-scoped."""
import tempfile
import unittest
from pathlib import Path

from test_memory import source_db
from packs.memory.models import Claim, Kind
from packs.memory.provider import OpenAIProvider
from packs.memory.query import Query
from packs.memory.sources import V2
from packs.memory.store import Store


class PeopleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        adapter = V2(Path(tmp.name) / 'source')
        source_db(adapter.database, [dict(member=n, parent=n, name=n, claims=[]) for n in ('Alex', 'Blair')])
        self.store = Store(Path(tmp.name) / 'memory.sqlite', create=True)
        self.addCleanup(self.store.close)
        self.store.import_source(adapter)
        provider = OpenAIProvider('', '')
        self.addCleanup(provider.close)
        self.query = Query(self.store, provider)
        for name, text, kind, topic in (
            ('Alex', 'Co-homebuyer', Kind.RELATIONSHIP, 'relationship'),
            ('Alex', '2024 Wedding with the owner', Kind.STATEMENT, 'event'),
            ('Alex', 'Enjoys sailing', Kind.MENTION, 'sailing'),
            ('Blair', 'Romantic partner in 2015', Kind.RELATIONSHIP, 'relationship'),
        ):
            source = self.store.conn.execute("SELECT id,hash FROM sources WHERE member=? AND key LIKE 'facts:%'", (name,)).fetchone()
            self.store.add_claim(source['id'], source['hash'], None,
                                 Claim(text, kind, topic, 'owner', '', '', '', text), human=False)
        self.store.conn.commit()

    def test_unique_event_and_relationship_are_combined_without_discovery(self):
        result = self.query.people('sailing')
        self.assertEqual(len(result.people), 1)
        person = result.people[0]
        self.assertEqual(person.name, 'Alex')
        texts = [hit.claim.text for hit in person.context]
        self.assertIn('Co-homebuyer', texts)
        self.assertIn('2024 Wedding with the owner', texts)
        self.assertTrue(all(hit.parent == person.evidence[0].parent for hit in (*person.evidence, *person.context)))
        self.assertTrue(all(hit.citations for hit in person.context))

    def test_context_reports_truncation(self):
        result = self.query.people('sailing', limit=5)
        person = result.people[0]
        context, truncated = self.query._context(person.evidence[0].parent, set(), 10)
        self.assertEqual(context, ())
        self.assertTrue(truncated)
