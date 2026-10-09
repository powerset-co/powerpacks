"""Exercise the owner's ask worker with a synthetic store and stubbed HTTP/model calls.

Changelog:
  2026-10-09: the worker takes the typed Ask; the reply goes through agent_inbox.send.
  2026-10-08: an ask arrives as an agent message and the answer goes back as one; leases are gone.
  2026-10-08: cover family evidence, privacy, validation, leases, and task isolation.
"""
from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from packs.ingestion.primitives.ask_worker import ask_worker
from packs.powerset.primitives.agent_inbox import agent_inbox
from packs.powerset.primitives.agent_inbox.messages import Verdict, parse
from packs.ingestion.primitives.deep_context_v2.db import queries, queries_enrich
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesConfig
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SharedContextFact, SynthesizedFacts

NOW = "2026-10-08T12:00:00Z"
ASKER = str(uuid4())
ASK_ID = str(uuid4())
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
        self.posts = []
        self.http = self.enterContext(patch("urllib.request.urlopen", side_effect=self._http))
        self.enterContext(patch.object(agent_inbox.auth, "bearer_token", return_value="synthetic-token"))
        self.enterContext(patch.object(agent_inbox.auth, "api_base", return_value="http://localhost:8769"))
        self.enterContext(patch.object(OpenAIResponsesConfig, "resolve", return_value=OpenAIResponsesConfig(
            model="synthetic-model", effort="low", concurrency=1, timeout=120, max_retries=0)))
        self.enterContext(patch("packs.ingestion.primitives.deep_context_v2.openai.AsyncOpenAI", return_value=AsyncMock()))
        self.model = self.enterContext(patch.object(ask_worker.OpenAIResponsesCaller, "call",
                                                    new_callable=AsyncMock, return_value=ANSWER))
        self.stderr = io.StringIO()
        self.enterContext(redirect_stderr(self.stderr))

    def _message(self, *slugs: str) -> dict:
        return {"id": str(uuid4()), "kind": "ask", "created_at": NOW,
                "from": {"operator_id": ASKER, "name": "Casey Delta"},
                "payload": {"ask_id": ASK_ID, "question": "Who can advise on engineering?",
                            "role": {"title": "Founding engineer", "company": "Acme", "job_description": "Build it."},
                            "candidates": [{"public_identifier": slug, "linkedin_url": f"https://www.linkedin.com/in/{slug}",
                                            "name": "Jordan Bravo"} for slug in slugs]}}

    def _http(self, request, *, timeout):
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token")
        self.posts.append((request.full_url, json.loads(request.data)))
        return io.BytesIO(b'{"id":"m-reply","status":"sent"}')

    def _run(self):
        (self.data / "inbox").mkdir(parents=True, exist_ok=True)
        (self.data / "inbox" / f"{self.message['id']}.json").write_text(json.dumps(self.message))
        envelope = parse(self.message)
        return ask_worker.answer(envelope.id, envelope.from_operator_id, envelope.payload,
                                 repo_root=self.root, env_file=self.root / ".env")

    def _reply(self) -> dict:
        self.assertEqual(len(self.posts), 1)
        url, body = self.posts[0]
        self.assertEqual((url, body["to"], body["kind"]), ("http://localhost:8769/v2/agent-messages", ASKER, "ask_answer"))
        return body["payload"]

    def _audits(self):
        return list((self.data / "asks").glob("*.json"))

    def test_found_answers_the_asker_and_audits_only_evidence_identifiers(self) -> None:
        self._run()
        self.assertEqual(self._reply(), {"ask_id": ASK_ID, "answers": [{"public_identifier": SLUG, "answer": ANSWER}]})
        self.model.assert_awaited_once()
        request = self.model.call_args.kwargs
        prompt = json.loads(request["user_prompt"])
        self.assertEqual(prompt["role"]["title"], "Founding engineer")
        evidence = prompt["evidence"]
        self.assertEqual(evidence["facts"]["title"], "Engineer")
        self.assertEqual(evidence["profile"]["headline"], "Engineer")
        self.assertEqual(evidence["channels"], {
            "source_channels": ["gmail_msgvault", "imessage"], "interaction_counts": {"gmail_msgvault": 1, "imessage": 1},
            "first_message_at": "2026-09-01T12:00:00Z", "last_message_at": NOW, "last_interaction": NOW,
            "from_me": 1, "from_them": 1, "group_count": 2})
        audit = json.loads(self._audits()[0].read_text())
        self.assertEqual(audit["answer"], ANSWER)
        self.assertEqual(audit["evidence_used"][0]["candidate_ids"], ["c1", "c2"])
        self.assertIn("title", audit["evidence_used"][0]["fact_keys"])
        for text in (request["user_prompt"], json.dumps(audit), json.dumps(self.posts)):
            self.assertNotIn("SECRET", text)
            self.assertNotIn("casey@example.com", text)
        self.assertNotIn("Former colleagues", json.dumps(audit))
        self.assertIn(f"{SLUG} recommend", self.stderr.getvalue())

    def test_a_failed_reply_is_resent_without_a_second_model_call(self) -> None:
        self.http.side_effect = OSError("relay down")
        with self.assertRaises(OSError):
            self._run()
        self.http.side_effect = self._http
        self._run()
        self.model.assert_awaited_once()
        self.assertEqual(self._reply()["answers"][0]["answer"], ANSWER)

    def test_not_found_declines_without_model_or_audit(self) -> None:
        self.message = self._message("casey-delta-3c4d")
        self._run()
        self.assertEqual(self._reply()["answers"][0]["answer"], {"declined": True, "reason": "not_in_store"})
        self.model.assert_not_called()
        self.assertEqual(self._audits(), [])

    def test_human_rejection_overrides_confirmed_linkedin(self) -> None:
        queries_enrich.insert_linkedin(self.conn, ("c1", URL, "42", "human_override", "wrong_person", "human",
                                                   "f2", 1.0, "", NOW))
        self.conn.commit()
        self._run()
        self.assertEqual(self._reply()["answers"][0]["answer"], {"declined": True, "reason": "not_in_store"})
        self.model.assert_not_called()

    def test_invalid_model_answers_never_leave_the_laptop(self) -> None:
        for field, value in [("verdict", "yes"), ("reason", "Email casey@example.com"),
                             ("relationship", "casey@example.com"), ("confidence", -0.1), ("confidence", 1.1),
                             ("confidence", float("nan")), ("confidence", True), ("can_intro", "true"),
                             ("last_contact", "2026-13"), ("last_contact", "2026-10-08"), ("extra", "text")]:
            with self.subTest(field=field, value=value):
                self.posts.clear()
                self.model.return_value = ANSWER | {field: value}
                self._run()
                self.assertEqual(self._reply()["answers"][0]["answer"], {"declined": True, "reason": "failed"})
                self.assertEqual(self._audits(), [])
        self.assertNotIn("casey@example.com", self.stderr.getvalue())

    def test_long_text_is_cut_to_what_the_asker_accepts(self) -> None:
        self.model.return_value = ANSWER | {"reason": "r" * 900, "relationship": "w" * 600}
        self._run()
        answer = self._reply()["answers"][0]["answer"]
        self.assertEqual((answer["reason"], answer["relationship"]), ("r" * 500, "w" * 500))
        Verdict.parse(answer)

    def test_one_model_failure_does_not_stop_the_next_candidate(self) -> None:
        queries_enrich.insert_linkedin(self.conn, ("c3", "https://www.linkedin.com/in/casey-delta-3c4d", "43",
                                                   "human_override", "confirmed", "human", "f", 1.0, "", NOW))
        self.conn.execute("INSERT INTO candidate_parent (candidate_id, parent_id, reason, created_at) "
                          "VALUES ('c3', 'li:43', 'human', ?)", (NOW,))
        self.conn.commit()
        self.message = self._message(SLUG, "casey-delta-3c4d")
        self.model.side_effect = [RuntimeError("private model output"), ANSWER]
        self._run()
        self.assertEqual([item["answer"] for item in self._reply()["answers"]],
                         [{"declined": True, "reason": "failed"}, ANSWER])
        self.assertNotIn("private model output", self.stderr.getvalue())

    def test_answer_allows_null_contact_and_boundary_values(self) -> None:
        for verdict, confidence in [("unsure", 0), ("not_fit", 1)]:
            with self.subTest(verdict=verdict):
                answer = copy.deepcopy(ANSWER)
                answer.update(verdict=verdict, confidence=confidence, last_contact=None, reason="x" * 240)
                self.assertEqual(ask_worker._Answer.model_validate(answer).model_dump(), answer)


if __name__ == "__main__":
    unittest.main()
