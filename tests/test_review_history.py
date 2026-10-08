"""Review decisions advance once; revisiting and editing never changes the pending count."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context_v2.db import queries
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profile, Profiles
from packs.ingestion.primitives.deep_context_v2.review.api import ReviewApi
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SynthesizedFacts

NOW = "2026-10-08T00:00:00Z"
URL = "https://www.linkedin.com/in/jordan-bravo"


def seed(conn):
    queries.upsert_candidates(conn, [("c1", "Jordan Bravo", 0, "{}", NOW), ("c2", "Casey Delta", 0, "{}", NOW)])
    for candidate, name in (("c1", "Jordan Bravo"), ("c2", "Casey Delta")):
        facts = SynthesizedFacts(name, (), (), "", "", "", "", "", "", (), (), (),
                                 OwnedIdentifiers((), (), ()), (), 0.9, False)
        conn.execute("INSERT INTO facts VALUES (?, ?, 'facts', 'test', 'low', ?)",
                     (candidate, json.dumps(facts.to_payload()), NOW))
        conn.execute("INSERT INTO candidate_parent VALUES (NULL, ?, ?, 'singleton', NULL, ?)",
                     (candidate, "p:" + candidate, NOW))
        conn.execute("INSERT INTO worth VALUES (NULL, ?, 'yes', 'machine', '', NULL, 'facts', ?)", (candidate, NOW))
        conn.execute("INSERT INTO candidate_linkedins VALUES (NULL, ?, ?, '123', 'research', 'needs_review', 'machine', 'facts', NULL, '', ?)",
                     (candidate, URL, NOW))
    conn.commit()


class ReviewHistoryTests(unittest.TestCase):
    def test_decisions_and_edits_keep_order_count_and_finished_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conn = open_store(root / "store.sqlite")
            self.addCleanup(conn.close)
            seed(conn)
            api = ReviewApi(conn, root)
            self.assertEqual(api.linkedin_card({}).pending, 2)
            answer = api.decide({"parent_slug": ["p:c1"], "decision": ["detach"]}).next
            self.assertEqual((answer.card.person.slug, answer.pending, answer.queue.index), ("p:c2", 1, 1))
            previous = api.linkedin_card({"index": ["0"]})
            self.assertEqual((previous.card.person.slug, previous.pending), ("p:c1", 1))
            answer = api.decide({"parent_slug": ["p:c1"], "decision": ["keep"], "pub": ["123"]}).next
            self.assertEqual((answer.card.person.slug, answer.pending), ("p:c2", 1))
            self.assertEqual(conn.execute("SELECT worth FROM current_worth WHERE parent_id='li:123'").fetchone()[0], "yes")
            profile = Profile(URL, "jordan-bravo", "456", "Jordan Bravo", "", "", (), (), NOW)
            with patch("packs.ingestion.primitives.deep_context_v2.review.decisions.load_profiles", return_value=Profiles({URL: profile}, 0, 0)):
                answer = api.decide({"parent_slug": ["p:c1"], "decision": ["fix"], "new_url": [URL]}).next
            self.assertEqual(answer.pending, 1)
            self.assertEqual(conn.execute("SELECT parent_id FROM current_parent WHERE candidate_id='c1'").fetchone()[0], "li:456")
            answer = api.decide({"parent_slug": ["p:c2"], "decision": ["detach"]}).next
            self.assertEqual(answer.pending, 0)
            self.assertIsNotNone(answer.finished)
            self.assertIsNone(api.linkedin_card({}).card)
            self.assertEqual(api.linkedin_card({"index": ["0"]}).card.person.slug, "p:c1")
            self.assertEqual(api.decide({"parent_slug": ["p:c2"], "decision": ["keep"], "pub": ["123"]}).next.pending, 0)
