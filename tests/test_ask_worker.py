"""Exercise passive Codex ask preparation and delivery with a synthetic store and relay.

Changelog:
  2026-10-09: patch auth at the shared relay sender.
  2026-10-08: an ask arrives as an agent message and the answer goes back as one; leases are gone.
  2026-10-08: cover family evidence, privacy, validation, leases, and task isolation.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from packs.ingestion.primitives.ask_worker import ask_worker
from packs.ingestion.primitives.deep_context_v2.db import queries, queries_enrich
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SharedContextFact, SynthesizedFacts

NOW = "2026-10-08T12:00:00Z"
ASKER = "00000000-0000-4000-8000-000000000001"
SLUG = "jordan-bravo-1a2b"
URL = f"https://www.linkedin.com/in/{SLUG}"
ANSWER = {"verdict": "recommend", "reason": "Relevant engineering experience.", "can_intro": True,
          "relationship": "former colleague", "last_contact": "2026-10", "confidence": 0.8}


class AskWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / ".powerpacks"
        self.conn = open_store(store_path(self.data))
        self.addCleanup(self.conn.close)
        queries.upsert_candidates(self.conn, [("c1", "Jordan Bravo", 0, "{}", NOW),
                                               ("c2", "Jordan Bravo", 0, "{}", NOW),
                                               ("c3", "Casey Delta", 0, "{}", NOW)])
        self.conn.executemany(
            "INSERT INTO candidate_parent (candidate_id, parent_id, reason, created_at) VALUES (?, ?, 'human', ?)",
            [("c1", "p:old", NOW), ("c1", "li:42", NOW), ("c2", "li:42", NOW), ("c3", "p:other", NOW)])
        queries_enrich.insert_linkedin(self.conn, ("c1", URL, "42", "human_override", "confirmed", "human",
                                                   "f", 1.0, "", NOW))
        facts = SynthesizedFacts(
            canonical_name="Jordan Bravo", aliases=(), employers=(), title="Engineer", school="", field_of_study="",
            location="", relationship_to_owner="former colleague", relationship_category="professional",
            topics=("engineering",), notable_events=(), identifiers=("casey@example.com",),
            owned_identifiers=OwnedIdentifiers(("casey@example.com",), (), ()),
            shared_context=(SharedContextFact("work", "Former colleagues", "SECRET QUOTED MESSAGE"),),
            confidence=0.9, is_owner=False)
        self.conn.execute(
            "INSERT INTO facts VALUES (?, ?, 'f', 'm', 'low', ?)", ("c2", json.dumps(facts.to_payload()), NOW))
        for candidate, channel, direction, at in [
            ("c1", "gmail_msgvault", "from_me", "2026-09-01T12:00:00Z"),
            ("c2", "imessage", "from_them", NOW), ("c3", "whatsapp", "from_them", NOW),
        ]:
            bundle = {"messages": [{"channel": channel, "direction": direction, "at": at,
                                     "text": "SECRET MESSAGE BODY", "subject": "SECRET SUBJECT"}],
                      "groups": ["SECRET GROUP TITLE"]}
            self.conn.execute("INSERT INTO bundles VALUES (?, ?, 'f', ?)", (candidate, json.dumps(bundle), NOW))
        self.conn.commit()
        cache = self.data / "network-import" / "profile_cache_v2" / f"{SLUG}.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"raw_response": {}, "normalized_profile": {
            "success": True, "member_id": "42", "full_name": "Jordan Bravo", "headline": "Engineer",
            "experiences": [{"title": "Engineer", "company_name": "Example Labs"}],
            "email": "casey@example.com"}}))
        self.message = self._message(SLUG)
        self.relay = Mock()
        self.sets = SimpleNamespace(data_root=self.data, known_operators=lambda: {ASKER}, message=self.relay)
        self.worker = ask_worker.AskWorker(self.sets)
        self.path = self.data / "inbox" / f"{self.message['id']}.json"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(self.message))

    def _message(self, *slugs):
        return {"id": str(uuid4()), "kind": "ask", "created_at": NOW,
                "from": {"operator_id": ASKER, "name": "Casey Delta"},
                "payload": {"ask_id": str(uuid4()), "question": "Who can advise on engineering?",
                            "role": {"title": "Founding engineer", "company": "Acme", "job_description": "Build it."},
                            "candidates": [{"public_identifier": slug, "linkedin_url": f"https://www.linkedin.com/in/{slug}",
                                            "name": "Jordan Bravo"} for slug in slugs]}}

    def batch(self, answer=ANSWER, slug=SLUG):
        return {"answers": [{"public_identifier": slug, "answer": answer}]}

    def thread(self):
        self.worker.thread(self.message["id"], "synthetic-thread")

    def test_pending_is_passive_and_uses_filtered_family_evidence(self):
        before = self.path.read_bytes()
        pending = self.worker.pending()[0]
        self.assertEqual(pending["id"], self.message["id"])
        self.assertEqual(pending["context"]["role"]["title"], "Founding engineer")
        evidence = pending["context"]["candidates"][0]["evidence"]
        self.assertEqual(evidence["facts"]["title"], "Engineer")
        self.assertEqual(evidence["profile"]["headline"], "Engineer")
        self.assertEqual(evidence["channels"]["interaction_counts"], {"gmail_msgvault": 1, "imessage": 1})
        self.assertIn("answers", pending["output_schema"]["properties"])
        self.assertNotIn("SECRET", json.dumps(pending))
        self.assertNotIn("casey@example.com", json.dumps(pending))
        self.assertEqual(self.path.read_bytes(), before)
        self.relay.assert_not_called()

    def test_thread_required_and_completed_answer_preserves_legacy_reply(self):
        with self.assertRaises(ValueError):
            self.worker.finish(self.message["id"], self.batch())
        self.thread()
        self.worker.finish(self.message["id"], self.batch())
        self.relay.assert_called_once_with(ASKER, "ask_answer", {"ask_id": self.message["payload"]["ask_id"], **self.batch()})
        self.assertEqual(self.worker.pending(), [])
        audit = json.loads(next((self.data / "asks").glob("*.json")).read_text())
        self.assertEqual(audit["answer"], ANSWER)
        self.assertEqual(audit["evidence_used"][0]["candidate_ids"], ["c1", "c2"])
        self.assertNotIn("SECRET", json.dumps(audit))
        self.worker.finish(self.message["id"], self.batch())
        self.assertEqual(self.relay.call_count, 1)

    def test_delivery_retry_uses_saved_batch_and_never_changes_thread_or_answer(self):
        self.thread()
        self.relay.side_effect = OSError("relay offline")
        with self.assertRaises(OSError):
            self.worker.finish(self.message["id"], self.batch())
        with patch.object(self.worker, "_prepare", side_effect=AssertionError("saved outcome must win")):
            pending = self.worker.pending()[0]
        self.assertEqual(pending["answer"], self.batch())
        self.assertEqual(pending["thread_id"], "synthetic-thread")
        with self.assertRaises(ValueError):
            self.worker.thread(self.message["id"], "different-thread")
        self.relay.side_effect = None
        self.worker.finish(self.message["id"], self.batch(ANSWER | {"reason": "Must not replace saved result"}))
        self.assertEqual(self.relay.call_args.args[2]["answers"], self.batch()["answers"])

    def test_not_found_is_resolved_without_a_codex_thread(self):
        queries_enrich.insert_linkedin(self.conn, ("c1", URL, "42", "human_override", "wrong_person", "human", "f2", 1.0, "", NOW))
        self.conn.commit()
        pending = self.worker.pending()[0]
        expected = self.batch({"declined": True, "reason": "not_in_store"})
        self.assertEqual(pending["answer"], expected)
        self.worker.finish(self.message["id"], expected)
        self.assertEqual(self.relay.call_args.args[2]["answers"], expected["answers"])

    def test_existing_paid_audit_is_reused_without_a_thread(self):
        audit = self.data / "asks" / f"{self.message['id']}-{SLUG}.json"
        audit.parent.mkdir(parents=True)
        audit.write_text(json.dumps({"answer": ANSWER}))
        with patch.object(ask_worker, "_evidence", side_effect=AssertionError("paid cache must win")):
            self.assertEqual(self.worker.pending()[0]["answer"], self.batch())
            self.worker.finish(self.message["id"], self.batch())
        self.assertEqual(json.loads(audit.read_text()), {"answer": ANSWER})

    def test_invalid_batch_never_leaves_the_laptop(self):
        self.thread()
        bad_answers = [self.batch(ANSWER | {"reason": "Email casey@example.com"}),
                       self.batch(ANSWER | {"confidence": True}), self.batch(ANSWER | {"last_contact": "2026-13"}),
                       self.batch(ANSWER | {"extra": "private"}), self.batch(slug="someone-else"), {"answers": []},
                       {"answers": self.batch()["answers"] * 2}]
        for answer in bad_answers:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                self.worker.finish(self.message["id"], answer)
        self.relay.assert_not_called()
        self.assertEqual(list((self.data / "asks").glob("*.json")), [])

    def test_unknown_sender_cannot_start_or_finish(self):
        self.sets.known_operators = lambda: set()
        self.assertEqual(self.worker.pending(), [])
        with self.assertRaises(PermissionError):
            self.thread()
        with self.assertRaises(PermissionError):
            self.worker.finish(self.message["id"], self.batch())
        self.relay.assert_not_called()

    def test_failure_before_thread_is_saved_and_delivered_only_once(self):
        from packs.powerset.primitives.agent_inbox.collaboration_messages import Failure
        self.relay.side_effect = OSError("relay offline")
        with self.assertRaises(OSError):
            self.worker.finish(self.message["id"], Failure(error="Codex unavailable"))
        pending = self.worker.pending()[0]
        self.assertEqual(pending["error"], "Codex unavailable")
        self.assertEqual(pending["answer"], self.batch({"declined": True, "reason": "failed"}))
        with self.assertRaises(ValueError):
            self.thread()
        self.relay.side_effect = None
        self.worker.finish(self.message["id"], Failure(error="Must not replace saved failure"))
        self.worker.finish(self.message["id"], Failure(error="Must not replace saved failure"))
        self.assertEqual(self.relay.call_count, 2)
        self.assertEqual(self.relay.call_args.args[2]["answers"], self.batch({"declined": True, "reason": "failed"})["answers"])


if __name__ == "__main__":
    unittest.main()
