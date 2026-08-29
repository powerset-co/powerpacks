"""Frozen rows for source-readiness probes and their CLI report."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp


@dataclass(frozen=True)
class SourceCount:
    source: str
    count: int


def source_counts(values: Mapping[str, int]) -> tuple[SourceCount, ...]:
    return tuple(SourceCount(source, count) for source, count in values.items())


@dataclass(frozen=True)
class CandidateCounts:
    total: int
    per_source: tuple[SourceCount, ...]
    with_dossiers: int


@dataclass(frozen=True)
class MessageCounts:
    total: int
    per_source: tuple[SourceCount, ...]


@dataclass(frozen=True)
class ImportReadinessCounts:
    message_people: int
    candidates: CandidateCounts


@dataclass(frozen=True)
class ProjectedReadinessCounts:
    message_people: int
    candidates: CandidateCounts
    messages: MessageCounts
    has_owner: bool
    owner_path: str


@dataclass(frozen=True)
class PathCheck:
    status: str
    path: str


@dataclass(frozen=True)
class ChatDbCheck:
    status: str
    messages: int
    error: str | None


@dataclass(frozen=True)
class PeopleCsvCheck:
    status: str
    path: str
    message_people: int


@dataclass(frozen=True)
class StatusCheck:
    status: str


@dataclass(frozen=True)
class ReadinessChecks:
    msgvault_gmail: PathCheck
    imessage_chat_db: ChatDbCheck
    whatsapp_wacli: PathCheck
    people_csv: PeopleCsvCheck
    owner_json: PathCheck
    openai_api_key: StatusCheck
    canonical_sqlite: PathCheck


@dataclass(frozen=True)
class ReadinessReport:
    source: str
    status: str
    ready: bool
    message_people: int
    candidates: CandidateCounts
    messages: MessageCounts
    checks: ReadinessChecks
    advice: tuple[str, ...]
    updated_at: IsoTimestamp
    next_command: str | None


def _source_counts_payload(rows: tuple[SourceCount, ...]) -> dict[str, int]:
    return {row.source: row.count for row in rows}


def readiness_payload(report: ReadinessReport) -> dict[str, object]:
    """Serialize the typed readiness report at the CLI response edge.

    The mirror image of parse-at-the-boundary: every field stays a typed
    dataclass until this one function, called once from check_readiness.main,
    turns it into the JSON dict the $deep-context skill actually reads.
    """
    checks = report.checks
    return {
        "source": report.source,
        "status": report.status,
        "ready": report.ready,
        "message_people": report.message_people,
        "candidates": {
            "total": report.candidates.total,
            "per_source": _source_counts_payload(report.candidates.per_source),
            "with_dossiers": report.candidates.with_dossiers,
        },
        "messages": {
            "total": report.messages.total,
            "per_source": _source_counts_payload(report.messages.per_source),
        },
        "checks": {
            "msgvault_gmail": {
                "status": checks.msgvault_gmail.status,
                "path": checks.msgvault_gmail.path,
            },
            "imessage_chat_db": {
                "status": checks.imessage_chat_db.status,
                "messages": checks.imessage_chat_db.messages,
                "error": checks.imessage_chat_db.error,
            },
            "whatsapp_wacli": {
                "status": checks.whatsapp_wacli.status,
                "path": checks.whatsapp_wacli.path,
            },
            "people_csv": {
                "status": checks.people_csv.status,
                "path": checks.people_csv.path,
                "message_people": checks.people_csv.message_people,
            },
            "owner_json": {
                "status": checks.owner_json.status,
                "path": checks.owner_json.path,
            },
            "openai_api_key": {"status": checks.openai_api_key.status},
            "canonical_sqlite": {
                "status": checks.canonical_sqlite.status,
                "path": checks.canonical_sqlite.path,
            },
        },
        "advice": list(report.advice),
        "updated_at": report.updated_at,
        "next_command": report.next_command,
    }
