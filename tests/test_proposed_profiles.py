"""A single proposed profile is usable until the owner corrects it."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.deep_context_v2.db import queries, queries_enrich, queries_realize
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.deep_context_v2.enrich import research
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profile, Profiles
from packs.ingestion.primitives.deep_context_v2.lookup import matches
from packs.ingestion.primitives.deep_context_v2.realize.realize import Realize
from packs.ingestion.primitives.deep_context_v2.review.api import ReviewApi
from packs.ingestion.primitives.deep_context_v2.review.commit import commit_review
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SynthesizedFacts
from packs.ingestion.primitives.share.web.model import SharePeople

NOW = "2026-10-20T00:00:00Z"
URL = "https://www.linkedin.com/in/jordan-bravo"


class ProposedProfilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = open_store(self.root / "store.sqlite")
        self.addCleanup(self.conn.close)
        queries.upsert_candidates(self.conn, [("c1", "Jordan Bravo", 0, "{}", NOW)])
        self.facts = SynthesizedFacts("Jordan Bravo", (), (), "", "", "", "", "friend", "personal",
                                      (), (), (), OwnedIdentifiers((), (), ()), (), 0.9, False)
        queries.upsert_facts(self.conn, "c1", json.dumps(self.facts.to_payload()), "facts", "test", "low", NOW)
        self.conn.execute("INSERT INTO candidate_parent VALUES (NULL, 'c1', 'p:1', 'singleton', NULL, ?)", (NOW,))
        self.conn.execute("INSERT INTO candidate_identifiers VALUES ('c1', 'email', 'casey@example.com', 'casey@example.com')")
        self.conn.execute("INSERT INTO candidate_sources VALUES ('c1', 'gmail_msgvault')")
        self.conn.execute("INSERT INTO worth VALUES (NULL, 'c1', 'yes', 'machine', '', NULL, 'facts', ?)", (NOW,))
        self.api = ReviewApi(self.conn, self.root)

    def linkedin(self, *, url=URL, member="123", verdict="needs_review", decided_by="machine"):
        queries_enrich.insert_linkedin(self.conn, ("c1", url, member, "research", verdict, decided_by,
                                                    "facts", None, "", NOW))
        self.conn.commit()

    def synthetic(self, *, handle=None):
        handle = handle or research.handle(self.facts)
        queries_enrich.upsert_research(self.conn, (handle, "p:1", "no_match", json.dumps({
            "content": {"real_name": "Jordan Research", "summary": "A researcher", "location_city": "Springfield",
                        "work_experience": [{"title": "Engineer", "company_name": "Example Labs", "is_current": True}],
                        "education": []}, "basis": []}), NOW))
        self.conn.commit()
        return "synthetic:" + handle

    def test_single_linkedin_attaches_without_identity_changes(self):
        self.linkedin()
        self.assertEqual(queries_realize.profile_keys(self.conn), {"p:1": URL})
        self.assertEqual(matches(self.conn, name="Jordan Bravo")[0].linkedin_url, URL)
        self.assertEqual(self.conn.execute("SELECT verdict, decided_by FROM candidate_linkedins").fetchone()[:],
                         ("needs_review", "machine"))
        self.assertEqual(self.conn.execute("SELECT parent_id FROM current_parent").fetchone()[0], "p:1")
        self.assertEqual(self.api.linkedin_card({}).pending, 0)
        self.assertIsNone(self.api.linkedin_card({}).card)
        self.assertEqual(self.api.linkedin_card({"index": ["0"]}).card.person.slug, "p:1")

    def test_worth_no_does_not_automatically_attach(self):
        self.linkedin()
        self.conn.execute("UPDATE worth SET worth = 'no'")
        self.assertEqual(queries_realize.profile_keys(self.conn), {})

    def test_synthetic_attaches_and_fills_realize_without_a_url(self):
        key = self.synthetic()
        self.assertEqual(queries_realize.profile_keys(self.conn), {"p:1": key})
        self.assertEqual(self.api.linkedin_card({}).pending, 0)
        rows, counts = Realize(self.conn, self.root).build()
        self.assertEqual(counts["synthetic"], 1)
        self.assertEqual((rows[0]["full_name"], rows[0]["current_title"], rows[0]["current_company"],
                          rows[0]["location_raw"], rows[0]["linkedin_url"]),
                         ("Jordan Research", "Engineer", "Example Labs", "Springfield", ""))
        self.assertEqual(Realize(self.conn, self.root).run().status, "completed")

    def test_human_accepted_synthetic_fills_its_stored_research(self):
        key = self.synthetic()
        self.api.decide({"parent_slug": ["p:1"], "decision": ["keep"], "pub": [key]})
        self.assertEqual(commit_review(self.conn, self.root), 1)
        self.assertEqual(queries_realize.profile_keys(self.conn), {"p:1": key})
        rows, counts = Realize(self.conn, self.root).build()
        self.assertEqual(rows[0]["full_name"], "Jordan Research")
        self.assertEqual(counts["synthetic"], 1)

    def test_competing_profiles_remain_pending(self):
        self.linkedin()
        self.linkedin(url="https://www.linkedin.com/in/casey-delta", member="456")
        self.assertEqual(queries_realize.profile_keys(self.conn), {})
        self.assertEqual(self.api.linkedin_card({}).pending, 1)

    def test_linkedin_and_synthetic_are_competing_profiles(self):
        self.linkedin()
        self.synthetic()
        self.assertEqual(queries_realize.profile_keys(self.conn), {})

    def test_old_synthetic_evidence_does_not_attach(self):
        self.synthetic(handle="old-evidence")
        self.assertEqual(queries_realize.profile_keys(self.conn), {})

    def test_synthetic_default_stays_with_its_correctable_family(self):
        key = self.synthetic()
        queries.upsert_candidates(self.conn, [("c2", "Jordan Bravo", 0, "{}", NOW)])
        queries.upsert_facts(self.conn, "c2", json.dumps(self.facts.to_payload()), "facts", "test", "low", NOW)
        self.conn.execute("INSERT INTO candidate_parent VALUES (NULL, 'c2', 'p:2', 'singleton', NULL, ?)", (NOW,))
        self.conn.execute("INSERT INTO worth VALUES (NULL, 'c2', 'yes', 'machine', '', NULL, 'facts', ?)", (NOW,))
        self.assertEqual(queries_realize.profile_keys(self.conn), {"p:1": key})

    def test_human_rejection_wins(self):
        self.linkedin(verdict="wrong_person", decided_by="human")
        self.linkedin()
        self.synthetic()
        self.assertEqual(queries_realize.profile_keys(self.conn), {})

    def test_machine_wrong_person_does_not_attach(self):
        self.linkedin(verdict="wrong_person")
        self.assertEqual(queries_realize.profile_keys(self.conn), {})

    def test_another_members_wrong_person_verdict_wins(self):
        self.linkedin()
        queries.upsert_candidates(self.conn, [("c2", "Casey Delta", 0, "{}", NOW)])
        self.conn.execute("INSERT INTO candidate_parent VALUES (NULL, 'c2', 'p:1', 'singleton', NULL, ?)", (NOW,))
        queries_enrich.insert_linkedin(self.conn, ("c2", URL, "123", "research", "wrong_person", "machine",
                                                    "facts", None, "", NOW))
        self.assertEqual(queries_realize.profile_keys(self.conn), {})

    def test_accepted_profile_keeps_existing_handling(self):
        self.linkedin(verdict="confirmed", decided_by="human")
        self.conn.execute("INSERT INTO candidate_parent VALUES (NULL, 'c1', 'li:123', 'human', NULL, ?)", (NOW,))
        self.conn.execute("INSERT INTO review_queue VALUES ('li:123', 'detach', '', '', ?)", (NOW,))
        self.assertEqual(queries_realize.profile_keys(self.conn), {"li:123": URL})

    def test_detach_linkedin_prevents_reattachment(self):
        self.linkedin()
        self.detach()

    def test_detach_synthetic_prevents_reattachment(self):
        self.synthetic()
        self.detach()

    def detach(self):
        people = SharePeople(self.conn, self.root)
        people.export_rows()
        self.api.decide({"parent_slug": ["p:1"], "decision": ["detach"]})
        self.assertEqual(queries_realize.profile_keys(self.conn), {})
        self.assertEqual(matches(self.conn, name="Jordan Bravo")[0].linkedin_url, "")
        self.assertEqual(people.export_rows()["p:1"]["full_name"], "Jordan Bravo")
        self.assertEqual(commit_review(self.conn, self.root), 1)
        self.assertEqual(queries_realize.profile_keys(self.conn), {})
        self.assertEqual(self.conn.execute("SELECT worth, decided_by FROM current_worth").fetchone()[:],
                         ("no", "human"))

    def test_retarget_linkedin_replaces_default(self):
        self.linkedin()
        self.retarget()

    def test_retarget_synthetic_replaces_default(self):
        self.synthetic()
        self.retarget()

    def retarget(self):
        url = "https://www.linkedin.com/in/casey-delta"
        self.api.decide({"parent_slug": ["p:1"], "decision": ["fix"], "new_url": [url]})
        self.assertEqual(queries_realize.profile_keys(self.conn), {})
        profile = Profile(url, "casey-delta", "456", "Casey Delta", "", "", (), (), NOW)
        with patch("packs.ingestion.primitives.deep_context_v2.review.decisions.load_profiles",
                   return_value=Profiles({url: profile}, 0, 0)):
            self.assertEqual(commit_review(self.conn, self.root), 1)
        self.assertEqual(queries_realize.profile_keys(self.conn), {"li:456": url})



if __name__ == "__main__":
    unittest.main()
