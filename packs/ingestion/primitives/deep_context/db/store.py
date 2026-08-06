"""Narrow projector and domain-transaction API for Deep Context SQLite."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path
from typing import Iterator

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db import graph
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    CandidatePeopleProjection,
    CanonicalGraphCounts,
    CanonicalGraphProjection,
    FactRow,
    GuidanceRow,
    HUMAN_DECISION_SOURCES,
    HumanWorth,
    IdentityMachineProjection,
    JobRow,
    LinkRow,
    ParentRow,
    PersonIdentifiersProjection,
    PersonRow,
    PersonSourcesProjection,
    ResearchRow,
    ResetReviewCounts,
    ReviewAction,
    ReviewSource,
    SpendApprovalRow,
    StageStateRow,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db.schema import (
    DDL,
    ROW_TYPES,
    SCHEMA_VERSION,
    UPSERTS,
)


class StoreError(ValueError):
    pass


class SchemaVersionError(StoreError):
    pass


def _schema_signature(conn: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(tuple(row) for row in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ))


def _expected_signature() -> tuple[tuple[str, str, str, str], ...]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(DDL)
        return _schema_signature(conn)
    finally:
        conn.close()


EXPECTED_SCHEMA_SIGNATURE = _expected_signature()


_CHILD_KEYS = {
    "person_identifiers": "person_id",
    "person_sources": "person_id",
    "candidate_people": "row_key",
}


class Db:
    """Open an exact v6 store or create one; existing files are never changed on failure."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.db_path.exists():
                self._validate_existing()
            else:
                self._create()
        except StoreError:
            raise
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open Deep Context database: {exc}") from exc

    def _create(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(DDL)
            conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
            conn.commit()
        except BaseException:
            conn.close()
            self.db_path.unlink(missing_ok=True)
            raise
        conn.close()

    def _validate_existing(self) -> None:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=rw", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            if "meta" not in tables:
                raise SchemaVersionError("deep-context DB has no schema version; import explicitly")
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            found = row["value"] if row else "missing"
            if found != str(SCHEMA_VERSION):
                raise SchemaVersionError(
                    f"deep-context DB schema is {found}, expected {SCHEMA_VERSION}; "
                    "migrate into a new canonical DB explicitly"
                )
            if _schema_signature(conn) != EXPECTED_SCHEMA_SIGNATURE:
                raise SchemaVersionError(
                    f"deep-context DB layout does not match schema version {SCHEMA_VERSION}; "
                    "migrate into a new canonical DB explicitly"
                )
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One owned connection: commit on success, rollback on any error."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _tx(self, conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        """Run inside the caller's open transaction, or own a fresh one."""
        if conn is not None:
            yield conn
            return
        with self.transaction() as owned:
            yield owned

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.transaction() as conn:
            return conn.execute(sql, params).fetchall()

    def _write(self, table: str, row: object, conn: sqlite3.Connection | None = None) -> None:
        with self._tx(conn) as target:
            target.execute(UPSERTS[table], asdict(row))

    def _replace_children(
        self, table: str, key: str, rows: tuple,
        *, conn: sqlite3.Connection | None = None,
    ) -> None:
        key_column = _CHILD_KEYS[table]
        if any(getattr(row, key_column) != key for row in rows):
            raise StoreError(f"{table} owner does not match {key_column}")
        names = [field.name for field in fields(ROW_TYPES[table])]
        with self._tx(conn) as target:
            target.execute(f"DELETE FROM {table} WHERE {key_column}=?", (key,))
            target.executemany(
                f"INSERT INTO {table} ({', '.join(names)}) VALUES "
                f"({', '.join(':' + name for name in names)})",
                [asdict(row) for row in rows],
            )

    def replace_canonical_graph(
        self, projection: CanonicalGraphProjection,
    ) -> CanonicalGraphCounts:
        """Atomically replace parent membership while preserving owned dependents."""
        with self.transaction() as conn:
            conn.execute("PRAGMA defer_foreign_keys=ON")
            conn.execute("BEGIN DEFERRED")
            try:
                return graph._replace_canonical_graph(conn, projection)
            except graph._GraphError as exc:
                raise StoreError(str(exc)) from exc

    def _project_candidate(self, row: LinkRow, *, conn: sqlite3.Connection | None = None) -> None:
        with self._tx(conn) as target:
            current = target.execute(
                "SELECT parent_id, kind FROM links WHERE row_key=?", (row.row_key,)
            ).fetchone()
            if current and tuple(current) != (row.parent_id, row.kind):
                raise StoreError(f"candidate owner/kind changed: {row.row_key}")
            target.execute(UPSERTS["links"], asdict(row))

    def project_identity(
        self, rows: tuple[IdentityMachineProjection, ...],
        *, conn: sqlite3.Connection | None = None,
    ) -> None:
        """Project one machine-owned identity batch without touching human decisions."""
        sql = (
            "UPDATE links SET machine_action=:machine_action, "
            "machine_approved=:machine_approved, machine_confidence=:machine_confidence, "
            "machine_reason=:machine_reason, machine_judgment=:machine_judgment, "
            "machine_reject=:machine_reject, "
            "machine_reject_confidence=:machine_reject_confidence, "
            "machine_reject_reason=:machine_reject_reason, "
            "machine_proposed_url=:machine_proposed_url, "
            "machine_proposed_public_identifier=:machine_proposed_public_identifier, "
            "authoritative_detach=:authoritative_detach, paid_profile=:paid_profile, "
            "judgment_fingerprint=:judgment_fingerprint, "
            "judgment_artifact_path=:judgment_artifact_path, "
            "judgment_payload_json=:judgment_payload_json, source=:source, "
            "updated_at=:updated_at WHERE row_key=:row_key"
        )
        with self._tx(conn) as target:
            for row in rows:
                if target.execute(sql, asdict(row)).rowcount != 1:
                    raise StoreError(f"unknown candidate: {row.row_key}")

    def _project_artifact(self, row: ArtifactRow, *, conn: sqlite3.Connection | None = None) -> bool:
        with self._tx(conn) as target:
            current = target.execute(
                "SELECT content_fingerprint, status FROM artifacts WHERE artifact_key=?",
                (row.artifact_key,),
            ).fetchone()
            if current and tuple(current) == (row.content_fingerprint, "projected"):
                return False
            target.execute(UPSERTS["artifacts"], asdict(row))
            return True

    def project_rows(
        self,
        rows: tuple[
            ParentRow
            | PersonRow
            | PersonIdentifiersProjection
            | PersonSourcesProjection
            | LinkRow
            | CandidatePeopleProjection
            | ArtifactRow
            | FactRow
            | SyntheticProfileRow
            | ResearchRow,
            ...,
        ],
    ) -> int:
        """Atomically project a closed union of frozen domain row models."""
        changed = 0
        with self.transaction() as conn:
            for row in rows:
                match row:
                    case ParentRow():
                        self._write("parents", row, conn)
                    case PersonRow():
                        self._write("people", row, conn)
                    case PersonIdentifiersProjection():
                        self._replace_children(
                            "person_identifiers", row.person_id, row.rows, conn=conn,
                        )
                    case PersonSourcesProjection():
                        self._replace_children(
                            "person_sources", row.person_id, row.rows, conn=conn,
                        )
                    case LinkRow():
                        self._project_candidate(row, conn=conn)
                    case CandidatePeopleProjection():
                        self._replace_children(
                            "candidate_people", row.row_key, row.rows, conn=conn,
                        )
                    case ArtifactRow():
                        changed += int(self._project_artifact(row, conn=conn))
                    case FactRow():
                        self._write("facts", row, conn)
                    case SyntheticProfileRow():
                        self._write("synthetic_profiles", row, conn)
                    case ResearchRow():
                        self._write("research", row, conn)
                    case _:
                        raise TypeError(f"unsupported projection row: {type(row).__name__}")
        return changed

    def _save_stage(
        self, row: StageStateRow, *, conn: sqlite3.Connection | None = None,
    ) -> None:
        with self._tx(conn) as target:
            current = target.execute(
                "SELECT selection_fingerprint FROM stage_state WHERE stage=?", (row.stage,)
            ).fetchone()
            target.execute(UPSERTS["stage_state"], asdict(row))
            if current and current[0] != row.selection_fingerprint:
                target.execute("DELETE FROM spend_approvals WHERE stage=?", (row.stage,))

    def _approve_spend(self, row: SpendApprovalRow) -> None:
        with self.transaction() as conn:
            stage = conn.execute(
                "SELECT selection_fingerprint FROM stage_state WHERE stage=?", (row.stage,)
            ).fetchone()
            if stage is None or stage[0] != row.selection_fingerprint:
                raise StoreError("spend approval does not match the current selection")
            conn.execute(UPSERTS["spend_approvals"], asdict(row))

    def save_state(
        self, row: GuidanceRow | JobRow | StageStateRow | SpendApprovalRow,
    ) -> None:
        """Persist one frozen workflow-state row through its domain validation."""
        match row:
            case GuidanceRow():
                self._write("guidance", row)
            case JobRow():
                self._write("jobs", row)
            case StageStateRow():
                self._save_stage(row)
            case SpendApprovalRow():
                self._approve_spend(row)
            case _:
                raise TypeError(f"unsupported state row: {type(row).__name__}")

    def decide_worth(
        self, parent_id: str, value: str | None, *, note: str | None = None,
        source: str = ReviewSource.REVIEW.value, decided_at: str | None = None,
    ) -> None:
        """Set or reset one human worth decision."""
        if value is None:
            values = (None, None, None, None)
        else:
            if value not in {item.value for item in HumanWorth}:
                raise StoreError(f"invalid human worth: {value}")
            if source not in HUMAN_DECISION_SOURCES:
                raise StoreError(f"invalid human decision source: {source}")
            values = (value, note, source, decided_at or now_iso())
        with self.transaction() as conn:
            changed = conn.execute(
                "UPDATE parents SET human_worth=?, human_worth_note=?, "
                "human_worth_source=?, human_worth_at=? WHERE parent_id=?",
                (*values, parent_id),
            ).rowcount
            if changed != 1:
                raise StoreError(f"unknown parent: {parent_id}")

    def _settle_identity(
        self, clicked_key: str, action: str, *, approved: str = "yes",
        replacement_url: str | None = None, replacement_public_identifier: str | None = None,
        source: str = ReviewSource.REVIEW.value, note: str | None = None,
        decided_at: str | None = None,
    ) -> list[str]:
        if action not in {item.value for item in ReviewAction}:
            raise StoreError(f"invalid identity action: {action}")
        if approved not in {"yes", "no"}:
            raise StoreError(f"invalid human approval: {approved}")
        if source not in HUMAN_DECISION_SOURCES:
            raise StoreError(f"invalid human decision source: {source}")
        if action == ReviewAction.RETARGET.value and not replacement_url:
            raise StoreError("retarget requires replacement_url")
        if action != ReviewAction.RETARGET.value and (replacement_url or replacement_public_identifier):
            raise StoreError("replacement is valid only for retarget")
        at = decided_at or now_iso()
        with self.transaction() as conn:
            clicked = conn.execute("SELECT * FROM links WHERE row_key=?", (clicked_key,)).fetchone()
            if clicked is None:
                raise StoreError(f"unknown candidate: {clicked_key}")
            if clicked["decision_action"] is not None and clicked["decision_source"] in HUMAN_DECISION_SOURCES:
                same = (
                    clicked["decision_action"], clicked["decision_approved"],
                    clicked["replacement_url"], clicked["replacement_public_identifier"],
                ) == (action, approved, replacement_url, replacement_public_identifier)
                if not same:
                    raise StoreError(f"candidate already has a human decision: {clicked_key}")
            conn.execute(
                "UPDATE links SET decision_action=?, decision_approved=?, decision_source=?, "
                "decision_note=?, decided_at=?, replacement_url=?, "
                "replacement_public_identifier=? WHERE row_key=?",
                (action, approved, source, note, at, replacement_url,
                 replacement_public_identifier, clicked_key),
            )
            siblings = [row["row_key"] for row in conn.execute(
                "SELECT row_key FROM links WHERE parent_id=? AND row_key!=? "
                "AND decision_action IS NULL ORDER BY row_key",
                (clicked["parent_id"], clicked_key),
            )]
            conn.executemany(
                "UPDATE links SET decision_action='detach', decision_approved='yes', "
                "decision_source=?, decided_at=? WHERE row_key=? AND decision_action IS NULL",
                [(ReviewSource.SIBLING_SETTLE.value, at, key) for key in siblings],
            )
        return [clicked_key, *siblings]

    def _reset_identity(self, candidate_key: str) -> list[str]:
        with self.transaction() as conn:
            row = conn.execute("SELECT parent_id FROM links WHERE row_key=?", (candidate_key,)).fetchone()
            if row is None:
                raise StoreError(f"unknown candidate: {candidate_key}")
            reset = [item["row_key"] for item in conn.execute(
                "SELECT row_key FROM links WHERE parent_id=? AND decision_source IN (?, ?, ?)",
                (row["parent_id"], *HUMAN_DECISION_SOURCES, ReviewSource.SIBLING_SETTLE.value),
            )]
            conn.execute(
                "UPDATE links SET decision_action=NULL, decision_approved=NULL, "
                "decision_source=NULL, decision_note=NULL, decided_at=NULL, "
                "replacement_url=NULL, replacement_public_identifier=NULL "
                "WHERE parent_id=? AND decision_source IN (?, ?, ?)",
                (row["parent_id"], *HUMAN_DECISION_SOURCES, ReviewSource.SIBLING_SETTLE.value),
            )
        return reset

    def decide_identity(
        self, candidate_key: str, action: str | None, *, approved: str = "yes",
        replacement_url: str | None = None,
        replacement_public_identifier: str | None = None,
        source: str = ReviewSource.REVIEW.value, note: str | None = None,
        decided_at: str | None = None,
    ) -> list[str]:
        """Settle one candidate family, or reset its human identity decisions."""
        if action is None:
            return self._reset_identity(candidate_key)
        return self._settle_identity(
            candidate_key,
            action,
            approved=approved,
            replacement_url=replacement_url,
            replacement_public_identifier=replacement_public_identifier,
            source=source,
            note=note,
            decided_at=decided_at,
        )

    def reset_review(self, *, apply: bool = True) -> ResetReviewCounts:
        """Clear human review state atomically while preserving every machine artifact."""
        stages = ("worth", "enrich", "enrichment", "linkedin", "review")
        sources = (*HUMAN_DECISION_SOURCES, ReviewSource.SIBLING_SETTLE.value)
        with self.transaction() as conn:
            if not apply:
                worth = conn.execute(
                    "SELECT count(*) FROM parents WHERE human_worth IS NOT NULL "
                    "OR human_worth_note IS NOT NULL OR human_worth_source IS NOT NULL "
                    "OR human_worth_at IS NOT NULL"
                ).fetchone()[0]
                identity = conn.execute(
                    "SELECT count(*) FROM links WHERE decision_source IN (?, ?, ?)", sources,
                ).fetchone()[0]
                stage_count = conn.execute(
                    "SELECT count(*) FROM stage_state WHERE stage IN (?, ?, ?, ?, ?)", stages,
                ).fetchone()[0]
                approvals = conn.execute(
                    "SELECT count(*) FROM spend_approvals WHERE stage IN (?, ?, ?, ?, ?)", stages,
                ).fetchone()[0]
                return ResetReviewCounts(worth, identity, stage_count, approvals)
            worth = conn.execute(
                "UPDATE parents SET human_worth=NULL, human_worth_note=NULL, "
                "human_worth_source=NULL, human_worth_at=NULL "
                "WHERE human_worth IS NOT NULL OR human_worth_note IS NOT NULL "
                "OR human_worth_source IS NOT NULL OR human_worth_at IS NOT NULL"
            ).rowcount
            identity = conn.execute(
                "UPDATE links SET decision_action=NULL, decision_approved=NULL, "
                "decision_source=NULL, decision_note=NULL, decided_at=NULL, "
                "replacement_url=NULL, replacement_public_identifier=NULL "
                "WHERE decision_source IN (?, ?, ?)", sources,
            ).rowcount
            stage_count = conn.execute(
                "UPDATE stage_state SET status='pending', selection_fingerprint=NULL, "
                "artifact_fingerprint=NULL, completed_at=NULL, error=NULL, updated_at=? "
                "WHERE stage IN (?, ?, ?, ?, ?)", (now_iso(), *stages),
            ).rowcount
            approvals = conn.execute(
                "DELETE FROM spend_approvals WHERE stage IN (?, ?, ?, ?, ?)", stages,
            ).rowcount
        return ResetReviewCounts(worth, identity, stage_count, approvals)
