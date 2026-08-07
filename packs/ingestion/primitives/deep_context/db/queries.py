"""Narrow typed reads for canonical people, facts, and artifacts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    FactRow,
    MergeVerdictRow,
    OwnerContextRow,
    OwnerEducation,
    OwnerProfile,
    OwnerWork,
    ParentSnapshotRow,
    PersonIdentifierRow,
    PersonRow,
    PersonSourceRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db


RowT = TypeVar("RowT")

_BOOLEAN_COLUMNS = frozenset(
    {
        "accepted",
        "authoritative_detach",
        "candidate_origin",
        "is_ghost",
        "is_owner",
        "paid_profile",
        "raw_import",
        "same_person",
        "tone_consistent",
    }
)


def typed_rows(
    db: Db,
    sql: str,
    row_type: type[RowT],
    params: Sequence[object] = (),
) -> tuple[RowT, ...]:
    """Materialize one explicit SELECT into its frozen row type."""
    result: list[RowT] = []
    for row in db.query(sql, params):
        values = dict(row)
        for column in _BOOLEAN_COLUMNS.intersection(values):
            values[column] = bool(values[column])
        result.append(row_type(**values))
    return tuple(result)


def owner_profile(db: Db) -> OwnerProfile | None:
    """Read the required synthesis owner profile without loading other tables."""
    rows = typed_rows(
        db,
        "SELECT * FROM owner_context WHERE context_key='owner'",
        OwnerContextRow,
    )
    if not rows:
        return None
    payload = parse_json_object(rows[0].payload_json)

    def date(value: object) -> int | str | None:
        return value if isinstance(value, (int, str)) else None

    education: list[OwnerEducation] = []
    for item in payload.get("education") or ():
        if isinstance(item, dict):
            education.append(
                OwnerEducation(
                    school=str(item.get("school") or ""),
                    start=date(item.get("start")),
                    end=date(item.get("end")),
                    note=str(item.get("note") or ""),
                )
            )
    work: list[OwnerWork] = []
    for item in payload.get("work") or ():
        if isinstance(item, dict):
            work.append(
                OwnerWork(
                    company=str(item.get("company") or ""),
                    title=str(item.get("title") or ""),
                    start=date(item.get("start")),
                    end=date(item.get("end")),
                )
            )
    return OwnerProfile(
        name=str(payload.get("name") or ""),
        emails=tuple(str(value) for value in payload.get("emails") or ()),
        phones=tuple(str(value) for value in payload.get("phones") or ()),
        education=tuple(education),
        work=tuple(work),
        locations=tuple(str(value) for value in payload.get("locations") or ()),
        notes=str(payload.get("notes") or ""),
    )


def owner_path(db: Db) -> str | None:
    rows = db.query("SELECT path FROM owner_context WHERE context_key='owner'")
    return str(rows[0]["path"]) if rows else None


def parents(db: Db, *, parent_id: str | None = None) -> tuple[ParentSnapshotRow, ...]:
    where = " WHERE parent_id=?" if parent_id is not None else ""
    params = (parent_id,) if parent_id is not None else ()
    return typed_rows(
        db,
        f"SELECT * FROM parents{where} ORDER BY parent_id",
        ParentSnapshotRow,
        params,
    )


def people(
    db: Db,
    *,
    parent_id: str | None = None,
    person_id: str | None = None,
) -> tuple[PersonRow, ...]:
    if person_id is not None:
        return typed_rows(
            db,
            "SELECT * FROM people WHERE person_id=? ORDER BY person_id",
            PersonRow,
            (person_id,),
        )
    where = " WHERE parent_id=?" if parent_id is not None else ""
    params = (parent_id,) if parent_id is not None else ()
    return typed_rows(
        db,
        f"SELECT * FROM people{where} ORDER BY person_id",
        PersonRow,
        params,
    )


def identifiers(
    db: Db,
    *,
    person_id: str | None = None,
    parent_id: str | None = None,
) -> tuple[PersonIdentifierRow, ...]:
    if parent_id is not None:
        return typed_rows(
            db,
            "SELECT pi.* FROM person_identifiers pi JOIN people pe USING(person_id) "
            "WHERE pe.parent_id=? ORDER BY pi.person_id, pi.kind, pi.normalized_value",
            PersonIdentifierRow,
            (parent_id,),
        )
    where = " WHERE person_id=?" if person_id is not None else ""
    params = (person_id,) if person_id is not None else ()
    return typed_rows(
        db,
        f"SELECT * FROM person_identifiers{where} ORDER BY person_id, kind, normalized_value",
        PersonIdentifierRow,
        params,
    )


def sources(
    db: Db,
    *,
    person_id: str | None = None,
    parent_id: str | None = None,
) -> tuple[PersonSourceRow, ...]:
    if parent_id is not None:
        return typed_rows(
            db,
            "SELECT ps.* FROM person_sources ps JOIN people pe USING(person_id) "
            "WHERE pe.parent_id=? ORDER BY ps.person_id, ps.source",
            PersonSourceRow,
            (parent_id,),
        )
    where = " WHERE person_id=?" if person_id is not None else ""
    params = (person_id,) if person_id is not None else ()
    return typed_rows(
        db,
        f"SELECT * FROM person_sources{where} ORDER BY person_id, source",
        PersonSourceRow,
        params,
    )


def artifacts(
    db: Db,
    *,
    kind: str | None = None,
    parent_id: str | None = None,
    person_id: str | None = None,
    candidate_key: str | None = None,
    candidate_keys: Sequence[str] | None = None,
    status: str | None = None,
    parent_owned: bool | None = None,
) -> tuple[ArtifactRow, ...]:
    clauses: list[str] = []
    params: list[str] = []
    for column, value in (
        ("kind", kind),
        ("parent_id", parent_id),
        ("person_id", person_id),
        ("candidate_key", candidate_key),
        ("status", status),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    if candidate_keys is not None:
        selected = tuple(dict.fromkeys(candidate_keys))
        if not selected:
            return ()
        placeholders = ",".join("?" for _ in selected)
        clauses.append(f"candidate_key IN ({placeholders})")
        params.extend(selected)
    if parent_owned is not None:
        clauses.append("person_id IS NULL" if parent_owned else "person_id IS NOT NULL")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return typed_rows(
        db,
        f"SELECT * FROM artifacts{where} ORDER BY artifact_key",
        ArtifactRow,
        tuple(params),
    )


def facts(
    db: Db,
    *,
    parent_id: str | None = None,
    parent_owned: bool | None = None,
) -> tuple[FactRow, ...]:
    clauses: list[str] = []
    params: list[str] = []
    if parent_id is not None:
        clauses.append("parent_id=?")
        params.append(parent_id)
    if parent_owned is not None:
        clauses.append("person_id IS NULL" if parent_owned else "person_id IS NOT NULL")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return typed_rows(
        db,
        f"SELECT * FROM facts{where} ORDER BY subject_key",
        FactRow,
        tuple(params),
    )


def merge_verdicts(db: Db) -> tuple[MergeVerdictRow, ...]:
    return typed_rows(
        db,
        "SELECT * FROM merge_verdicts ORDER BY person_a, person_b",
        MergeVerdictRow,
    )
