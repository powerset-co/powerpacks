"""Typed shared searches and follow-ups at the relay and local HTTP boundaries."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator


def _uuid(value: str) -> str:
    return str(UUID(value))


Id = Annotated[str, AfterValidator(_uuid)]
Text = Annotated[str, Field(min_length=1, max_length=20_000)]
Title = Annotated[str, Field(min_length=1, max_length=500)]
SearchId = Annotated[str, Field(min_length=1, max_length=200)]


class Boundary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> Self:
        from packs.powerset.primitives.agent_inbox.messages import Rejected

        try:
            return cls.model_validate(payload)
        except ValidationError as error:
            raise Rejected(f"invalid {cls.__name__}") from error


class ConversationMessage(Boundary):
    id: Id
    author_id: Id
    author_name: Annotated[str, Field(max_length=500)]
    text: Text
    created_at: Annotated[str, Field(min_length=1, max_length=40)]
    request_id: Id | None = None
    recipient_id: Id | None = None
    reply_to: Id | None = None
    status: Literal["unsent", "queued", "working", "answered", "failed"] | None = None
    answer: Text | None = None
    error: Text | None = None
    thread_id: Annotated[str, Field(min_length=1, max_length=500)] | None = None


class Conversation(Boundary):
    id: Id
    set_id: Id
    search_id: SearchId
    title: Title
    kind: Literal["search"] = "search"
    query: Text | None = None
    messages: list[ConversationMessage] = Field(default_factory=list)


class CreateConversation(Boundary):
    set_id: Id
    search_id: SearchId
    title: Title
    kind: Literal["search"] = "search"
    query: Text | None = None


class SendMessage(Boundary):
    conversation_id: Id
    text: Text
    recipient_id: Id | None = None
    reply_to: Id | None = None


class ResendMessage(Boundary):
    conversation_id: Id
    message_id: Id


class SetChat(CreateConversation):
    conversation_id: Id
    message: ConversationMessage

    @model_validator(mode="after")
    def relay_message(self) -> Self:
        if self.message.status in {"unsent", "working"} or self.message.thread_id is not None:
            raise ValueError("local delivery and Codex thread state must not cross the relay")
        return self


class SetQuestion(SetChat):
    @model_validator(mode="after")
    def question(self) -> Self:
        message = self.message
        if message.request_id != message.id or message.recipient_id is None or message.status != "queued":
            raise ValueError("question must identify its queued request and recipient")
        if message.thread_id is not None or message.answer is not None or message.error is not None:
            raise ValueError("question must be unanswered")
        return self


class SetQuestionReply(Boundary):
    set_id: Id
    search_id: SearchId
    conversation_id: Id
    request_id: Id
    reply_to: Id | None = None
    status: Literal["answered", "failed"]
    answer: Text | None = None
    error: Text | None = None

    @model_validator(mode="after")
    def result(self) -> Self:
        if self.status == "answered" and (self.answer is None or self.error is not None):
            raise ValueError("answered requires only an answer")
        if self.status == "failed" and (self.error is None or self.answer is not None):
            raise ValueError("failed requires only an error")
        return self


class Thread(Boundary):
    thread_id: Annotated[str, Field(min_length=1, max_length=500)]


class Answer(Boundary):
    text: Text


class Failure(Boundary):
    error: Text
