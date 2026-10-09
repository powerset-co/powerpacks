"""The agent messages Powerpacks understands: one frozen dataclass per kind, and the inbox's gate.

The relay carries any kind; this file is the list a laptop accepts. `parse` turns one relay message into
an Envelope holding a typed payload, or raises Rejected: an unknown kind, a missing or mistyped field, a
field over its size limit, or an id that is not a uuid (the id names the inbox file). agent_inbox acks
every message and keeps only what parses, so anything else is discarded.

Changelog:
- 2026-10-08: created: set invites, replies, deletes and leaves; asks and their answers; debug checks.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, Union

MAX_TEXT = 500           # a question, a set name, an email
MAX_CANDIDATES = 50      # pinned candidates in one ask
MAX_RESULT = 25_000      # one debug check's output (agent_debug caps at 20,000)


class Rejected(ValueError):
    """The message is not one this laptop understands; the reason is safe to log."""


def _text(value: object, name: str, *, limit: int = MAX_TEXT, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value):
        raise Rejected(f"{name} must be a string of 1-{limit} characters")
    return value


def _uuid(value: object, name: str) -> str:
    try:
        return str(uuid.UUID(_text(value, name)))
    except ValueError as error:
        raise Rejected(f"{name} must be a uuid") from error


def _list(value: object, name: str, limit: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise Rejected(f"{name} must be a list of at most {limit}")
    return value


def _dict(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Rejected(f"{name} must be an object")
    return value


@dataclass(frozen=True)
class SetInvite:
    """An invite to a set; accepting stores the set on the invitee's laptop."""
    set_id: str
    set_name: str
    from_email: str

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> SetInvite:
        return cls(_uuid(payload.get("set_id"), "set_id"), _text(payload.get("set_name"), "set_name"),
                   _text(payload.get("from_email", ""), "from_email", empty=True))


@dataclass(frozen=True)
class SetInviteReply:
    invite_id: str
    answer: Literal["accepted", "declined"]

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> SetInviteReply:
        answer = payload.get("answer")
        if answer not in ("accepted", "declined"):
            raise Rejected("answer must be accepted or declined")
        return cls(_uuid(payload.get("invite_id"), "invite_id"), answer)


@dataclass(frozen=True)
class SetDeleted:
    """The owner deleted the set; honoured only from the set's owner (sets.py)."""
    set_id: str

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> SetDeleted:
        return cls(_uuid(payload.get("set_id"), "set_id"))


@dataclass(frozen=True)
class SetLeft:
    """A member left the set."""
    set_id: str

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> SetLeft:
        return cls(_uuid(payload.get("set_id"), "set_id"))


@dataclass(frozen=True)
class AskCandidate:
    public_identifier: str
    linkedin_url: str
    name: str

    @classmethod
    def parse(cls, value: object) -> AskCandidate:
        row = _dict(value, "candidate")
        return cls(_text(row.get("public_identifier"), "public_identifier"),
                   _text(row.get("linkedin_url"), "linkedin_url"), _text(row.get("name"), "name"))


@dataclass(frozen=True)
class Ask:
    """Would you recommend these people? Answered from local evidence, only for a set member (asks_loop)."""
    ask_id: str
    question: str
    candidates: tuple[AskCandidate, ...]

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> Ask:
        return cls(_uuid(payload.get("ask_id"), "ask_id"), _text(payload.get("question"), "question"),
                   tuple(AskCandidate.parse(row) for row in
                         _list(payload.get("candidates"), "candidates", MAX_CANDIDATES)))


@dataclass(frozen=True)
class AskAnswer:
    ask_id: str
    answers: tuple[dict[str, Any], ...]  # {public_identifier, answer}; read only against the asker's own ask.json

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> AskAnswer:
        answers = tuple(_dict(row, "answer") for row in _list(payload.get("answers"), "answers", MAX_CANDIDATES))
        for row in answers:
            _text(row.get("public_identifier"), "public_identifier")
            _dict(row.get("answer"), "answer")
        return cls(_uuid(payload.get("ask_id"), "ask_id"), answers)


@dataclass(frozen=True)
class DebugRequest:
    """Run the named read-only checks (all when none are named); answered only for a set member."""
    checks: tuple[str, ...]

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> DebugRequest:
        return cls(tuple(_text(name, "check", limit=40) for name in _list(payload.get("checks", []), "checks", 20)))


@dataclass(frozen=True)
class DebugResult:
    request_id: str
    results: dict[str, Any]  # {check: {ok, output}}

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> DebugResult:
        results = _dict(payload.get("results"), "results")
        for name, result in results.items():
            _text(name, "check", limit=40)
            _text(_dict(result, "result").get("output"), "output", limit=MAX_RESULT, empty=True)
        return cls(_uuid(payload.get("request_id"), "request_id"), results)


Payload = Union[SetInvite, SetInviteReply, SetDeleted, SetLeft, Ask, AskAnswer, DebugRequest, DebugResult]
KINDS: dict[str, type[Payload]] = {
    "set_invite": SetInvite, "set_invite_reply": SetInviteReply, "set_deleted": SetDeleted, "set_left": SetLeft,
    "ask": Ask, "ask_answer": AskAnswer, "debug_request": DebugRequest, "debug_result": DebugResult,
}


@dataclass(frozen=True)
class Envelope:
    """One message as the inbox keeps it: who sent it, when, and its typed payload."""
    id: str
    kind: str
    from_operator_id: str
    from_name: str
    created_at: str
    payload: Payload


def parse(raw: object) -> Envelope:
    """The relay's message, typed; Rejected when this laptop does not understand it."""
    message = _dict(raw, "message")
    kind = message.get("kind")
    if kind not in KINDS:
        raise Rejected(f"unknown kind {str(kind)[:40]!r}")
    sender = _dict(message.get("from"), "from")
    return Envelope(_uuid(message.get("id"), "id"), kind, _uuid(sender.get("operator_id"), "from.operator_id"),
                    _text(sender.get("name") or "", "from.name", empty=True),
                    _text(message.get("created_at"), "created_at", limit=40),
                    KINDS[kind].parse(_dict(message.get("payload"), "payload")))
