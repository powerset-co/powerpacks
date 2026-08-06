"""Validate, plan, and atomically apply the canonical parent graph."""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace

from packs.ingestion.primitives.deep_context.db.models import (
    CanonicalGraphCounts,
    CanonicalGraphProjection,
    HUMAN_DECISION_SOURCES,
    PersonSourceRow,
    ReviewSource,
)
from packs.ingestion.primitives.deep_context.db.identity_policy import (
    _clear_machine_winner_conflicts,
)
from packs.ingestion.primitives.deep_context.db.schema import UPSERTS


class _GraphError(ValueError):
    pass


@dataclass(frozen=True)
class _GraphPlan:
    old_people: dict[str, sqlite3.Row]
    human_owners: dict[str, sqlite3.Row]
    candidate_targets: dict[str, str]
    artifact_targets: dict[str, str]
    fact_targets: dict[str, str]
    dependent_targets: dict[str, dict[str, str]]
    sources: tuple[PersonSourceRow, ...]
    parents_removed: int


def _settle_merged_identity_families(conn: sqlite3.Connection) -> None:
    """Extend the latest direct human decision across its current parent family."""
    direct_sources = tuple(sorted(HUMAN_DECISION_SOURCES))
    winners: dict[str, sqlite3.Row] = {}
    rows = conn.execute(
        "SELECT row_key, parent_id, decided_at FROM links "
        "WHERE decision_action IS NOT NULL AND decision_source IN (?, ?) "
        "ORDER BY decided_at DESC, row_key",
        direct_sources,
    )
    for row in rows:
        winners.setdefault(row["parent_id"], row)
    for parent_id, winner in winners.items():
        conn.execute(
            "UPDATE links SET decision_action='detach', decision_approved='yes', "
            "decision_source=?, decision_note=NULL, decided_at=?, replacement_url=NULL, "
            "replacement_public_identifier=NULL WHERE parent_id=? AND row_key!=?",
            (
                ReviewSource.SIBLING_SETTLE.value,
                winner["decided_at"],
                parent_id,
                winner["row_key"],
            ),
        )


def _validate(projection: CanonicalGraphProjection) -> tuple[tuple[str, ...], tuple[str, ...]]:
    parent_ids = tuple(row.parent_id for row in projection.parents)
    person_ids = tuple(row.person_id for row in projection.people)
    parent_set = set(parent_ids)
    person_set = set(person_ids)
    identifier_keys = tuple(
        (row.person_id, row.kind, row.normalized_value) for row in projection.identifiers
    )
    source_keys = tuple((row.person_id, row.source) for row in projection.sources)
    for keys, label in (
        (parent_ids, "parents"), (person_ids, "people"),
        (identifier_keys, "identifiers"), (source_keys, "sources"),
    ):
        if len(keys) != len(set(keys)):
            raise _GraphError(f"canonical graph contains duplicate {label}")
    for rows, field, owners, label in (
        (projection.people, "parent_id", parent_set, "person references an unknown parent"),
        (projection.identifiers, "person_id", person_set, "identifier references an unknown person"),
        (projection.sources, "person_id", person_set, "source references an unknown person"),
    ):
        if any(getattr(row, field) not in owners for row in rows):
            raise _GraphError(f"canonical {label}")
    return parent_ids, person_ids


def _plan(
    conn: sqlite3.Connection,
    projection: CanonicalGraphProjection,
    parent_ids: tuple[str, ...],
    person_ids: tuple[str, ...],
) -> _GraphPlan:
    old_parents = {
        row["parent_id"]: row for row in conn.execute("SELECT * FROM parents")
    }
    old_people = {row["person_id"]: row for row in conn.execute("SELECT * FROM people")}
    parent_set = set(parent_ids)
    person_set = set(person_ids)
    new_parent_by_person = {row.person_id: row.parent_id for row in projection.people}
    targets_by_old: dict[str, Counter[str]] = defaultdict(Counter)
    for person_id, new_parent in new_parent_by_person.items():
        old = old_people.get(person_id)
        if old is not None:
            targets_by_old[old["parent_id"]][new_parent] += 1

    def inherited_target(old_parent: str) -> str:
        targets = targets_by_old.get(old_parent) or Counter()
        if old_parent in parent_set and not targets:
            return old_parent
        if len(targets) == 1:
            return next(iter(targets))
        raise _GraphError(f"ambiguous canonical parent split: {old_parent}")

    removed_people = set(old_people) - person_set
    if removed_people:
        placeholders = ",".join("?" for _ in removed_people)
        for table in ("candidate_people", "artifacts", "facts"):
            found = conn.execute(
                f"SELECT 1 FROM {table} WHERE person_id IN ({placeholders}) LIMIT 1",
                tuple(removed_people),
            ).fetchone()
            if found:
                raise _GraphError(f"canonical graph would orphan {table}")

    memberships: dict[str, list[str]] = defaultdict(list)
    for row in conn.execute("SELECT row_key, person_id FROM candidate_people"):
        memberships[row["row_key"]].append(row["person_id"])
    candidate_targets: dict[str, str] = {}
    for row in conn.execute("SELECT row_key, parent_id FROM links"):
        members = memberships.get(row["row_key"], [])
        if any(person_id not in person_set for person_id in members):
            raise _GraphError(f"candidate has a removed person: {row['row_key']}")
        targets = {new_parent_by_person[person_id] for person_id in members}
        if len(targets) > 1:
            raise _GraphError(f"candidate crosses canonical parents: {row['row_key']}")
        if targets:
            candidate_targets[row["row_key"]] = next(iter(targets))
            continue
        candidate_targets[row["row_key"]] = inherited_target(row["parent_id"])

    parent_targets = {old_parent: inherited_target(old_parent) for old_parent in old_parents}

    artifact_targets: dict[str, str] = {}
    rows = conn.execute(
        "SELECT artifact_key, parent_id, person_id, candidate_key FROM artifacts"
    )
    for row in rows:
        if row["person_id"]:
            target = new_parent_by_person.get(row["person_id"])
            if target is None:
                raise _GraphError(f"artifact has a removed person: {row['artifact_key']}")
        elif row["candidate_key"]:
            target = candidate_targets.get(row["candidate_key"])
            if target is None:
                raise _GraphError(f"artifact has an unknown candidate: {row['artifact_key']}")
        else:
            target = parent_targets[row["parent_id"]]
        artifact_targets[row["artifact_key"]] = target

    fact_targets: dict[str, str] = {}
    rows = conn.execute("SELECT subject_key, parent_id, person_id, artifact_key FROM facts")
    for row in rows:
        target = (
            new_parent_by_person.get(row["person_id"])
            if row["person_id"]
            else artifact_targets.get(row["artifact_key"])
        )
        fact_targets[row["subject_key"]] = target or parent_targets[row["parent_id"]]

    dependent_targets: dict[str, dict[str, str]] = {}
    for table in ("research", "guidance", "jobs"):
        key = "name" if table == "jobs" else "handle"
        dependent_targets[table] = {}
        rows = conn.execute(
            f"SELECT {key}, parent_id, candidate_key FROM {table} WHERE parent_id IS NOT NULL"
        )
        for row in rows:
            target = (
                candidate_targets.get(row["candidate_key"])
                if row["candidate_key"]
                else parent_targets[row["parent_id"]]
            )
            if target is None:
                raise _GraphError(f"{table} has an unknown candidate: {row[key]}")
            dependent_targets[table][row[key]] = target

    human_owners: dict[str, sqlite3.Row] = {}
    for old_parent, targets in targets_by_old.items():
        old = old_parents.get(old_parent)
        if old is None or old["human_worth"] is None:
            continue
        for target in targets:
            current = human_owners.get(target)
            if current is None or (old["human_worth_at"] or "") > (
                current["human_worth_at"] or ""
            ):
                human_owners[target] = old

    old_sources = tuple(
        PersonSourceRow(row["person_id"], row["source"])
        for row in conn.execute("SELECT person_id, source FROM person_sources")
        if row["person_id"] in person_set
    )
    return _GraphPlan(
        old_people,
        human_owners,
        candidate_targets,
        artifact_targets,
        fact_targets,
        dependent_targets,
        projection.sources or old_sources,
        len(set(old_parents) - parent_set),
    )


def _apply(
    conn: sqlite3.Connection,
    projection: CanonicalGraphProjection,
    plan: _GraphPlan,
) -> CanonicalGraphCounts:
    for row in projection.parents:
        conn.execute(UPSERTS["parents"], asdict(row))
        owner = plan.human_owners.get(row.parent_id)
        conn.execute(
            "UPDATE parents SET human_worth=?, human_worth_note=?, "
            "human_worth_source=?, human_worth_at=? WHERE parent_id=?",
            (
                owner["human_worth"] if owner else None,
                owner["human_worth_note"] if owner else None,
                owner["human_worth_source"] if owner else None,
                owner["human_worth_at"] if owner else None,
                row.parent_id,
            ),
        )
    for row in projection.people:
        old = plan.old_people.get(row.person_id)
        effective = replace(
            row,
            facts_json=row.facts_json if row.facts_json is not None else old["facts_json"] if old else None,
            confidence=row.confidence if row.confidence is not None else old["confidence"] if old else None,
        )
        conn.execute(UPSERTS["people"], asdict(effective))

    for table, key, targets in (
        ("links", "row_key", plan.candidate_targets),
        ("candidate_people", "row_key", plan.candidate_targets),
        ("artifacts", "artifact_key", plan.artifact_targets),
        ("facts", "subject_key", plan.fact_targets),
    ):
        conn.executemany(
            f"UPDATE {table} SET parent_id=? WHERE {key}=?",
            [(target, row_key) for row_key, target in targets.items()],
        )
    _settle_merged_identity_families(conn)
    _clear_machine_winner_conflicts(
        conn, (row.parent_id for row in projection.parents),
    )
    for table, targets in plan.dependent_targets.items():
        key = "name" if table == "jobs" else "handle"
        conn.executemany(
            f"UPDATE {table} SET parent_id=? WHERE {key}=?",
            [(target, row_key) for row_key, target in targets.items()],
        )

    conn.execute("DELETE FROM person_identifiers")
    conn.executemany(
        "INSERT INTO person_identifiers VALUES "
        "(:person_id, :kind, :normalized_value, :display_value)",
        [asdict(row) for row in projection.identifiers],
    )
    conn.execute("DELETE FROM person_sources")
    conn.executemany(
        "INSERT INTO person_sources VALUES (:person_id, :source)",
        [asdict(row) for row in plan.sources],
    )
    for table, key, identifiers in (
        ("people", "person_id", tuple(row.person_id for row in projection.people)),
        ("parents", "parent_id", tuple(row.parent_id for row in projection.parents)),
    ):
        if identifiers:
            placeholders = ",".join("?" for _ in identifiers)
            conn.execute(f"DELETE FROM {table} WHERE {key} NOT IN ({placeholders})", identifiers)
        else:
            conn.execute(f"DELETE FROM {table}")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise _GraphError(f"canonical graph violates foreign keys: {violations[0]}")
    return CanonicalGraphCounts(
        len(projection.parents),
        len(projection.people),
        len(projection.identifiers),
        len(plan.sources),
        plan.parents_removed,
    )


def _replace_canonical_graph(
    conn: sqlite3.Connection,
    projection: CanonicalGraphProjection,
) -> CanonicalGraphCounts:
    parent_ids, person_ids = _validate(projection)
    return _apply(conn, projection, _plan(conn, projection, parent_ids, person_ids))
