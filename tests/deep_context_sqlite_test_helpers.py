"""Test-only SQLite seeding and inspection outside the production API."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest import mock

from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import judge
from packs.ingestion.primitives.deep_context.shared.openai_responses import OpenAIUsage
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    CandidatePeopleProjection,
    CandidatePersonRow,
    FactRow,
    LinkRow,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
    PersonSourceRow,
    PersonSourcesProjection,
    ProjectionStatus,
    ResearchRow,
    RowKind,
    SyntheticProfileRow,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db


SeedRow = ParentRow | PersonRow | ArtifactRow | FactRow | LinkRow


@contextmanager
def connect(db: Db) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(db.db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def query(db: Db, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    with connect(db) as connection:
        return connection.execute(sql, params).fetchall()


def scalar(db: Db, sql: str, params: tuple | dict = ()) -> object:
    return query(db, sql, params)[0][0]


def message_payload(
    text: str,
    *,
    channel: str = "imessage",
    at: str = "2026-08-06T12:00:00Z",
    direction: str = "from_them",
    subject: str = "",
) -> dict[str, str]:
    """Return the complete shape written by MessageEntry.to_payload."""
    return {
        "channel": channel,
        "at": at,
        "direction": direction,
        "subject": subject,
        "text": text,
    }


def write_override_rows(
    path: Path,
    columns: list[str],
    rows: dict[str, dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for key in sorted(rows):
            writer.writerow({column: rows[key].get(column, "") for column in columns})


def project_parent(db: Db, row: ParentRow) -> None:
    db.project_rows((row,))


def project_person(db: Db, row: PersonRow) -> None:
    db.project_rows((row,))


def project_candidate(db: Db, row: LinkRow) -> None:
    db.project_rows((row,))


def project_artifact(db: Db, row: ArtifactRow) -> bool:
    return bool(db.project_rows((row,)))


def project_fact(db: Db, row: FactRow) -> None:
    db.project_rows((row,))


def project_synthetic_profile(db: Db, row: SyntheticProfileRow) -> None:
    db.project_rows((row,))


def project_research(db: Db, row: ResearchRow) -> None:
    db.project_rows((row,))


def replace_person_identifiers(
    db: Db,
    person_id: str,
    rows: tuple[PersonIdentifierRow, ...],
) -> None:
    db.project_rows((PersonIdentifiersProjection(person_id, rows),))


def replace_person_sources(
    db: Db,
    person_id: str,
    rows: tuple[PersonSourceRow, ...],
) -> None:
    db.project_rows((PersonSourcesProjection(person_id, rows),))


def replace_candidate_people(
    db: Db,
    row_key: str,
    rows: tuple[CandidatePersonRow, ...],
) -> None:
    db.project_rows((CandidatePeopleProjection(row_key, rows),))


def seed_identity(
    db: Db,
    *,
    parent_id: str,
    person_id: str,
    row_key: str,
    name: str,
    machine_worth: str,
    display_slug: str | None = None,
    parent_public_identifier: str | None = None,
    public_identifier: str | None = None,
    kind: str = RowKind.PUB.value,
    linkedin_url: str | None = None,
    human_worth: str | None = None,
    link_updates: dict[str, object] | None = None,
    include_link: bool = True,
    candidate_people: bool = False,
    artifact_root: Path | None = None,
    dossier_body: str = "",
    avatar_bytes: bytes = b"",
) -> None:
    """Seed one facts-backed parent family through the public typed store door."""
    if candidate_people and not include_link:
        raise ValueError("candidate_people requires an identity link")
    slug = row_key if display_slug is None else display_slug
    artifact_key = f"facts:{person_id}"
    fact_payload = {
        "canonical_name": name,
        "network_worth": {"decision": machine_worth, "reason": "fixture"},
    }
    if artifact_root:
        fact_path = artifact_root / f"{person_id}.jsonl"
        fact_path.write_text(json.dumps({"facts": fact_payload}) + "\n", encoding="utf-8")
        fact_fingerprint = hashlib.sha256(fact_path.read_bytes()).hexdigest()
    else:
        fact_path = Path(f"/facts/{person_id}.jsonl")
        fact_fingerprint = f"worth-{person_id}"
    link_values: dict[str, object] = {
        "linkedin_url": linkedin_url,
        "display_name": name,
        "source": WriterSource.RECONCILE.value,
    }
    link_values.update(link_updates or {})
    rows: list[SeedRow] = [
        ParentRow(
            parent_id,
            parent_public_identifier or f"parent-worth:{parent_id}",
            name,
            slug,
        ),
        PersonRow(person_id, parent_id, slug, slug, name),
        ArtifactRow(
            artifact_key,
            ArtifactKind.FACTS.value,
            parent_id,
            str(fact_path),
            fact_fingerprint,
            ProjectionStatus.PROJECTED.value,
            person_id=person_id,
            payload_json=json.dumps({"facts": fact_payload}),
        ),
        FactRow(
            person_id,
            parent_id,
            artifact_key,
            person_id=person_id,
            machine_worth=machine_worth,
            machine_worth_reason="fixture",
            confidence=0.6,
            facts_json=json.dumps(fact_payload),
        ),
    ]
    if include_link:
        rows.append(LinkRow(
            row_key,
            parent_id,
            public_identifier or row_key,
            kind,
            **link_values,
        ))
    if artifact_root and dossier_body:
        dossier = artifact_root / f"{slug}.md"
        dossier.write_text(dossier_body, encoding="utf-8")
        rows.append(ArtifactRow(
            f"dossier:{parent_id}",
            ArtifactKind.DOSSIER.value,
            parent_id,
            str(dossier),
            hashlib.sha256(dossier.read_bytes()).hexdigest(),
            ProjectionStatus.PROJECTED.value,
            payload_json=json.dumps({"body": dossier_body}),
        ))
    if artifact_root and avatar_bytes:
        avatar = artifact_root / f"{slug}.image"
        avatar.write_bytes(avatar_bytes)
        rows.append(ArtifactRow(
            f"avatar:{row_key}",
            ArtifactKind.AVATAR.value,
            parent_id,
            str(avatar),
            hashlib.sha256(avatar_bytes).hexdigest(),
            ProjectionStatus.PROJECTED.value,
            candidate_key=row_key,
            payload_json=json.dumps({
                "content_type": "image/png",
                "base64": base64.b64encode(avatar_bytes).decode("ascii"),
            }),
        ))
    db.project_rows(tuple(rows))
    if candidate_people:
        replace_candidate_people(
            db,
            row_key,
            (CandidatePersonRow(row_key, person_id, parent_id),),
        )
    if human_worth is not None:
        db.decide_worth(parent_id, human_worth)


def stub_identity_judge(answer: dict[str, object]):
    """Replace the OpenAI caller with one that returns `answer`, spending nothing.

    Patched where ``judge_batch`` looks the class up, so a stage builds its
    caller exactly as it does in production and only the network call is fake.
    This is how a test exercises the REAL judging path — the alternative,
    an offline switch that settles verdicts deterministically, was deleted
    from both reconcile stages precisely because production never takes it.
    """

    class _StubCaller:
        def __init__(self, config) -> None:
            self.usage = OpenAIUsage()

        async def call(self, **_kwargs):
            return SimpleNamespace(payload=dict(answer), usage=OpenAIUsage())

        async def close(self) -> None:
            """judge_batch closes the caller in a finally; nothing to release here."""

    return mock.patch.object(judge, "OpenAIResponsesCaller", _StubCaller)
