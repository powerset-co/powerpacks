"""Typed memory inputs, claims and query results."""
from __future__ import annotations

import json
import math
from array import array
from builtins import object as JsonValue
from dataclasses import asdict, dataclass, field
from datetime import date
from calendar import monthrange
import re
from enum import StrEnum
from typing import Protocol

from packs.ingestion.primitives.common.jsonio import short_hash


class Kind(StrEnum):
    STATEMENT = "statement"
    MENTION = "mention"
    DISCUSSION = "discussion"
    WORK = "work"
    RELATIONSHIP = "relationship"
    LP = "lp"
    PREFERENCE = "preference"
    TENURE = "tenure"
    TAG = "tag"


def normalized(text: str) -> str:
    return " ".join(text.casefold().split())


@dataclass(frozen=True)
class Claim:
    text: str
    kind: Kind
    topic: str
    scope: str
    object: str
    start: str
    end: str
    quote: str
    aliases: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: JsonValue) -> Claim:
        row = object_dict(value)
        text = string(row["text"])
        topic = string(row["topic"])
        start, end = string(row["start"]), string(row["end"])
        if start:
            date_bound(start)
        if end:
            date_bound(end, upper=True)
        if start and end and date_bound(start) > date_bound(end, upper=True):
            raise ValueError("tenure starts after it ends")
        if not text.strip() or not topic.strip():
            raise ValueError("claim text and topic must be nonempty")
        return cls(text=text, kind=Kind(string(row["kind"])), topic=topic, scope=string(row["scope"]),
                   object=string(row["object"]), start=start, end=end, quote=string(row["quote"]), aliases=strings(row["aliases"]))

    def serialized(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)

    def fingerprint(self) -> str:
        return short_hash(json.dumps((normalized(self.text), self.kind, normalized(self.topic), normalized(self.scope),
                                      normalized(self.object), self.start, self.end)), n=64)


def date_bound(value: str, *, upper: bool = False) -> str:
    if value == "present":
        if not upper:
            raise ValueError("present is only an end date")
        return "9999-12-31"
    if not re.fullmatch(r"\d{4}(-\d{2}){0,2}", value):
        raise ValueError("invalid claim date")
    parts = [int(part) for part in value.split("-")]
    year = parts[0]
    month = parts[1] if len(parts) > 1 else (12 if upper else 1)
    day = parts[2] if len(parts) > 2 else (monthrange(year, month)[1] if upper else 1)
    return date(year, month, day).isoformat()


def object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("expected JSON object")
    return dict(value)


def string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected string")
    return value


def strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("expected string list")
    return tuple(string(item) for item in value)


def vector(value: object) -> tuple[float, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("expected nonempty embedding")
    result: list[float] = []
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(item):
            raise ValueError("invalid embedding value")
        result.append(float(item))
    norm = math.sqrt(sum(item * item for item in result))
    if norm == 0:
        raise ValueError("zero embedding")
    return tuple(item / norm for item in result)


def packed(values: tuple[float, ...] | list[float]) -> bytes:
    """Float32 little-endian blob: a quarter of the JSON text, parsed in C."""
    return array("f", values).tobytes()


def cached_vector(value: bytes) -> array[float]:
    values = array("f")
    values.frombytes(value)
    return values


def document(claim: Claim) -> str:
    """The text that is embedded for a claim; its hash keys the embeddings table."""
    return "\n".join((claim.text, claim.kind, claim.topic, claim.scope, claim.object, claim.start, claim.end))


@dataclass(frozen=True)
class Identity:
    member: str
    parent: str
    name: str
    identifiers: tuple[str, ...]


@dataclass(frozen=True)
class Source:
    key: str
    member: str
    citation: str
    content: str
    error: str = ""


@dataclass(frozen=True)
class IdentityMatch:
    parent: str
    name: str


@dataclass(frozen=True)
class SourceRevision:
    id: int
    hash: str
    content: str


@dataclass(frozen=True)
class OpenedClaim:
    claim: Claim
    human: bool
    active: bool
    source: str
    source_hash: str
    saved_content: str
    latest_read_error: str
    original_message_citation_verified: bool = False


@dataclass
class ImportReport:
    failures: tuple[tuple[str, str], ...] = ()
    members: int = 0
    parents: int = 0
    sources: int = 0
    changed: int = 0
    failed_reads: int = 0
    unavailable: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Filters:
    parents: tuple[str, ...] = ()
    kind: Kind | None = None
    topic: str = ""
    scope: str = ""
    object: str = ""


@dataclass(frozen=True)
class Citation:
    source: str
    source_hash: str
    quote: str
    level: str = "derived artifact; original message citation unverified"


@dataclass(frozen=True)
class Hit:
    claim_id: int
    parent: str
    name: str
    claim: Claim
    score: float
    lexical_rank: int | None
    vector_rank: int | None
    citations: tuple[Citation, ...]


@dataclass(frozen=True)
class PersonHit:
    """One person in a search answer: their score, warmth (0-4 from Deep Context) and the claims that matched."""
    parent: str
    name: str
    warmth: float
    score: float
    evidence: tuple[Hit, ...]
    members: tuple[str, ...]
    context: tuple[Hit, ...] = ()
    context_truncated: bool = False


@dataclass(frozen=True)
class People:
    """The one search answer the page and the agent both read.
    `layers` are the owner labels the text named; with other words present they filter, alone they list everyone."""
    people: tuple[PersonHit, ...]
    layers: tuple[str, ...]
    filtered: bool
    total: int
    lexical_candidates: int
    vector_candidates: int
    missing_embeddings: int


class Provider(Protocol):
    generation_key: str
    embedding_key: str

    def generate(self, content: str) -> str: ...

    def embed(self, content: str) -> tuple[float, ...]: ...

    def embed_many(self, contents: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


def response_json(raw: str) -> dict[str, object]:
    payload = object_dict(json.loads(raw))
    if payload.get("status") != "completed":
        raise ValueError("generation incomplete")
    output = payload["output"]
    if not isinstance(output, list):
        raise ValueError("invalid response output")
    for item in output:
        row = object_dict(item)
        if row.get("type") != "message":
            continue
        parts = row["content"]
        if not isinstance(parts, list):
            raise ValueError("invalid response content")
        for part in parts:
            block = object_dict(part)
            if block.get("type") == "output_text":
                return object_dict(json.loads(string(block["text"])))
    raise ValueError("generation returned no output")

