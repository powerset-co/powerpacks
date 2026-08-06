"""Test-only SQLite seeding and inspection outside the production API."""

from __future__ import annotations

import csv
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from packs.ingestion.primitives.deep_context.db.models import (
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
    ResearchRow,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db


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
