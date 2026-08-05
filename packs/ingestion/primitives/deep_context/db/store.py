"""Narrow projector and domain-transaction API for Deep Context SQLite."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path
from typing import Iterator

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.schema import (
    DDL,
    ROW_TYPES,
    SCHEMA_VERSION,
    ArtifactRow,
    CandidatePersonRow,
    FactRow,
    GuidanceRow,
    HUMAN_DECISION_SOURCES,
    HumanWorth,
    JobRow,
    LinkRow,
    ParentRow,
    PersonIdentifierRow,
    PersonRow,
    ResearchRow,
    ReviewAction,
    ReviewSource,
    SpendApprovalRow,
    StageStateRow,
    SyntheticProfileRow,
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


_KEYS = {
    "parents": ("parent_id",), "people": ("person_id",),
    "person_identifiers": ("person_id", "kind", "normalized_value"),
    "links": ("row_key",), "candidate_people": ("row_key", "person_id"),
    "artifacts": ("artifact_key",), "facts": ("subject_key",),
    "synthetic_profiles": ("public_identifier",), "research": ("handle",),
    "guidance": ("handle",), "jobs": ("name",), "stage_state": ("stage",),
    "spend_approvals": ("stage",),
}


def _upsert_sql(table: str) -> str:
    names = [field.name for field in fields(ROW_TYPES[table])]
    updates = [name for name in names if name not in _KEYS[table]]
    return (
        f"INSERT INTO {table} ({', '.join(names)}) VALUES "
        f"({', '.join(':' + name for name in names)}) ON CONFLICT "
        f"({', '.join(_KEYS[table])}) DO UPDATE SET "
        + ", ".join(f"{name}=excluded.{name}" for name in updates)
    )


_UPSERTS = {table: _upsert_sql(table) for table in ROW_TYPES}


class Db:
    """Open an exact v5 store or create one; existing files are never changed on failure."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.db_path.exists():
            self._validate_existing()
        else:
            self._create()

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
        finally:
            if self.db_path.exists():
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
                    "deep-context DB layout does not match schema version 5; "
                    "migrate into a new canonical DB explicitly"
                )
        finally:
            conn.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
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

    transaction = connect

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def _write(self, table: str, row: object, conn: sqlite3.Connection | None = None) -> None:
        if conn is not None:
            conn.execute(_UPSERTS[table], asdict(row))
            return
        with self.connect() as owned:
            owned.execute(_UPSERTS[table], asdict(row))

    def project_parent(self, row: ParentRow, *, conn: sqlite3.Connection | None = None) -> None:
        self._write("parents", row, conn)

    def project_person(self, row: PersonRow, *, conn: sqlite3.Connection | None = None) -> None:
        self._write("people", row, conn)

    def replace_person_identifiers(
        self, person_id: str, rows: tuple[PersonIdentifierRow, ...],
        *, conn: sqlite3.Connection | None = None,
    ) -> None:
        if any(row.person_id != person_id for row in rows):
            raise StoreError("identifier owner does not match person")

        def replace(target: sqlite3.Connection) -> None:
            target.execute("DELETE FROM person_identifiers WHERE person_id=?", (person_id,))
            target.executemany(
                "INSERT INTO person_identifiers VALUES "
                "(:person_id, :kind, :normalized_value, :display_value)",
                [asdict(row) for row in rows],
            )

        if conn is not None:
            replace(conn)
        else:
            with self.connect() as owned:
                replace(owned)

    def project_candidate(self, row: LinkRow, *, conn: sqlite3.Connection | None = None) -> None:
        def project(target: sqlite3.Connection) -> None:
            current = target.execute(
                "SELECT parent_id, kind FROM links WHERE row_key=?", (row.row_key,)
            ).fetchone()
            if current and tuple(current) != (row.parent_id, row.kind):
                raise StoreError(f"candidate owner/kind changed: {row.row_key}")
            target.execute(_UPSERTS["links"], asdict(row))

        if conn is not None:
            project(conn)
        else:
            with self.connect() as owned:
                project(owned)

    def replace_candidate_people(
        self, row_key: str, rows: tuple[CandidatePersonRow, ...],
        *, conn: sqlite3.Connection | None = None,
    ) -> None:
        if any(row.row_key != row_key for row in rows):
            raise StoreError("candidate membership key mismatch")

        def replace(target: sqlite3.Connection) -> None:
            target.execute("DELETE FROM candidate_people WHERE row_key=?", (row_key,))
            target.executemany(
                "INSERT INTO candidate_people VALUES (:row_key, :person_id, :parent_id)",
                [asdict(row) for row in rows],
            )

        if conn is not None:
            replace(conn)
        else:
            with self.connect() as owned:
                replace(owned)

    def project_artifact(self, row: ArtifactRow, *, conn: sqlite3.Connection | None = None) -> bool:
        def project(target: sqlite3.Connection) -> bool:
            current = target.execute(
                "SELECT content_fingerprint, status FROM artifacts WHERE artifact_key=?",
                (row.artifact_key,),
            ).fetchone()
            if current and tuple(current) == (row.content_fingerprint, "projected"):
                return False
            target.execute(
                "INSERT INTO artifacts VALUES ("
                ":artifact_key, :kind, :parent_id, :person_id, :candidate_key, :path, "
                ":content_fingerprint, :input_fingerprint, :status, :error, :payload_json, "
                ":projected_at) ON CONFLICT(artifact_key) DO UPDATE SET "
                "kind=excluded.kind, parent_id=excluded.parent_id, person_id=excluded.person_id, "
                "candidate_key=excluded.candidate_key, path=excluded.path, "
                "content_fingerprint=excluded.content_fingerprint, "
                "input_fingerprint=excluded.input_fingerprint, status=excluded.status, "
                "error=excluded.error, payload_json=excluded.payload_json, "
                "projected_at=excluded.projected_at",
                asdict(row),
            )
            return True

        if conn is not None:
            return project(conn)
        with self.connect() as owned:
            return project(owned)

    def project_fact(self, row: FactRow, *, conn: sqlite3.Connection | None = None) -> None:
        self._write("facts", row, conn)

    def project_synthetic_profile(
        self, row: SyntheticProfileRow, *, conn: sqlite3.Connection | None = None,
    ) -> None:
        self._write("synthetic_profiles", row, conn)

    def project_research(self, row: ResearchRow, *, conn: sqlite3.Connection | None = None) -> None:
        self._write("research", row, conn)

    def save_guidance(self, row: GuidanceRow) -> None:
        self._write("guidance", row)

    def save_job(self, row: JobRow, *, conn: sqlite3.Connection | None = None) -> None:
        self._write("jobs", row, conn)

    def save_stage(
        self, row: StageStateRow, *, conn: sqlite3.Connection | None = None,
    ) -> None:
        def save(target: sqlite3.Connection) -> None:
            current = target.execute(
                "SELECT selection_fingerprint FROM stage_state WHERE stage=?", (row.stage,)
            ).fetchone()
            target.execute(_UPSERTS["stage_state"], asdict(row))
            if current and current[0] != row.selection_fingerprint:
                target.execute("DELETE FROM spend_approvals WHERE stage=?", (row.stage,))

        if conn is not None:
            save(conn)
        else:
            with self.connect() as owned:
                save(owned)

    def approve_spend(self, row: SpendApprovalRow) -> None:
        with self.connect() as conn:
            stage = conn.execute(
                "SELECT selection_fingerprint FROM stage_state WHERE stage=?", (row.stage,)
            ).fetchone()
            if stage is None or stage[0] != row.selection_fingerprint:
                raise StoreError("spend approval does not match the current selection")
            conn.execute(_UPSERTS["spend_approvals"], asdict(row))

    def set_worth(self, parent_id: str, value: str, *, note: str | None = None,
                  source: str = ReviewSource.REVIEW.value, decided_at: str | None = None) -> None:
        if value not in {item.value for item in HumanWorth}:
            raise StoreError(f"invalid human worth: {value}")
        if source not in HUMAN_DECISION_SOURCES:
            raise StoreError(f"invalid human decision source: {source}")
        with self.connect() as conn:
            changed = conn.execute(
                "UPDATE parents SET human_worth=?, human_worth_note=?, "
                "human_worth_source=?, human_worth_at=? WHERE parent_id=?",
                (value, note, source, decided_at or now_iso(), parent_id),
            ).rowcount
            if changed != 1:
                raise StoreError(f"unknown parent: {parent_id}")

    def reset_worth(self, parent_id: str) -> None:
        with self.connect() as conn:
            changed = conn.execute(
                "UPDATE parents SET human_worth=NULL, human_worth_note=NULL, "
                "human_worth_source=NULL, human_worth_at=NULL WHERE parent_id=?",
                (parent_id,),
            ).rowcount
            if changed != 1:
                raise StoreError(f"unknown parent: {parent_id}")

    def settle_identity(
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
        with self.connect() as conn:
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

    def reset_identity(self, candidate_key: str) -> list[str]:
        with self.connect() as conn:
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

    def rows(self) -> dict[str, dict[str, str]]:
        """Explicit review.csv projection; runtime never reads this export."""
        from packs.ingestion.primitives.deep_context.db import batons

        out: dict[str, dict[str, str]] = {}
        for link in self.query(
            "SELECT l.*, (SELECT person_id FROM candidate_people cp "
            "WHERE cp.row_key=l.row_key ORDER BY person_id LIMIT 1) person_id FROM links l"
        ):
            row = {column: "" for column in batons.OVERRIDE_COLUMNS}
            values = {
                "public_identifier": link["public_identifier"], "person_id": link["person_id"],
                "linkedin_url": link["linkedin_url"], "action": link["decision_action"] or link["machine_action"],
                "approved": link["decision_approved"] or link["machine_approved"],
                "new_linkedin_url": link["replacement_url"],
                "new_public_identifier": link["replacement_public_identifier"],
                "confidence": link["machine_confidence"], "reason": link["machine_reason"],
                "llm_reject": link["machine_judgment"],
                "llm_reject_confidence": link["machine_confidence"],
                "llm_reject_reason": link["machine_reason"],
                "llm_judge_fingerprint": link["judgment_fingerprint"],
                "source": link["decision_source"] or link["source"],
                "updated_at": link["decided_at"] or link["updated_at"],
            }
            if link["decision_action"] is None and link["machine_action"] == "retarget":
                values["new_linkedin_url"] = link["machine_proposed_url"]
                values["new_public_identifier"] = link["machine_proposed_public_identifier"]
            row.update({key: "" if value is None else str(value) for key, value in values.items()})
            out[link["row_key"]] = row
        for parent in self.query("SELECT * FROM parents"):
            row = {column: "" for column in batons.OVERRIDE_COLUMNS}
            row.update({
                "public_identifier": parent["public_identifier"],
                "llm_worth": parent["machine_worth"] or "",
                "llm_worth_reason": parent["machine_worth_reason"] or "",
                "network_worth": parent["human_worth"] or "",
                "user_worth_note": parent["human_worth_note"] or "",
                "source": parent["human_worth_source"] or parent["source"] or "",
                "updated_at": parent["human_worth_at"] or parent["updated_at"] or "",
            })
            out[f"parent-worth:{parent['parent_id']}"] = row
        return out

    def export_batons(self, review_csv: Path, synthetic_csv: Path | None = None) -> None:
        from packs.ingestion.primitives.deep_context.db import batons

        batons.write_override_rows(review_csv, self.rows())
        if synthetic_csv is None:
            return
        rows = []
        for profile in self.query(
            "SELECT s.*, l.decision_action, l.decision_approved, l.machine_approved "
            "FROM synthetic_profiles s JOIN links l ON l.row_key=s.candidate_key "
            "ORDER BY s.public_identifier"
        ):
            gate = profile["decision_approved"] or profile["machine_approved"] or ""
            rows.append(json.loads(profile["profile_json"]) | {
                "public_identifier": profile["public_identifier"],
                "linkedin_url": profile["linkedin_url"] or "",
                "approved": gate,
            })
        batons.write_synthetic_rows(synthetic_csv, rows)
