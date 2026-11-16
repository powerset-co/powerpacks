"""The agent messages Powerpacks understands: one frozen dataclass per kind, and the inbox's gate.

The relay carries any kind; this file is the list a laptop accepts. `parse` turns one relay message into
an Envelope holding a typed payload, or raises Rejected: an unknown kind, a missing or mistyped field, a
field over its size limit, or an id that is not a uuid (the id names the inbox file). agent_inbox acks
every message and keeps only what parses, so anything else is discarded.

Changelog:
- 2026-11-16: only support requests and results remain; the set, ask, debug and set-chat kinds left with
  local sets (networks upload to the cloud).
- 2026-10-09: parse ask answers step by step.
- 2026-10-09: an ask carries the search's role (title, company, job description).
- 2026-10-09: an answer's reason and relationship take up to MAX_TEXT (500) characters.
- 2026-10-08: set_members carries the owner's full member list; typed answers and debug results.
- 2026-10-08: created: set invites, replies, deletes and leaves; asks and their answers; debug checks.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Union

MAX_TEXT = 500           # a thread id, a name
MAX_RESULT = 25_000      # a support request's text, or its result


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


def _dict(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Rejected(f"{name} must be an object")
    return value


@dataclass(frozen=True)
class SupportRequest:
    text: str
    thread_id: str

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> SupportRequest:
        return cls(_text(payload.get("text"), "text", limit=MAX_RESULT),
                   _text(payload.get("thread_id"), "thread_id"))


@dataclass(frozen=True)
class SupportResult:
    text: str
    thread_id: str
    request_id: str

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> SupportResult:
        return cls(_text(payload.get("text"), "text", limit=MAX_RESULT),
                   _text(payload.get("thread_id"), "thread_id"),
                   _uuid(payload.get("request_id"), "request_id"))


Payload = Union[SupportRequest, SupportResult]
KINDS: dict[str, type[Payload]] = {"support_request": SupportRequest, "support_result": SupportResult}


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
