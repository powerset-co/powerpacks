"""The agent messages Powerpacks understands: one frozen dataclass per kind, and the inbox's gate.

The relay carries any kind; this file is the list a laptop accepts. `parse` turns one relay message into
an Envelope holding a typed payload, or raises Rejected: an unknown kind, a missing or mistyped field, a
field over its size limit, or an id that is not a uuid (the id names the inbox file). agent_inbox acks
every message and keeps only what parses, so anything else is discarded.

Changelog:
- 2026-10-09: an ask carries the search's role (title, company, job description).
- 2026-10-08: set_members carries the owner's full member list; typed answers and debug results.
- 2026-10-08: created: set invites, replies, deletes and leaves; asks and their answers; debug checks.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, Union

MAX_TEXT = 500           # a question, a set name, an email
MAX_CANDIDATES = 50      # pinned candidates in one ask
MAX_MEMBERS = 50         # people in one set
MAX_JD = 8_000          # the role's job description in an ask (ask_set trims to it)
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
class SetMember:
    name: str
    email: str
    role: Literal["owner", "member"]
    operator_id: str

    @classmethod
    def parse(cls, value: object) -> SetMember:
        row = _dict(value, "member")
        role = row.get("role")
        if role not in ("owner", "member"):
            raise Rejected("role must be owner or member")
        return cls(_text(row.get("name") or "", "name", empty=True), _text(row.get("email") or "", "email", empty=True),
                   role, _uuid(row.get("operator_id"), "operator_id"))


@dataclass(frozen=True)
class SetMembers:
    """The owner's full member list, sent to every member when someone joins or leaves; honoured only from
    the set's owner (sets.py)."""
    set_id: str
    members: tuple[SetMember, ...]

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> SetMembers:
        return cls(_uuid(payload.get("set_id"), "set_id"),
                   tuple(SetMember.parse(row) for row in _list(payload.get("members"), "members", MAX_MEMBERS)))


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
class Role:
    """The search the candidates came from: what they are being considered for."""
    title: str
    company: str
    job_description: str

    @classmethod
    def parse(cls, value: object) -> Role:
        row = _dict(value, "role")
        return cls(_text(row.get("title"), "title"), _text(row.get("company"), "company", empty=True),
                   _text(row.get("job_description"), "job_description", limit=MAX_JD, empty=True))


@dataclass(frozen=True)
class Ask:
    """Would you recommend these people for this role? Answered from local evidence, only for a set member
    (asks_loop)."""
    ask_id: str
    question: str
    role: Role
    candidates: tuple[AskCandidate, ...]

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> Ask:
        return cls(_uuid(payload.get("ask_id"), "ask_id"), _text(payload.get("question"), "question"),
                   Role.parse(payload.get("role")),
                   tuple(AskCandidate.parse(row) for row in
                         _list(payload.get("candidates"), "candidates", MAX_CANDIDATES)))


@dataclass(frozen=True)
class Verdict:
    """One owner's answer about one candidate (ask_worker's model answer)."""
    verdict: Literal["recommend", "not_fit", "unsure"]
    reason: str
    can_intro: bool
    relationship: str
    last_contact: str | None
    confidence: float

    @classmethod
    def parse(cls, value: object) -> Verdict:
        row = _dict(value, "answer")
        verdict, can_intro, confidence = row.get("verdict"), row.get("can_intro"), row.get("confidence")
        if verdict not in ("recommend", "not_fit", "unsure"):
            raise Rejected("verdict must be recommend, not_fit or unsure")
        if not isinstance(can_intro, bool):
            raise Rejected("can_intro must be true or false")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise Rejected("confidence must be a number from 0 to 1")
        last = row.get("last_contact")
        return cls(verdict, _text(row.get("reason"), "reason", limit=240, empty=True), can_intro,
                   _text(row.get("relationship"), "relationship", limit=120, empty=True),
                   None if last is None else _text(last, "last_contact", limit=7), float(confidence))


@dataclass(frozen=True)
class Declined:
    """The owner could not answer: the person is not in their store, or their model call failed."""
    declined: Literal[True]
    reason: Literal["not_in_store", "failed"]


def _answer_body(value: object) -> Verdict | Declined:
    row = _dict(value, "answer")
    if row.get("declined") is True:
        if row.get("reason") not in ("not_in_store", "failed"):
            raise Rejected("a declined answer's reason must be not_in_store or failed")
        return Declined(True, row["reason"])
    return Verdict.parse(row)


@dataclass(frozen=True)
class CandidateAnswer:
    public_identifier: str
    answer: Verdict | Declined


@dataclass(frozen=True)
class AskAnswer:
    ask_id: str
    answers: tuple[CandidateAnswer, ...]

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> AskAnswer:
        return cls(_uuid(payload.get("ask_id"), "ask_id"), tuple(
            CandidateAnswer(_text(_dict(row, "answer").get("public_identifier"), "public_identifier"),
                            _answer_body(row.get("answer")))
            for row in _list(payload.get("answers"), "answers", MAX_CANDIDATES)))


@dataclass(frozen=True)
class DebugRequest:
    """Run the named read-only checks (all when none are named); answered only for a set member."""
    checks: tuple[str, ...]

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> DebugRequest:
        return cls(tuple(_text(name, "check", limit=40) for name in _list(payload.get("checks", []), "checks", 20)))


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    output: str


@dataclass(frozen=True)
class DebugResult:
    request_id: str
    results: dict[str, CheckResult]

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> DebugResult:
        results = _dict(payload.get("results"), "results")
        if len(results) > 20:
            raise Rejected("results must hold at most 20 checks")
        typed = {}
        for name, result in results.items():
            row = _dict(result, "result")
            if not isinstance(row.get("ok"), bool):
                raise Rejected("ok must be true or false")
            typed[_text(name, "check", limit=40)] = CheckResult(
                row["ok"], _text(row.get("output"), "output", limit=MAX_RESULT, empty=True))
        return cls(_uuid(payload.get("request_id"), "request_id"), typed)


Payload = Union[SetInvite, SetInviteReply, SetDeleted, SetLeft, SetMembers, Ask, AskAnswer, DebugRequest,
                DebugResult]
KINDS: dict[str, type[Payload]] = {
    "set_invite": SetInvite, "set_invite_reply": SetInviteReply, "set_deleted": SetDeleted, "set_left": SetLeft,
    "set_members": SetMembers,
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
