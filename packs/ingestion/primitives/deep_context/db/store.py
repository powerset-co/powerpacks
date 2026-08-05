"""Small transaction and projection API for the canonical deep-context DB."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path

from packs.ingestion.primitives.deep_context.db import batons
from packs.ingestion.primitives.deep_context.db.schema import (
    DDL,
    ROW_TYPES,
    SCHEMA_VERSION,
    DecisionKind,
    DecisionRow,
    FactRow,
    GuidanceRow,
    JobRow,
    LinkRow,
    ParentRow,
    PersonRow,
    ResearchRow,
    StageStateRow,
    SyntheticProfileRow,
    VerdictRow,
    PARENT_WORTH_PREFIX,
)


class StoreError(ValueError):
    pass


class SchemaVersionError(StoreError):
    pass


def _upsert_sql(table: str, row_type: type, keys: tuple[str, ...]) -> str:
    names = [field.name for field in fields(row_type)]
    updates = [name for name in names if name not in keys]
    return (
        f"INSERT INTO {table} ({', '.join(names)}) VALUES "
        f"({', '.join(':' + name for name in names)}) ON CONFLICT "
        f"({', '.join(keys)}) DO UPDATE SET "
        + ", ".join(f"{name} = excluded.{name}" for name in updates)
    )


_KEYS = {
    "people": ("person_id",), "parents": ("parent_id",), "links": ("row_key",),
    "decisions": ("kind", "target"), "facts": ("subject_key",),
    "verdicts": ("candidate_key",), "synthetic_profiles": ("public_identifier",),
    "research": ("handle",), "guidance": ("handle",), "jobs": ("name",),
    "stage_state": ("stage",),
}
_UPSERTS = {table: _upsert_sql(table, row_type, _KEYS[table])
            for table, row_type in ROW_TYPES.items()}


class Db:
    """Open a compatible store or create a new one; never discard data."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if not tables:
                conn.executescript(DDL)
                conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                             (str(SCHEMA_VERSION),))
                conn.commit()
            else:
                if "meta" not in tables:
                    raise SchemaVersionError("deep-context DB has no schema version; import it explicitly")
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
                if row is None or row["value"] != str(SCHEMA_VERSION):
                    found = row["value"] if row else "missing"
                    raise SchemaVersionError(
                        f"deep-context DB schema is {found}, expected {SCHEMA_VERSION}; "
                        "migrate into a new canonical DB explicitly")
                try:
                    conn.executescript(DDL)  # adds new tables/indexes, never replaces rows
                except sqlite3.OperationalError as exc:
                    raise SchemaVersionError(
                        "deep-context DB does not match canonical schema; "
                        "migrate into a new canonical DB explicitly") from exc
                self._check_columns(conn)
                conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _check_columns(conn: sqlite3.Connection) -> None:
        for table, row_type in ROW_TYPES.items():
            actual = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            missing = {field.name for field in fields(row_type)} - actual
            if missing:
                raise SchemaVersionError(
                    f"deep-context DB table {table} is missing {sorted(missing)}; "
                    "migrate into a new canonical DB explicitly")

    @contextmanager
    def connect(self):
        """One caller-owned transaction; retained as the views transaction API."""
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

    @staticmethod
    def put(conn: sqlite3.Connection, table: str, row: object) -> None:
        """Upsert inside an existing domain transaction."""
        conn.execute(_UPSERTS[table], asdict(row))

    def _put(self, table: str, row: object) -> None:
        with self.connect() as conn:
            self.put(conn, table, row)

    def upsert_person(self, row: PersonRow) -> None:
        self._put("people", row)

    def upsert_parent(self, row: ParentRow) -> None:
        self._put("parents", row)

    def upsert_link(self, row: LinkRow) -> None:
        self._put("links", row)

    def upsert_decision(self, row: DecisionRow) -> None:
        self._put("decisions", row)

    def upsert_fact(self, row: FactRow) -> None:
        self._put("facts", row)

    def upsert_verdict(self, row: VerdictRow) -> None:
        self._put("verdicts", row)

    def upsert_synthetic_profile(self, row: SyntheticProfileRow) -> None:
        self._put("synthetic_profiles", row)

    def upsert_research(self, row: ResearchRow) -> None:
        self._put("research", row)

    def upsert_guidance(self, row: GuidanceRow) -> None:
        self._put("guidance", row)

    def upsert_job(self, row: JobRow) -> None:
        self._put("jobs", row)

    def upsert_stage_state(self, row: StageStateRow) -> None:
        self._put("stage_state", row)

    def delete_decision(self, kind: str, target: str) -> None:
        """Absence of a decision is the pending state."""
        with self.connect() as conn:
            conn.execute("DELETE FROM decisions WHERE kind = ? AND target = ?", (kind, target))

    def update_link(self, row_key: str, **columns: object) -> None:
        allowed = {field.name for field in fields(LinkRow)} - {"row_key"}
        unknown = set(columns) - allowed
        if not columns or unknown:
            raise StoreError(f"invalid link update columns: {sorted(unknown)}")
        assignments = ", ".join(f"{name} = :{name}" for name in columns)
        with self.connect() as conn:
            changed = conn.execute(
                f"UPDATE links SET {assignments} WHERE row_key = :row_key",
                {**columns, "row_key": row_key}).rowcount
            if changed != 1:
                raise StoreError(f"unknown link: {row_key}")

    @staticmethod
    def _pipe(value: str | None) -> str:
        if value is None:
            return ""
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value
        return "|".join(str(item) for item in parsed)

    @staticmethod
    def _number(value: float | None) -> str:
        return "" if value is None else format(value, "g")

    def rows(self) -> dict[str, dict[str, str]]:
        """Explicit legacy review.csv projection; runtime never reads it back."""
        decisions = {(row["kind"], row["target"]): row
                     for row in self.query("SELECT * FROM decisions")}
        out: dict[str, dict[str, str]] = {}
        for link in self.query("SELECT * FROM links"):
            row = {column: "" for column in batons.OVERRIDE_COLUMNS}
            for column in ("public_identifier", "person_id", "linkedin_url",
                           "new_linkedin_url", "new_public_identifier", "reason",
                           "llm_reject", "llm_reject_reason", "llm_judge_fingerprint",
                           "llm_worth", "llm_worth_reason", "source", "updated_at"):
                row[column] = link[column] or ""
            row["match_emails"], row["match_phones"] = (
                self._pipe(link["match_emails"]), self._pipe(link["match_phones"]))
            row["confidence"] = self._number(link["confidence"])
            row["llm_reject_confidence"] = self._number(link["llm_reject_confidence"])
            identity = decisions.get((DecisionKind.IDENTITY.value, link["row_key"]))
            row["action"] = identity["value"] if identity else (link["proposed_action"] or "")
            if identity:
                row["approved"] = identity["approved"] or ""
                row["source"], row["updated_at"] = identity["source"] or "", identity["decided_at"] or ""
            worth = decisions.get((DecisionKind.WORTH.value, link["row_key"]))
            if worth:
                row["network_worth"], row["user_worth_note"] = worth["value"], worth["note"] or ""
            out[link["row_key"]] = row
        for parent in self.query("SELECT * FROM parents"):
            row = {column: "" for column in batons.OVERRIDE_COLUMNS}
            for column in ("public_identifier", "llm_worth", "llm_worth_reason", "source", "updated_at"):
                row[column] = parent[column] or ""
            row["worth_person_ids"] = self._pipe(parent["worth_person_ids"])
            worth = decisions.get((DecisionKind.WORTH.value, parent["parent_id"]))
            if worth:
                row["network_worth"], row["user_worth_note"] = worth["value"], worth["note"] or ""
            out[PARENT_WORTH_PREFIX + parent["parent_id"]] = row
        return out

    def export_batons(self, review_csv: Path, synthetic_csv: Path | None = None) -> None:
        """Explicit DB -> compatibility files. It is never part of a click."""
        batons.write_override_rows(review_csv, self.rows())
        if synthetic_csv is None:
            return
        gates = {row["target"]: ("auto" if row["approved"] == "auto" else row["value"])
                 for row in self.query(
                     "SELECT target, value, approved FROM decisions WHERE kind = ?",
                     (DecisionKind.SYNTHETIC_GATE.value,))}
        profiles = [json.loads(row["profile_json"] or "{}") | {
            "public_identifier": row["public_identifier"],
            "linkedin_url": row["linkedin_url"] or "",
            "approved": gates.get(row["public_identifier"], ""),
        } for row in self.query("SELECT * FROM synthetic_profiles ORDER BY public_identifier")]
        batons.write_synthetic_rows(synthetic_csv, profiles)
