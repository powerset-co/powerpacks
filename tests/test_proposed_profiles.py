"""Only judged, supported identities attach; human decisions remain authoritative."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from packs.ingestion.primitives.deep_context_v2.db import queries, queries_enrich, queries_realize
from packs.ingestion.primitives.deep_context_v2.db.schema import DDL
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.deep_context_v2.enrich import judge, proposals, research
from packs.ingestion.primitives.deep_context_v2.enrich.family import load_families
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import Profile
from packs.ingestion.primitives.deep_context_v2.enrich.sol_identity import SolVerdict
from packs.ingestion.primitives.deep_context_v2.realize.realize import Realize
from packs.ingestion.primitives.deep_context_v2.review.queue import load_card, review_list
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SynthesizedFacts

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

    def linkedin(self, *, decided_by="machine", verdict="needs_review"):
        queries_enrich.insert_linkedin(self.conn, ("c1", URL, "123", "research", verdict, decided_by,
                                                    "old", .99, "", NOW))

    def synthetic(self, *, linkedin_url=None):
        handle = research.handle(self.facts)
        queries_enrich.upsert_research(self.conn, (handle, "p:1", "complete" if linkedin_url else "no_match", json.dumps({
            "content": {"real_name": "Jordan Bravo", "summary": "An engineer and longtime community volunteer.",
                        "location_city": "Springfield", "linkedin_url": linkedin_url,
                        "work_experience": [{"title": "Engineer", "company_name": "Example Labs", "is_current": True}],
                        "education": [{"school_name": "Example College"}]}, "basis": []}), NOW))
        return "synthetic:" + handle

    def task(self, *, profiles=None):
        planned = judge.plan(proposals.derive(self.conn), profiles or {}, queries_enrich.machine_judgments(self.conn))
        self.assertEqual(len(planned.tasks), 1)
        return planned.tasks[0]

    def decide(self, task, *answers):
        with patch("packs.ingestion.primitives.deep_context_v2.enrich.sol_identity.verdict", AsyncMock(side_effect=answers)):
            verdicts = asyncio.run(judge.sol(None, task, ""))
        judge.write(self.conn, task, verdicts, NOW)

    def test_schema_six_upgrade_preserves_decisions_and_legacy_scores(self):
        old_ddl = DDL.replace("  match_probability REAL,  -- probability of same person (legacy confidence never populates this)\n", "")
        old_ddl = old_ddl.replace("judgment_fingerprint, match_probability, confidence", "judgment_fingerprint, confidence")
        old_ddl = old_ddl.replace("  CHECK (match_probability IS NULL OR match_probability BETWEEN 0 AND 1)",
                                  "  CHECK (origin <> 'synthetic' OR decided_by = 'human')")
        old_ddl = old_ddl.replace(", 'insufficient_evidence'", "")
        old_path = self.root / "old.sqlite"
        old = sqlite3.connect(old_path)
        old.executescript(old_ddl)
        old.execute("INSERT INTO meta VALUES ('schema_version', '6')")
        old.execute("INSERT INTO candidates VALUES ('c1', 'Jordan Bravo', 0, '{}', ?)", (NOW,))
        old.execute("INSERT INTO candidate_parent VALUES (NULL, 'c1', 'li:123', 'human', NULL, ?)", (NOW,))
        old.execute("INSERT INTO candidate_linkedins VALUES (NULL, 'c1', ?, '123', 'research', 'confirmed', 'human', 'old', .99, 'kept', ?)", (URL, NOW))
        old.commit()
        old.close()
        upgraded = open_store(old_path)
        self.addCleanup(upgraded.close)
        row = upgraded.execute("SELECT confidence, match_probability, decided_by, reason FROM current_linkedins").fetchone()
        self.assertEqual(tuple(row), (.99, None, 'human', 'kept'))
        self.assertEqual(queries_realize.profile_keys(upgraded), {'li:123': URL})
        self.assertEqual(upgraded.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_unjudged_and_old_confidence_do_not_attach(self):
        self.linkedin()
        self.synthetic()
        self.assertEqual(queries_realize.profile_keys(self.conn), {})

    def test_synthetic_threshold_persists_and_exports_without_linkedin(self):
        key = self.synthetic()
        task = self.task()
        self.decide(task, SolVerdict("needs_review", .90, "Independent source identifies the same person."))
        self.assertEqual(queries_realize.profile_keys(self.conn), {"p:1": key})
        self.assertEqual(review_list(self.conn), [])
        self.assertEqual(self.conn.execute("SELECT match_probability, confidence FROM candidate_linkedins").fetchone()[:], (.90, None))
        rows, _ = Realize(self.conn, self.root).build()
        self.assertEqual((rows[0]["linkedin_url"], rows[0]["current_company"]), ("", "Example Labs"))
        self.assertEqual(Realize(self.conn, self.root).run().status, "completed")
        self.assertEqual(judge.plan(proposals.derive(self.conn), {}, queries_enrich.machine_judgments(self.conn)).tasks, [])

    def test_single_low_match_keeps_person_and_worth_without_review(self):
        self.synthetic()
        self.decide(self.task(), SolVerdict("needs_review", .89, "Insufficient independent evidence."))
        self.assertEqual(queries_realize.profile_keys(self.conn), {})
        self.assertEqual(review_list(self.conn), [])
        self.assertEqual(self.conn.execute("SELECT worth FROM current_worth").fetchone()[0], "yes")
        self.assertEqual(len(Realize(self.conn, self.root).build()[0]), 1)

    def test_empty_or_unfetched_linkedin_does_not_veto_research(self):
        key = self.synthetic(linkedin_url=URL)
        empty = Profile(URL, "jordan-bravo", "123", "Jordan Bravo", "-", "Springfield", (), (), NOW)
        task = self.task(profiles={URL: empty})
        self.decide(task, SolVerdict("needs_review", .2, "Name only."), SolVerdict("confirmed", .96, "Independent biography."))
        self.assertEqual(queries_realize.profile_keys(self.conn), {"p:1": key})

    def test_same_linkedin_and_research_are_one_identity_with_richer_context(self):
        self.synthetic(linkedin_url=URL)
        profile = Profile(URL, "jordan-bravo", "123", "Jordan Bravo", "Engineer", "Springfield",
                          ("Engineer @ Example Labs, 2020-",), (), NOW)
        task = self.task(profiles={URL: profile})
        self.assertEqual(len(task.candidates), 1)
        self.decide(task, SolVerdict("confirmed", .98, "Same employer."))
        self.assertEqual(queries_realize.profile_keys(self.conn), {"li:123": URL})
        rows, _ = Realize(self.conn, self.root).build()
        self.assertEqual(rows[0]["linkedin_url"], URL)
        self.assertIn("community volunteer", rows[0]["summary"])
        self.assertEqual(review_list(self.conn), [])

    def test_explicit_research_linkedin_is_one_proposal_without_an_employer(self):
        self.synthetic(linkedin_url=URL)
        stored = queries_enrich.research_by_handle(self.conn)[research.handle(self.facts)]
        result = json.loads(stored.result_json)
        result["content"]["work_experience"] = []
        queries_enrich.upsert_research(self.conn, (research.handle(self.facts), "p:1", "complete", json.dumps(result), NOW))
        profile = Profile(URL, "jordan-bravo", "123", "Jordan Bravo", "Volunteer", "Springfield", (), (), NOW)
        task = self.task(profiles={URL: profile})
        self.assertEqual(len(task.candidates), 1)
        self.decide(task, SolVerdict("confirmed", .97, "Independent social context."))
        self.assertEqual(queries_realize.profile_keys(self.conn), {"li:123": URL})

    def test_distinct_supported_competitors_remain_pending(self):
        self.synthetic()
        profile = Profile(URL, "jordan-bravo", "123", "Jordan Bravo", "Engineer", "Springfield",
                          ("Engineer @ Other Labs, 2020-",), (), NOW)
        task = self.task()
        task = replace(task, candidates=(judge.ProposedProfile("research", profile), *task.candidates))
        self.decide(task, SolVerdict("needs_review", .92, "Plausible employer."), SolVerdict("confirmed", .96, "Plausible biography."))
        self.assertEqual(queries_realize.profile_keys(self.conn), {})
        self.assertEqual(review_list(self.conn), ["p:1"])
        self.assertEqual(len(load_card(self.conn, self.root, "p:1").pending), 2)

    def test_human_rejection_prevents_new_machine_attachment(self):
        self.linkedin(decided_by="human", verdict="wrong_person")
        self.synthetic()
        self.assertNotIn(URL, proposals.all_urls(proposals.derive(self.conn)))
        self.assertEqual(judge.plan(proposals.derive(self.conn), {}, set()).tasks, [])
        self.assertEqual(queries_realize.profile_keys(self.conn), {})

    def test_prior_pending_linkedin_is_rejudged_with_research(self):
        self.linkedin()
        key = self.synthetic()
        task = self.task()
        self.assertIn(URL, proposals.all_urls(proposals.derive(self.conn)))
        self.decide(task, SolVerdict("confirmed", .96, "Independent biography."))
        self.assertEqual(queries_realize.profile_keys(self.conn), {"p:1": key})
        self.assertEqual(review_list(self.conn), [])
        self.assertEqual(self.conn.execute("SELECT verdict FROM current_linkedins WHERE member_id = '123'").fetchone()[0],
                         "insufficient_evidence")
        self.assertEqual(judge.plan(proposals.derive(self.conn), {}, queries_enrich.machine_judgments(self.conn)).tasks, [])

    def test_new_evidence_does_not_reuse_an_old_machine_synthetic_confirmation(self):
        self.synthetic()
        self.decide(self.task(), SolVerdict("confirmed", .96, "Independent biography."))
        self.facts = replace(self.facts, location="Other City")
        queries.upsert_facts(self.conn, "c1", json.dumps(self.facts.to_payload()), "new-facts", "test", "low", NOW)
        self.synthetic()
        self.decide(self.task(), SolVerdict("needs_review", .50, "The new evidence does not establish the identity."))
        self.assertEqual(queries_realize.profile_keys(self.conn), {})
        self.assertEqual(review_list(self.conn), [])

    def test_deleted_family_is_not_judged(self):
        self.synthetic()
        self.conn.execute("INSERT INTO deleted_parents VALUES ('p:1', ?)", (NOW,))
        self.assertEqual(load_families(self.conn), [])


if __name__ == "__main__":
    unittest.main()
