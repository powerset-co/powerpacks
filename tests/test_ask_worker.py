"""Exercise the owner's ask worker with a synthetic store and stubbed HTTP/model calls.

Changelog:
  2026-10-08: cover family evidence, privacy, validation, leases, and task isolation.
"""
from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from packs.ingestion.primitives.ask_worker import ask_worker
from packs.ingestion.primitives.deep_context_v2.db import queries, queries_enrich
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesConfig
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SharedContextFact, SynthesizedFacts

NOW = "2026-10-08T12:00:00Z"
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
        self.tasks = [self._task()]
        self.posts = []
        self.http = self.enterContext(patch("urllib.request.urlopen", side_effect=self._http))
        self.enterContext(patch.object(ask_worker.auth, "bearer_token", return_value="synthetic-token"))
        self.enterContext(patch.object(ask_worker.auth, "api_base", return_value="http://localhost:8769"))
        self.enterContext(patch.object(OpenAIResponsesConfig, "resolve", return_value=OpenAIResponsesConfig(
            model="synthetic-model", effort="low", concurrency=1, timeout=120, max_retries=0)))
        self.enterContext(patch("packs.ingestion.primitives.deep_context_v2.openai.AsyncOpenAI", return_value=AsyncMock()))
        self.model = self.enterContext(patch.object(ask_worker.OpenAIResponsesCaller, "call",
                                                    new_callable=AsyncMock, return_value=ANSWER))
        self.stderr = io.StringIO()
        self.enterContext(redirect_stderr(self.stderr))
        self.device_id = str(uuid4())

    def _task(self, slug: str = SLUG) -> dict:
        return {"task_id": str(uuid4()), "ask_id": str(uuid4()), "question": "Who can advise on engineering?",
                "asker": {"operator_id": str(uuid4()), "name": "Casey Delta"},
                "candidate": {"public_identifier": slug, "linkedin_url": f"https://www.linkedin.com/in/{slug}",
                              "name": "Jordan Bravo"}, "leased_until": "2026-10-08T12:15:00Z"}

    def _http(self, request, *, timeout):
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token")
        self.assertEqual(request.get_header("X-device-id"), self.device_id)
        if request.get_method() == "GET":
            self.assertEqual(request.full_url, "http://localhost:8769/v2/ask-tasks?limit=20")
            return io.BytesIO(json.dumps({"tasks": self.tasks}).encode())
        self.posts.append((request.full_url, json.loads(request.data)))
        return io.BytesIO(b'{"status":"answered"}')

    def _run(self):
        return ask_worker.run(repo_root=self.root, env_file=self.root / ".env", device_id=self.device_id)

    def _audits(self):
        return list((self.data / "asks").glob("*.json"))

    def test_found_posts_answer_and_audits_only_evidence_identifiers(self) -> None:
        self.assertEqual(self._run(), [ANSWER])
        task = self.tasks[0]
        self.assertEqual(self.posts, [(f"http://localhost:8769/v2/ask-tasks/{task['task_id']}/answer", ANSWER)])
        self.model.assert_awaited_once()
        request = self.model.call_args.kwargs
        evidence = json.loads(request["user_prompt"])["evidence"]
        self.assertEqual(evidence["facts"]["title"], "Engineer")
        self.assertEqual(evidence["profile"]["headline"], "Engineer")
        self.assertEqual(evidence["channels"], {
            "source_channels": ["gmail_msgvault", "imessage"], "interaction_counts": {"gmail_msgvault": 1, "imessage": 1},
            "first_message_at": "2026-09-01T12:00:00Z", "last_message_at": NOW, "last_interaction": NOW,
            "from_me": 1, "from_them": 1, "group_count": 2})
        audit = json.loads(self._audits()[0].read_text())
        self.assertEqual(audit["task"], task)
        self.assertEqual(audit["answer"], ANSWER)
        self.assertEqual(audit["evidence_used"][0]["candidate_ids"], ["c1", "c2"])
        self.assertIn("title", audit["evidence_used"][0]["fact_keys"])
        self.assertTrue(audit["answered_at"].endswith("Z"))
        for text in (request["user_prompt"], json.dumps(audit), json.dumps(self.posts)):
            self.assertNotIn("SECRET", text)
            self.assertNotIn("casey@example.com", text)
        self.assertNotIn("Former colleagues", json.dumps(audit))
        self.assertIn(f"{SLUG} recommend", self.stderr.getvalue())

    def test_not_found_declines_without_model_or_audit(self) -> None:
        self.tasks = [self._task("casey-delta-3c4d")]
        declined = {"declined": True, "reason": "not_in_store"}
        self.assertEqual(self._run(), [declined])
        self.assertEqual(self.posts[0][1], declined)
        self.model.assert_not_called()
        self.assertEqual(self._audits(), [])
        self.assertIn("casey-delta-3c4d declined", self.stderr.getvalue())

    def test_human_rejection_overrides_confirmed_linkedin(self) -> None:
        queries_enrich.insert_linkedin(self.conn, ("c1", URL, "42", "human_override", "wrong_person", "human",
                                                   "f2", 1.0, "", NOW))
        self.conn.commit()
        self.assertEqual(self._run(), [{"declined": True, "reason": "not_in_store"}])
        self.model.assert_not_called()

    def test_invalid_model_answers_never_post_or_write(self) -> None:
        for field, value in [("verdict", "yes"), ("reason", "x" * 241), ("reason", "Email casey@example.com"),
                             ("relationship", "casey@example.com"), ("confidence", -0.1), ("confidence", 1.1),
                             ("confidence", float("nan")), ("confidence", True), ("can_intro", "true"),
                             ("last_contact", "2026-13"), ("last_contact", "2026-10-08"), ("extra", "text")]:
            with self.subTest(field=field, value=value):
                self.model.return_value = ANSWER | {field: value}
                self.assertEqual(self._run(), [])
                self.assertEqual(self.posts, [])
                self.assertEqual(self._audits(), [])
        self.assertNotIn("casey@example.com", self.stderr.getvalue())

    def test_one_model_failure_does_not_stop_next_task(self) -> None:
        self.tasks.append(self._task())
        self.model.side_effect = [RuntimeError("private model output"), ANSWER]
        self.assertEqual(self._run(), [ANSWER])
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(self.posts[0][0].endswith(f"/{self.tasks[1]['task_id']}/answer"))
        self.assertEqual([path.stem for path in self._audits()], [self.tasks[1]["task_id"]])
        self.assertNotIn("private model output", self.stderr.getvalue())

    def test_lease_conflict_writes_nothing_and_next_task_posts(self) -> None:
        self.tasks.append(self._task())
        first_id = self.tasks[0]["task_id"]

        def conflict(request, *, timeout):
            if request.full_url.endswith(f"/{first_id}/answer"):
                raise urllib.error.HTTPError(request.full_url, 409, "expired", {}, None)
            return self._http(request, timeout=timeout)

        self.http.side_effect = conflict
        self.assertEqual(self._run(), [ANSWER])
        self.assertEqual([path.stem for path in self._audits()], [self.tasks[1]["task_id"]])

    def test_empty_queue_does_not_call_model(self) -> None:
        self.tasks = []
        self.assertEqual(self._run(), [])
        self.model.assert_not_called()
        self.assertEqual(self._audits(), [])

    def test_answer_allows_null_contact_and_boundary_values(self) -> None:
        for verdict, confidence in [("unsure", 0), ("not_fit", 1)]:
            with self.subTest(verdict=verdict):
                answer = copy.deepcopy(ANSWER)
                answer.update(verdict=verdict, confidence=confidence, last_contact=None, reason="x" * 240)
                self.assertEqual(ask_worker._Answer.model_validate(answer).model_dump(), answer)


if __name__ == "__main__":
    unittest.main()
