"""Search history collaboration over the existing inbox; Codex questions require an explicit desktop action."""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ValidationError

from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.share.web.sets import CloudError, Member, NeedsSignIn, Sets
from packs.powerset.primitives.agent_inbox.collaboration_messages import (
    Answer, Conversation, ConversationMessage, CreateConversation, Failure, ResendMessage, SendMessage,
    SetChat, SetQuestion, SetQuestionReply, Thread,
)
from packs.powerset.primitives.agent_inbox.messages import Rejected, parse
from packs.search.primitives.deep_search.results_web.server import SearchRoutes, search_routes

PREFIX = "/api/collaboration"
MAX_REQUEST_BYTES = 100_000
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TERMINAL = {"answered", "failed"}


def _conversation_id(set_id: str, search_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"powerpacks:set:{set_id}:search:{search_id}"))


class Collaboration:
    def __init__(self, sets: Sets, searches: SearchRoutes | None = None) -> None:
        self.sets = sets
        self.directory = sets.data_root / "conversations"
        self.searches = searches if searches is not None else search_routes(sets.data_root / "deep-search")

    def _members(self, set_id: str) -> dict[str, Member]:
        view = next((view for view in self.sets.kept() if view.set_id == set_id), None)
        if view is None:
            raise PermissionError("not a member of the selected set")
        members = {member.operator_id: member for member in self.sets.members(view)}
        if self.sets.me().operator_id not in members:
            raise PermissionError("not a member of the selected set")
        return members

    def _read(self, conversation_id: str) -> Conversation:
        path = self.directory / f"{UUID(conversation_id)}.json"
        return Conversation.model_validate_json(path.read_text(encoding="utf-8"))

    def _save(self, conversation: Conversation) -> None:
        write_json(self.directory / f"{conversation.id}.json", conversation.model_dump(exclude_none=True))

    def _all(self) -> list[Conversation]:
        return [Conversation.model_validate_json(path.read_text(encoding="utf-8"))
                for path in sorted(self.directory.glob("*.json"))]

    def _put(self, conversation: Conversation, message: ConversationMessage) -> Conversation:
        if message.reply_to is not None and message.recipient_id != self._root_question(conversation, message.reply_to).recipient_id:
            raise PermissionError("reply recipient does not match the root question")
        previous = next((held for held in conversation.messages if held.id == message.id), None)
        if previous is not None:
            if (previous.author_id, previous.text, previous.recipient_id, previous.reply_to) != (message.author_id, message.text, message.recipient_id, message.reply_to):
                raise PermissionError("message does not match the original")
            if previous.status in TERMINAL or (previous.status != "unsent" and message.status in {None, "queued"}):
                return conversation
        messages = [message if held.id == message.id else held for held in conversation.messages]
        if previous is None:
            messages.append(message)
        messages.sort(key=lambda held: (held.created_at, held.id))
        updated = conversation.model_copy(update={"messages": messages})
        self._save(updated)
        return updated

    def _broadcast(self, conversation: Conversation, message: ConversationMessage, *, skip: str | None = None) -> None:
        payload = SetChat(set_id=conversation.set_id, search_id=conversation.search_id, title=conversation.title,
                          query=conversation.query, conversation_id=conversation.id, message=message)
        me = self.sets.me().operator_id
        for operator_id in self._members(conversation.set_id):
            if operator_id not in {me, skip}:
                self.sets.message(operator_id, "set_chat", payload.model_dump(exclude_none=True))

    def settle(self) -> None:
        self.sets.settle()
        incoming = []
        for path in sorted((self.sets.data_root / "inbox").glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("kind") not in {"set_chat", "set_question", "set_question_reply"} or "applied_at" in raw:
                continue
            try:
                envelope = parse(raw)
            except Rejected as error:
                write_json(path, {**raw, "applied_at": now_iso(), "refused": str(error)})
                continue
            incoming.append((path, raw, envelope))
        incoming.sort(key=lambda held: held[2].payload.message.created_at if isinstance(held[2].payload, SetChat)
                      else held[2].created_at)
        for path, raw, envelope in incoming:
            try:
                payload = envelope.payload
                assert isinstance(payload, (SetChat, SetQuestion, SetQuestionReply))
                members = self._members(payload.set_id)
                if envelope.from_operator_id not in members:
                    raise PermissionError("sender is not in the selected set")
                try:
                    conversation = self._read(payload.conversation_id)
                except FileNotFoundError:
                    if isinstance(payload, SetQuestionReply):
                        raise PermissionError("no original question")
                    conversation = Conversation(id=payload.conversation_id, set_id=payload.set_id, search_id=payload.search_id,
                                                title=payload.title, query=payload.query)
                if conversation.set_id != payload.set_id:
                    raise PermissionError("conversation belongs to another set")
                if conversation.search_id != payload.search_id or conversation.id != _conversation_id(payload.set_id, payload.search_id):
                    raise PermissionError("conversation belongs to another search")
                if isinstance(payload, SetQuestionReply):
                    self._receive_reply(conversation, payload, envelope.from_operator_id)
                else:
                    message = payload.message
                    if message.author_id != envelope.from_operator_id:
                        raise PermissionError("message author does not match sender")
                    if message.recipient_id is not None and message.recipient_id not in members:
                        raise PermissionError("recipient is not in the selected set")
                    if isinstance(payload, SetQuestion) and message.recipient_id != self.sets.me().operator_id:
                        raise PermissionError("question belongs to another recipient")
                    if not isinstance(payload, SetQuestion) and message.recipient_id == self.sets.me().operator_id:
                        raise PermissionError("only set_question can request a Codex answer")
                    self._put(conversation, message.model_copy(update={"author_name": members[message.author_id].name}))
            except (PermissionError, ValueError) as error:
                write_json(path, {**raw, "applied_at": now_iso(), "refused": str(error)})
                continue
            write_json(path, {**raw, "applied_at": now_iso()})

    def _receive_reply(self, conversation: Conversation, reply: SetQuestionReply, sender: str) -> None:
        original = next((held for held in conversation.messages if held.id == reply.request_id), None)
        if original is None or original.author_id != self.sets.me().operator_id or original.recipient_id != sender:
            raise PermissionError("reply does not match this operator's question and recipient")
        if original.request_id != reply.request_id:
            raise PermissionError("reply does not match the original request")
        if original.reply_to != reply.reply_to:
            raise PermissionError("reply does not match the original root question")
        if original.status in TERMINAL:
            return
        message = original.model_copy(update={"status": reply.status, "answer": reply.answer, "error": reply.error})
        self._broadcast(conversation, message, skip=sender)
        self._put(conversation, message)

    def snapshot(self) -> dict[str, Any]:
        self.settle()
        me = self.sets.me()
        sets = [{"set_id": view.set_id, "name": view.name,
                 "members": [{"operator_id": member.operator_id, "name": member.name}
                             for member in self._members(view.set_id).values()]}
                for view in self.sets.kept()]
        selected = {view["set_id"] for view in sets}
        return {"sets": sets, "me": {"operator_id": me.operator_id, "name": me.name},
                "conversations": [held.model_dump(exclude_none=True) for held in self._all() if held.set_id in selected]}

    def create(self, request: CreateConversation) -> Conversation:
        self.settle()
        self._members(request.set_id)
        conversation_id = _conversation_id(request.set_id, request.search_id)
        try:
            existing = self._read(conversation_id)
        except FileNotFoundError:
            existing = None
        search = self.searches.one(request.search_id)
        if search is None:
            if existing is not None:
                return existing  # A received search need not have local candidate artifacts.
            raise FileNotFoundError("saved search not found")
        brief = []
        if search.jd_text:
            brief.append(f"Job description:\n{search.jd_text}")
        if search.queries:
            brief.append("Search queries:\n" + "\n".join(search.queries))
        query = "\n\n".join(brief) or None
        metadata = CreateConversation(set_id=request.set_id, search_id=request.search_id, title=search.title, query=query)
        conversation = (existing.model_copy(update={"title": metadata.title, "query": metadata.query}) if existing is not None
                        else Conversation(id=conversation_id, **metadata.model_dump()))
        self._save(conversation)
        return conversation

    @staticmethod
    def _root_question(conversation: Conversation, reply_to: str) -> ConversationMessage:
        root = next((held for held in conversation.messages if held.id == reply_to), None)
        if root is None or root.reply_to is not None or root.request_id != root.id or root.recipient_id is None:
            raise ValueError("reply_to must reference a root question in this search conversation")
        return root

    def send(self, request: SendMessage) -> Conversation:
        self.settle()
        conversation = self._read(request.conversation_id)
        members = self._members(conversation.set_id)
        me = self.sets.me()
        recipient_id = request.recipient_id
        if request.reply_to is not None:
            root = self._root_question(conversation, request.reply_to)
            if recipient_id is not None and recipient_id != root.recipient_id:
                raise PermissionError("reply recipient does not match the root question")
            recipient_id = root.recipient_id
        if recipient_id is not None and (recipient_id not in members or recipient_id == me.operator_id):
            raise PermissionError("select another member of this set")
        message_id = str(uuid4())
        message = ConversationMessage(id=message_id, author_id=me.operator_id, author_name=me.name,
                                      text=request.text, created_at=datetime.now(timezone.utc).isoformat(), recipient_id=recipient_id,
                                      reply_to=request.reply_to, request_id=message_id if recipient_id else None,
                                      status="unsent")
        updated = self._put(conversation, message)
        return self._deliver(updated, message)

    def _deliver(self, conversation: Conversation, message: ConversationMessage) -> Conversation:
        wire = message.model_copy(update={"status": "queued" if message.recipient_id else None, "error": None})
        try:
            if wire.recipient_id:
                payload = SetQuestion(set_id=conversation.set_id, search_id=conversation.search_id, title=conversation.title,
                                      query=conversation.query, conversation_id=conversation.id, message=wire)
                self.sets.message(wire.recipient_id, "set_question", payload.model_dump(exclude_none=True))
            self._broadcast(conversation, wire, skip=wire.recipient_id)
        except (CloudError, NeedsSignIn) as error:
            self._put(conversation, message.model_copy(update={"status": "unsent", "error": str(error)}))
            raise
        return self._put(conversation, wire)

    def resend(self, request: ResendMessage) -> Conversation:
        self.settle()
        conversation = self._read(request.conversation_id)
        members = self._members(conversation.set_id)
        message = next((held for held in conversation.messages if held.id == request.message_id), None)
        if message is None:
            raise FileNotFoundError("message not found")
        if message.author_id != self.sets.me().operator_id:
            raise PermissionError("only the sender can resend a message")
        if message.recipient_id is not None and message.recipient_id not in members:
            raise PermissionError("recipient is not in the selected set")
        if message.status != "unsent":
            return conversation
        return self._deliver(conversation, message)

    def pending(self) -> dict[str, Any]:
        self.settle()
        me = self.sets.me().operator_id
        questions = []
        for conversation in self._all():
            try:
                members = self._members(conversation.set_id)
            except PermissionError:
                continue
            for message in conversation.messages:
                if message.recipient_id == me and message.author_id in members and message.status in {"queued", "working"}:
                    question = message.text
                    if message.reply_to is not None:
                        root = self._root_question(conversation, message.reply_to)
                        question = f"Original question: {root.text}\n"
                        if root.answer is not None:
                            question += f"Original answer: {root.answer}\n"
                        question += f"Follow-up: {message.text}"
                    questions.append({"id": message.id, "set_id": conversation.set_id, "conversation_id": conversation.id,
                                      "search_id": conversation.search_id,
                                      "from_name": message.author_name, "question": question,
                                      **({"reply_to": message.reply_to} if message.reply_to else {}),
                                      **({"query": conversation.query} if conversation.query else {}),
                                      **({"answer": message.answer} if message.answer else {}),
                                      **({"error": message.error} if message.error else {}),
                                      **({"thread_id": message.thread_id} if message.thread_id else {})})
        return {"questions": questions}

    def _question(self, question_id: str) -> tuple[Conversation, ConversationMessage]:
        self.settle()
        question_id = str(UUID(question_id))
        for conversation in self._all():
            message = next((held for held in conversation.messages if held.id == question_id), None)
            if message is None:
                continue
            members = self._members(conversation.set_id)
            if (message.recipient_id != self.sets.me().operator_id or message.author_id not in members
                    or message.request_id != question_id):
                raise PermissionError("question is not addressed to this operator in this set")
            return conversation, message
        raise FileNotFoundError("question not found")

    def thread(self, question_id: str, request: Thread) -> Conversation:
        conversation, message = self._question(question_id)
        if (message.status in TERMINAL or message.answer is not None or message.error is not None
                or (message.thread_id is not None and message.thread_id != request.thread_id)):
            raise ValueError("question already has a thread or a result")
        updated = message.model_copy(update={"thread_id": request.thread_id, "status": "working"})
        # Working is local to the recipient; no inference is started by the relay daemon.
        return self._put(conversation, updated)

    def finish(self, question_id: str, request: Answer | Failure) -> Conversation:
        conversation, message = self._question(question_id)
        if message.status in TERMINAL:
            return conversation
        if isinstance(request, Answer) and (message.status != "working" or message.thread_id is None):
            raise ValueError("record the Codex thread before answering")
        answer, error = message.answer, message.error
        if answer is None and error is None:
            if isinstance(request, Answer):
                answer = request.text
            else:
                error = request.error
        status: Literal["answered", "failed"] = "answered" if answer is not None else "failed"
        saved = message.model_copy(update={"answer": answer, "error": error,
                                          "status": "working"})
        conversation = self._put(conversation, saved)
        reply = SetQuestionReply(set_id=conversation.set_id, search_id=conversation.search_id, conversation_id=conversation.id,
                                 request_id=question_id, reply_to=message.reply_to, status=status, answer=answer, error=error)
        self.sets.message(message.author_id, "set_question_reply", reply.model_dump(exclude_none=True))
        return self._put(conversation, saved.model_copy(update={"status": status}))


class CollaborationRoutes:
    def __init__(self, collaboration: Collaboration) -> None:
        self.collaboration = collaboration

    @staticmethod
    def _json(handler: BaseHTTPRequestHandler, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if parsed.path not in {PREFIX, PREFIX + "/pending"}:
            return False
        try:
            result = self.collaboration.pending() if parsed.path.endswith("/pending") else self.collaboration.snapshot()
            self._json(handler, result)
        except PermissionError as error:
            self._json(handler, {"error": str(error)}, HTTPStatus.FORBIDDEN)
        except (CloudError, NeedsSignIn) as error:
            self._json(handler, {"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
        return True

    def post(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if not parsed.path.startswith(PREFIX + "/"):
            return False
        origin = (handler.headers.get("Origin") or "").strip()
        host = (handler.headers.get("Host") or "").strip()
        scheme = "https" if getattr(handler.connection, "cipher", None) else "http"
        hostname = (urllib.parse.urlsplit(f"//{host}").hostname or "").lower()
        if origin and (origin != f"{scheme}://{host}" or hostname not in LOCAL_HOSTS):
            self._json(handler, {"error": "cross-origin request rejected"}, HTTPStatus.FORBIDDEN)
            return True
        try:
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError("invalid request size")
            body = json.loads(handler.rfile.read(length))
            if parsed.path == PREFIX + "/conversations":
                conversation = self.collaboration.create(CreateConversation.model_validate(body))
            elif parsed.path == PREFIX + "/messages":
                conversation = self.collaboration.send(SendMessage.model_validate(body))
            elif parsed.path == PREFIX + "/resend":
                conversation = self.collaboration.resend(ResendMessage.model_validate(body))
            else:
                parts = parsed.path.removeprefix(PREFIX + "/").split("/")
                if len(parts) != 3 or parts[0] != "questions" or parts[2] not in {"thread", "answer", "failed"}:
                    self._json(handler, {"error": "not found"}, HTTPStatus.NOT_FOUND)
                    return True
                question_id = str(UUID(parts[1]))
                if parts[2] == "thread":
                    conversation = self.collaboration.thread(question_id, Thread.model_validate(body))
                else:
                    request = Answer.model_validate(body) if parts[2] == "answer" else Failure.model_validate(body)
                    conversation = self.collaboration.finish(question_id, request)
            self._json(handler, conversation.model_dump(exclude_none=True))
        except PermissionError as error:
            self._json(handler, {"error": str(error)}, HTTPStatus.FORBIDDEN)
        except FileNotFoundError:
            self._json(handler, {"error": "conversation or question not found"}, HTTPStatus.NOT_FOUND)
        except (ValidationError, ValueError) as error:
            self._json(handler, {"error": "invalid request" if isinstance(error, ValidationError) else str(error)}, HTTPStatus.BAD_REQUEST)
        except (CloudError, NeedsSignIn) as error:
            self._json(handler, {"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
        return True
