"""Shared search transport and explicit desktop question actions, using an in-memory relay."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import urllib.parse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from pydantic import ValidationError

from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.share.web.sets import CloudError, Member, SetView
from packs.ingestion.primitives.deep_context_v2.db import queries_share
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.share.web.sets import Sets
from packs.powerset.primitives.agent_inbox.collaboration_messages import (
    Answer, CreateConversation, Failure, ResendMessage, SendMessage, Thread,
)
from packs.powerset.primitives.agent_inbox.messages import Rejected, parse
from packs.shared.web.collaboration import Collaboration, CollaborationRoutes
from packs.shared.web import server as local_server


class LocalSets:
    def __init__(self, root: Path, me: Member, relay: dict) -> None:
        self.data_root = root
        self.identity = me
        self.views = []
        self.relay = relay
        self.sent = []

    def me(self):
        return self.identity

    def kept(self):
        return self.views

    def members(self, view):
        return list(view.members)

    def settle(self):
        pass

    def message(self, to, kind, payload):
        raw = {"id": str(uuid4()), "kind": kind, "payload": payload,
               "from": {"operator_id": self.identity.operator_id, "name": self.identity.name},
               "created_at": now_iso()}
        parse(raw)
        write_json(self.relay[to].data_root / "inbox" / f"{raw['id']}.json", raw)
        self.sent.append((to, raw))
        return {"id": raw["id"], "status": "queued"}


class CollaborationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.relay = {}
        self.members = [Member(name, "synthetic@example.com", "member", str(uuid4()))
                        for name in ["Jordan Bravo", "Casey Example", "Taylor Demo"]]
        self.set_id, self.other_set = str(uuid4()), str(uuid4())
        self.stores = []
        for index, member in enumerate(self.members):
            sets = LocalSets(self.root / str(index), member, self.relay)
            self.relay[member.operator_id] = sets
            sets.views = [SetView(self.set_id, "Test team", "member", tuple(self.members))]
            self.stores.append(Collaboration(sets))
        self.sender, self.recipient, self.observer = self.stores

    @staticmethod
    def saved_search(data_root, search_id, *, title="Team discussion", query="Find relevant people", jd=""):
        run = data_root / "deep-search" / search_id
        write_json(run / "results.json", {"title": title, "summary": {
            "pond_chain": [{"run": search_id, "pond_n": 1, "query": query}], "groups": {}}})
        if jd:
            (run / "jd.txt").write_text(jd, encoding="utf-8")

    def conversation(self):
        search_id = f"synthetic-search-{uuid4()}"
        self.saved_search(self.sender.sets.data_root, search_id)
        return self.sender.create(CreateConversation(set_id=self.set_id, search_id=search_id, title="Team discussion"))

    def question(self):
        conversation = self.conversation()
        sent = self.sender.send(SendMessage(conversation_id=conversation.id, text="Who can help?",
                                            recipient_id=self.members[1].operator_id))
        self.recipient.settle()
        return sent, sent.messages[0]

    def test_chat_broadcast_persisted_and_idempotent(self):
        conversation = self.conversation()
        sent = self.sender.send(SendMessage(conversation_id=conversation.id, text="Hello team"))
        for store in [self.recipient, self.observer]:
            store.settle()
            store.settle()
            restored = Collaboration(store.sets).snapshot()["conversations"][0]
            self.assertEqual(restored["messages"], sent.model_dump(exclude_none=True)["messages"])
            self.assertEqual(store.pending(), {"questions": []})
        self.assertEqual(len(self.sender.sets.sent), 2)

    def test_search_identity_required_and_creation_idempotent_per_set(self):
        for fields in [{}, {"search_id": ""}, {"search_id": "x" * 201}, {"search_id": "valid", "kind": "chat"}]:
            with self.assertRaises(ValidationError):
                CreateConversation.model_validate({"set_id": self.set_id, "title": "Search", **fields})
        request = CreateConversation(set_id=self.set_id, search_id="synthetic-native-thread", title="Original title", query="Original brief")
        for store in [self.sender, self.recipient]:
            self.saved_search(store.sets.data_root, request.search_id, title="Original title", query="Original brief")
        original = self.sender.create(request)
        repeated = self.sender.create(request.model_copy(update={"title": "Changed title", "query": "Changed brief"}))
        independently_created = self.recipient.create(request)
        self.assertEqual((repeated.id, repeated.title, repeated.query), (original.id, "Original title", "Search queries:\nOriginal brief"))
        self.assertEqual(independently_created.id, original.id)
        self.assertEqual(original.kind, "search")
        self.sender.sets.views.append(SetView(self.other_set, "Other team", "member", tuple(self.members)))
        other = self.sender.create(request.model_copy(update={"set_id": self.other_set}))
        self.assertNotEqual(other.id, original.id)
        self.assertEqual(self.recipient.snapshot()["conversations"][0]["search_id"], "synthetic-native-thread")

    def test_creation_uses_saved_title_queries_and_jd_only(self):
        search_id = "synthetic-authoritative-search"
        self.saved_search(self.sender.sets.data_root, search_id, title="Saved engineering search",
                          query="Distributed systems engineer", jd="Build distributed services.")
        created = self.sender.create(CreateConversation(set_id=self.set_id, search_id=search_id,
                                                        title="Browser supplied title", query="Browser supplied query"))
        self.assertEqual(created.title, "Saved engineering search")
        self.assertEqual(created.query, "Job description:\nBuild distributed services.\n\nSearch queries:\nDistributed systems engineer")
        self.sender.send(SendMessage(conversation_id=created.id, text="Who can help?", recipient_id=self.members[1].operator_id))
        payload = self.sender.sets.sent[0][1]["payload"]
        self.assertEqual(payload["title"], created.title)
        self.assertEqual(payload["query"], created.query)
        self.assertEqual(set(payload), {"set_id", "search_id", "title", "kind", "query", "conversation_id", "message"})
        self.assertNotIn("candidates", payload)
        self.assertNotIn("profiles", payload)
        self.assertEqual(self.recipient.pending()["questions"][0]["query"], created.query)

    def test_received_search_preserved_without_artifacts_unknown_local_search_rejected(self):
        conversation, _ = self.question()
        self.assertFalse((self.recipient.sets.data_root / "deep-search" / conversation.search_id).exists())
        received = self.recipient.create(CreateConversation(set_id=self.set_id, search_id=conversation.search_id,
                                                           title="Spoofed title", query="Spoofed brief"))
        self.assertEqual((received.id, received.title, received.query), (conversation.id, conversation.title, conversation.query))
        for search_id in ["missing-local-search", "../not-a-search"]:
            with self.assertRaises(FileNotFoundError):
                self.sender.create(CreateConversation(set_id=self.set_id, search_id=search_id, title="Unknown"))
        self.assertEqual(len(self.sender.snapshot()["conversations"]), 1)

    def test_followup_inherits_recipient_and_transports_root_context(self):
        conversation, root = self.question()
        self.recipient.thread(root.id, Thread(thread_id="synthetic-root-thread"))
        self.recipient.finish(root.id, Answer(text="Casey can help."))
        self.sender.settle()
        followup = self.sender.send(SendMessage(conversation_id=conversation.id, text="Why Casey?", reply_to=root.id)).messages[-1]
        self.assertEqual((followup.reply_to, followup.recipient_id), (root.id, root.recipient_id))
        pending = self.recipient.pending()["questions"][0]
        self.assertEqual((pending["id"], pending["reply_to"], pending["search_id"]), (followup.id, root.id, conversation.search_id))
        self.assertEqual(pending["question"], "Original question: Who can help?\nOriginal answer: Casey can help.\nFollow-up: Why Casey?")
        self.recipient.thread(followup.id, Thread(thread_id="synthetic-followup-thread"))
        self.recipient.finish(followup.id, Answer(text="Casey has relevant experience."))
        self.sender.settle()
        self.observer.settle()
        for store in self.stores:
            messages = store._read(conversation.id).messages
            self.assertEqual(messages[-1].reply_to, root.id)
            self.assertEqual(messages[-1].answer, "Casey has relevant experience.")

    def test_followup_requires_same_conversation_root_and_recipient(self):
        conversation, root = self.question()
        other = self.conversation()
        with self.assertRaises(ValueError):
            self.sender.send(SendMessage(conversation_id=other.id, text="Wrong search", reply_to=root.id))
        with self.assertRaises(PermissionError):
            self.sender.send(SendMessage(conversation_id=conversation.id, text="Wrong recipient", reply_to=root.id,
                                         recipient_id=self.members[2].operator_id))
        followup = self.sender.send(SendMessage(conversation_id=conversation.id, text="Follow-up", reply_to=root.id)).messages[-1]
        with self.assertRaises(ValueError):
            self.sender.send(SendMessage(conversation_id=conversation.id, text="Nested reply", reply_to=followup.id))
        ordinary = self.sender.send(SendMessage(conversation_id=conversation.id, text="Ordinary comment")).messages[-1]
        with self.assertRaises(ValueError):
            self.sender.send(SendMessage(conversation_id=conversation.id, text="Not a question", reply_to=ordinary.id))

    def test_followups_received_with_root_before_uuid_inbox_order(self):
        conversation = self.conversation()
        root = self.sender.send(SendMessage(conversation_id=conversation.id, text="Root",
                                            recipient_id=self.members[1].operator_id)).messages[0]
        followup = self.sender.send(SendMessage(conversation_id=conversation.id, text="Follow-up", reply_to=root.id)).messages[-1]
        # Delivery and file names can differ; the message timestamps order roots before their replies.
        paths = list((self.recipient.sets.data_root / "inbox").glob("*.json"))
        for path in paths:
            raw = json.loads(path.read_text())
            raw["id"] = "ffffffff-ffff-ffff-ffff-ffffffffffff" if raw["payload"]["message"]["id"] == root.id else "00000000-0000-0000-0000-000000000000"
            write_json(path, {**raw, "applied_at": now_iso()})
            write_json(path.parent / f"{raw['id']}.json", raw)
        pending = self.recipient.pending()["questions"]
        self.assertEqual([held["id"] for held in pending], [root.id, followup.id])

    def test_received_followup_and_answer_cannot_change_search_or_root(self):
        conversation, root = self.question()
        followup = self.sender.send(SendMessage(conversation_id=conversation.id, text="Follow-up", reply_to=root.id)).messages[-1]
        self.recipient.settle()
        payload = self.sender.sets.sent[-2][1]["payload"]
        changed = {**payload, "search_id": "another-native-search", "message": {**payload["message"], "id": str(uuid4()), "request_id": str(uuid4())}}
        changed["message"]["request_id"] = changed["message"]["id"]
        self.sender.sets.message(self.members[1].operator_id, "set_question", changed)
        self.recipient.settle()
        self.assertEqual(len(self.recipient._read(conversation.id).messages), 2)
        changed = {**payload, "message": {**payload["message"], "id": str(uuid4()), "request_id": str(uuid4()),
                                            "recipient_id": self.members[2].operator_id}}
        changed["message"]["request_id"] = changed["message"]["id"]
        self.sender.sets.message(self.members[2].operator_id, "set_question", changed)
        self.observer.settle()
        self.assertEqual(len(self.observer._read(conversation.id).messages), 2)
        self.assertEqual(self.observer.pending(), {"questions": []})
        reply = {"set_id": self.set_id, "search_id": conversation.search_id, "conversation_id": conversation.id,
                 "request_id": followup.id, "status": "answered", "answer": "Wrong root"}
        self.recipient.sets.message(self.members[0].operator_id, "set_question_reply", reply)
        self.sender.settle()
        self.assertEqual(self.sender._read(conversation.id).messages[-1].status, "queued")

    def test_only_recipient_pending_answer_shared_and_no_duplicate_execution(self):
        conversation, question = self.question()
        self.assertEqual(len(self.recipient.pending()["questions"]), 1)
        self.assertEqual(self.observer.pending(), {"questions": []})
        self.assertEqual(self.observer.snapshot()["conversations"][0]["messages"][0]["text"], "Who can help?")
        self.recipient.thread(question.id, Thread(thread_id="synthetic-thread"))
        self.recipient.thread(question.id, Thread(thread_id="synthetic-thread"))
        with self.assertRaises(ValueError):
            self.recipient.thread(question.id, Thread(thread_id="different-thread"))
        restored = Collaboration(self.recipient.sets)
        self.assertEqual(restored.pending()["questions"][0]["thread_id"], "synthetic-thread")
        restored.finish(question.id, Answer(text="Casey can help."))
        restored.finish(question.id, Answer(text="Casey can help."))
        self.assertEqual(len(self.recipient.sets.sent), 1)
        self.sender.settle()
        self.sender.settle()
        self.observer.settle()
        for store in self.stores:
            answer = store._read(conversation.id).messages[0]
            self.assertEqual((answer.status, answer.answer), ("answered", "Casey can help."))
        self.assertEqual(self.recipient.pending(), {"questions": []})
        with self.assertRaises(ValueError):
            restored.thread(question.id, Thread(thread_id="synthetic-thread"))

    def test_selected_set_membership_required_for_send_receive_and_execution(self):
        conversation = self.conversation()
        outsider = Member("Morgan Example", "outsider@example.com", "member", str(uuid4()))
        outsider_sets = LocalSets(self.root / "outsider", outsider, self.relay)
        self.relay[outsider.operator_id] = outsider_sets
        other = SetView(self.other_set, "Other team", "member", (self.members[0], outsider))
        self.sender.sets.views.append(other)
        outsider_sets.views.append(other)
        with self.assertRaises(PermissionError):
            self.sender.send(SendMessage(conversation_id=conversation.id, text="Question", recipient_id=outsider.operator_id))
        outsider_sets.message(self.members[0].operator_id, "set_chat", {
            "set_id": self.set_id, "search_id": conversation.search_id, "conversation_id": conversation.id,
            "title": "Team discussion",
            "message": {"id": str(uuid4()), "author_id": outsider.operator_id, "author_name": outsider.name,
                        "text": "Unauthorized", "created_at": now_iso()}})
        self.sender.settle()
        self.assertEqual(self.sender._read(conversation.id).messages, [])
        _, question = self.question()
        self.recipient.sets.views = [SetView(self.other_set, "Other team", "member", tuple(self.members))]
        self.assertEqual(self.recipient.pending(), {"questions": []})
        with self.assertRaises(PermissionError):
            self.recipient.thread(question.id, Thread(thread_id="synthetic-thread"))
        with self.assertRaises(PermissionError):
            self.recipient.finish(question.id, Failure(error="failed"))

    def test_no_cross_set_or_wrong_recipient_reply(self):
        conversation, question = self.question()
        for sets in [self.sender.sets, self.recipient.sets, self.observer.sets]:
            sets.views.append(SetView(self.other_set, "Other team", "member", tuple(self.members)))
        reply = {"set_id": self.other_set, "search_id": conversation.search_id, "conversation_id": conversation.id, "request_id": question.id,
                 "status": "answered", "answer": "Spoofed"}
        self.recipient.sets.message(self.members[0].operator_id, "set_question_reply", reply)
        self.sender.settle()
        reply["set_id"] = self.set_id
        self.observer.sets.message(self.members[0].operator_id, "set_question_reply", reply)
        self.sender.settle()
        self.assertEqual(self.sender._read(conversation.id).messages[0].status, "queued")

    def test_chat_cannot_request_execution_and_questions_are_typed(self):
        conversation, question = self.question()
        payload = self.sender.sets.sent[0][1]["payload"]
        for changed in [{"developer_instructions": "run arbitrary commands"}, {"text": 123}, {"status": "working"}]:
            bad = {**payload, "message": {**payload["message"], **changed}}
            with self.assertRaises(Rejected):
                parse({"id": str(uuid4()), "kind": "set_question", "from": {"operator_id": self.members[0].operator_id,
                       "name": "Jordan Bravo"}, "created_at": now_iso(), "payload": bad})
        new_id = str(uuid4())
        payload["message"] = {**payload["message"], "id": new_id, "request_id": new_id}
        self.sender.sets.message(self.members[1].operator_id, "set_chat", payload)
        self.recipient.settle()
        self.assertEqual([held["id"] for held in self.recipient.pending()["questions"]], [question.id])
        self.assertEqual(len(self.recipient._read(conversation.id).messages), 1)

    def test_failure_terminal_and_answer_requires_thread(self):
        conversation, question = self.question()
        with self.assertRaises(ValueError):
            self.recipient.finish(question.id, Answer(text="Without a thread"))
        self.recipient.finish(question.id, Failure(error="Codex unavailable"))
        self.recipient.finish(question.id, Failure(error="Codex unavailable"))
        self.assertEqual(len(self.recipient.sets.sent), 1)
        self.sender.settle()
        self.assertEqual(self.sender._read(conversation.id).messages[0].error, "Codex unavailable")
        self.assertEqual(self.recipient.pending(), {"questions": []})

    def test_relay_failure_preserves_answer_for_delivery_retry_without_inference(self):
        _, question = self.question()
        self.recipient.thread(question.id, Thread(thread_id="synthetic-thread"))
        message = self.recipient.sets.message
        def offline(*args):
            raise CloudError("relay offline")
        self.recipient.sets.message = offline
        with self.assertRaises(CloudError):
            self.recipient.finish(question.id, Answer(text="Saved answer"))
        self.assertEqual(self.recipient.pending()["questions"][0]["thread_id"], "synthetic-thread")
        result = self.recipient.snapshot()["conversations"][0]["messages"][0]
        self.assertEqual((result["status"], result["answer"]), ("working", "Saved answer"))
        self.recipient.sets.message = message
        self.recipient.finish(question.id, Answer(text="Different retry text must not replace the result"))
        self.recipient.finish(question.id, Answer(text="Saved answer"))
        self.assertEqual(len(self.recipient.sets.sent), 1)
        self.sender.settle()
        self.assertEqual(self.sender.snapshot()["conversations"][0]["messages"][0]["answer"], "Saved answer")
        self.assertEqual(self.recipient.pending(), {"questions": []})

    def test_partial_send_keeps_original_for_reply_authorization(self):
        conversation = self.conversation()
        message = self.sender.sets.message
        def partially_offline(to, kind, payload):
            if to == self.members[2].operator_id:
                raise CloudError("observer offline")
            return message(to, kind, payload)
        self.sender.sets.message = partially_offline
        with self.assertRaises(CloudError):
            self.sender.send(SendMessage(conversation_id=conversation.id, text="Saved first",
                                         recipient_id=self.members[1].operator_id))
        self.assertEqual(self.sender._read(conversation.id).messages[0].text, "Saved first")
        self.assertEqual(self.sender._read(conversation.id).messages[0].status, "unsent")
        self.assertEqual(len(self.recipient.pending()["questions"]), 1)
        original = self.sender._read(conversation.id).messages[0]
        self.sender.sets.message = message
        self.sender.resend(ResendMessage(conversation_id=conversation.id, message_id=original.id))
        self.assertEqual(len(self.recipient.pending()["questions"]), 1)
        self.assertEqual(self.recipient.pending()["questions"][0]["id"], original.id)
        self.assertEqual(self.sender._read(conversation.id).messages[0].status, "queued")
        self.assertEqual(len(self.observer.snapshot()["conversations"][0]["messages"]), 1)
        self.recipient.thread(original.id, Thread(thread_id="synthetic-thread"))
        self.recipient.finish(original.id, Answer(text="No duplicate execution"))
        self.sender.settle()
        self.assertEqual(self.sender._read(conversation.id).messages[0].answer, "No duplicate execution")

    def test_failure_before_thread_retries_only_saved_failure_delivery(self):
        conversation, question = self.question()
        message = self.recipient.sets.message
        def offline(*args):
            raise CloudError("relay offline")
        self.recipient.sets.message = offline
        with self.assertRaises(CloudError):
            self.recipient.finish(question.id, Failure(error="Codex thread could not start"))
        pending = self.recipient.pending()["questions"][0]
        self.assertEqual(pending["error"], "Codex thread could not start")
        self.assertNotIn("thread_id", pending)
        with self.assertRaises(ValueError):
            self.recipient.thread(question.id, Thread(thread_id="must-not-execute"))
        self.recipient.sets.message = message
        self.recipient.finish(question.id, Failure(error="Different retry error"))
        self.recipient.finish(question.id, Failure(error="Different retry error"))
        self.assertEqual(len(self.recipient.sets.sent), 1)
        self.sender.settle()
        self.assertEqual(self.sender._read(conversation.id).messages[0].error, "Codex thread could not start")
        self.assertEqual(self.recipient.pending(), {"questions": []})

    def test_real_http_routes_and_origin_protection(self):
        self.saved_search(self.sender.sets.data_root, "synthetic-http-search", title="HTTP team", query="Engineer")
        routes = CollaborationRoutes(self.sender)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                routes.get(self, urllib.parse.urlparse(self.path))
            def do_POST(self):
                routes.post(self, urllib.parse.urlparse(self.path))
            def log_message(self, *args):
                pass
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = http.client.HTTPConnection(*server.server_address)
                body = json.dumps({"set_id": self.set_id, "search_id": "synthetic-http-search", "title": "HTTP team", "query": "Engineer"})
                client.request("POST", "/api/collaboration/conversations", body, {"Origin": "https://evil.example"})
                response = client.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                client.request("POST", "/api/collaboration/conversations", body)
                response = client.getresponse()
                conversation = json.loads(response.read())
                self.assertEqual((response.status, conversation["query"]), (200, "Search queries:\nEngineer"))
                client.request("POST", "/api/collaboration/messages", json.dumps({"conversation_id": conversation["id"], "text": "Hello"}))
                response = client.getresponse()
                self.assertEqual(json.loads(response.read())["messages"][0]["text"], "Hello")
                client.request("GET", "/api/collaboration")
                response = client.getresponse()
                self.assertEqual(len(json.loads(response.read())["conversations"]), 1)
                client.request("GET", "/api/collaboration/pending")
                response = client.getresponse()
                self.assertEqual(json.loads(response.read()), {"questions": []})
                client.request("POST", "/api/collaboration/messages", json.dumps({
                    "conversation_id": conversation["id"], "text": "HTTP question", "recipient_id": self.members[1].operator_id}))
                response = client.getresponse()
                question = json.loads(response.read())["messages"][-1]
                routes.collaboration = self.recipient
                client.request("GET", "/api/collaboration/pending")
                response = client.getresponse()
                pending = json.loads(response.read())["questions"][0]
                self.assertEqual((pending["id"], pending["query"]), (question["id"], "Search queries:\nEngineer"))
                client.request("POST", f"/api/collaboration/questions/{question['id']}/thread", json.dumps({"thread_id": "synthetic-http-thread"}))
                response = client.getresponse()
                working = next(held for held in json.loads(response.read())["messages"] if held["id"] == question["id"])
                self.assertEqual(working["status"], "working")
                client.request("POST", f"/api/collaboration/questions/{question['id']}/answer", json.dumps({"text": "HTTP answer"}))
                response = client.getresponse()
                answered = next(held for held in json.loads(response.read())["messages"] if held["id"] == question["id"])
                self.assertEqual(answered["answer"], "HTTP answer")
                routes.collaboration = self.sender
                client.request("GET", "/api/collaboration")
                response = client.getresponse()
                self.assertEqual(json.loads(response.read())["conversations"][0]["messages"][-1]["status"], "answered")
                client.close()
            finally:
                server.shutdown()
                thread.join()

    def test_mounted_server_routes_with_real_sets_sqlite(self):
        root = self.root / "mounted"
        self.saved_search(root / ".powerpacks", "synthetic-mounted-search", title="Mounted artifact title")
        conn = open_store(root / local_server.STORE)
        queries_share.insert_set(conn, self.set_id, "Test team", "member",
                                 json.dumps([asdict(member) for member in self.members]), now_iso())
        conn.close()
        with patch("packs.ingestion.primitives.deep_context_v2.openai.load_env"), \
                patch("packs.ingestion.primitives.share.web.logbook.default_paths",
                      return_value={"gmail": root / "synthetic-gmail.sqlite"}), \
                patch("packs.shared.web.asks_loop.run"), \
                patch.object(Sets, "me", return_value=self.members[0]), \
                patch.object(Sets, "message") as relay, \
                ThreadingHTTPServer(("127.0.0.1", 0), local_server.persistent_handler(root)) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = http.client.HTTPConnection(*server.server_address)
                client.request("GET", "/api/collaboration")
                response = client.getresponse()
                snapshot = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(snapshot["sets"][0]["set_id"], self.set_id)
                client.request("POST", "/api/collaboration/conversations", json.dumps({
                    "set_id": self.set_id, "search_id": "synthetic-mounted-search", "title": "Mounted HTTP"}))
                response = client.getresponse()
                conversation = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(conversation["title"], "Mounted artifact title")
                client.request("POST", "/api/collaboration/messages", json.dumps({
                    "conversation_id": conversation["id"], "text": "Synthetic mounted message"}))
                response = client.getresponse()
                self.assertEqual(json.loads(response.read())["messages"][0]["text"], "Synthetic mounted message")
                self.assertEqual(response.status, 200)
                self.assertEqual(relay.call_count, 2)
                client.request("GET", "/searches")
                response = client.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
                client.close()
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
