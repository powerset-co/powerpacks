"""The deep-context store: one sqlite db, upserts in, queries out.

Rule zero (single-user local tool): the db is the record, the CSVs are
re-derivable export batons. No locks, no recovery flags, no version ceremony
— sqlite's default transaction per connection is the whole concurrency story,
and a schema mismatch drops and rebuilds from the batons.

A user action is ONE upsert (upsert_decision / update_link) followed by
export_batons — never a whole-store rewrite. Full derive-and-replace exists
only at import_batons, the boundary that absorbs an externally produced CSV.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db import batons
from packs.ingestion.primitives.deep_context.db.schema import (
    DDL,
    SCHEMA_VERSION,
    ApprovedState,
    DecisionKind,
    DecisionRow,
    HumanWorth,
    LinkRow,
    LLM_REJECT_VALUES,
    MachineWorth,
    ParentRow,
    PARENT_WORTH_PREFIX,
    ReviewAction,
    ReviewSource,
    RowKind,
    classify_review_key,
)


class StoreError(ValueError):
    """A baton holds a state the schema refuses; offenders are named."""


def _insert_sql(table: str, row_type: type, replace: bool = False) -> str:
    names = [field.name for field in fields(row_type)]
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    return "{} INTO {} ({}) VALUES ({})".format(
        verb, table, ", ".join(names), ", ".join(f":{name}" for name in names))


# Columns of a review row that belong to the links table verbatim.
_LINK_COLUMNS = [f.name for f in fields(LinkRow) if f.name not in ("row_key", "kind", "proposed_action")]


class Db:
    """Construct-and-use over db_path; every method opens its own connection."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.row_factory = sqlite3.Row
            has_meta = conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'meta'").fetchone()
            if has_meta:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
                if row is not None and row["value"] != str(SCHEMA_VERSION):
                    for table in ("links", "parents", "people", "decisions", "meta"):
                        conn.execute(f"DROP TABLE IF EXISTS {table}")
                    conn.commit()
            conn.executescript(DDL)
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    # -- writes (the click path) -------------------------------------------

    def upsert_decision(self, decision: DecisionRow) -> None:
        with self.connect() as conn:
            conn.execute(_insert_sql("decisions", DecisionRow, replace=True),
                         asdict(decision))

    def delete_decision(self, kind: str, target: str) -> None:
        """Absence == pending; deleting a decision IS the reset."""
        with self.connect() as conn:
            conn.execute("DELETE FROM decisions WHERE kind = ? AND target = ?",
                         (kind, target))

    def update_link(self, row_key: str, **columns: str) -> None:
        """Targeted machine-state update (fix/retarget proposal fields)."""
        assignments = ", ".join(f"{name} = :{name}" for name in columns)
        with self.connect() as conn:
            conn.execute(f"UPDATE links SET {assignments} WHERE row_key = :row_key",
                         {**columns, "row_key": row_key})

    def upsert_link(self, link: LinkRow) -> None:
        with self.connect() as conn:
            conn.execute(_insert_sql("links", LinkRow, replace=True), asdict(link))

    # -- baton import (the boundary; strict, full replace) ------------------

    def needs_import(self, review_csv: Path, index_json: Path | None = None) -> bool:
        try:
            stat = review_csv.stat()
        except OSError:
            return False
        rows = self.query("SELECT value FROM meta WHERE key = 'review_csv_stat'")
        if not rows or rows[0]["value"] != f"{stat.st_mtime_ns}:{stat.st_size}":
            return True
        if index_json is not None and index_json.exists():
            istat = index_json.stat()
            irows = self.query("SELECT value FROM meta WHERE key = 'index_json_stat'")
            return not irows or irows[0]["value"] != f"{istat.st_mtime_ns}:{istat.st_size}"
        return False

    def import_batons(self, review_csv: Path, synthetic_csv: Path | None = None,
                      index_json: Path | None = None) -> dict:
        rows = batons.load_override_rows(review_csv)
        links, parents, decisions = _derive(rows)
        gates, gate_errors = batons.read_synthetic_gates(synthetic_csv)
        if gate_errors:
            raise StoreError("; ".join(gate_errors[:10]))
        decisions.extend(gates)
        people = (batons.people_from_index(index_json)
                  if index_json is not None and index_json.exists() else None)
        with self.connect() as conn:
            conn.execute("DELETE FROM links")
            conn.execute("DELETE FROM parents")
            conn.execute("DELETE FROM decisions")
            conn.executemany(_insert_sql("links", LinkRow), [asdict(r) for r in links])
            conn.executemany(_insert_sql("parents", ParentRow), [asdict(r) for r in parents])
            conn.executemany(_insert_sql("decisions", DecisionRow), [asdict(r) for r in decisions])
            if people is not None:
                conn.execute("DELETE FROM people")
                conn.executemany(
                    "INSERT INTO people (person_id, parent_id, child_slug, parent_slug) "
                    "VALUES (:person_id, :parent_id, :child_slug, :parent_slug)", people)
                istat = index_json.stat()
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('index_json_stat', ?)",
                             (f"{istat.st_mtime_ns}:{istat.st_size}",))
            stat = review_csv.stat()
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES "
                "('schema_version', :v), ('imported_at', :t), ('review_csv_stat', :s)",
                {"v": str(SCHEMA_VERSION), "t": now_iso(), "s": f"{stat.st_mtime_ns}:{stat.st_size}"})
        return {"links": len(links), "parents": len(parents),
                "decisions": len(decisions) - len(gates), "synthetic_gates": len(gates),
                "people": len(people) if people is not None else 0}

    # -- baton export (db -> CSVs; the record never depends on it) ----------

    def rows(self) -> dict[str, dict[str, str]]:
        """Baton-shaped review rows composed from the tables."""
        decided = {(d["kind"], d["target"]): d for d in self.query("SELECT * FROM decisions")}
        out: dict[str, dict[str, str]] = {}
        for link in self.query("SELECT * FROM links"):
            row = {column: "" for column in batons.OVERRIDE_COLUMNS}
            for column in _LINK_COLUMNS:
                row[column] = link[column]
            identity = decided.get((DecisionKind.IDENTITY.value, link["row_key"]))
            if identity is not None:
                row["action"], row["approved"] = identity["value"], identity["approved"]
                row["source"], row["updated_at"] = identity["source"], identity["decided_at"]
            else:
                row["action"] = link["proposed_action"]
            worth = decided.get((DecisionKind.WORTH.value, link["row_key"]))
            if worth is not None:
                row["network_worth"], row["user_worth_note"] = worth["value"], worth["note"]
            out[link["row_key"]] = row
        for parent in self.query("SELECT * FROM parents"):
            row = {column: "" for column in batons.OVERRIDE_COLUMNS}
            for column in ("public_identifier", "worth_person_ids", "llm_worth",
                           "llm_worth_reason", "source", "updated_at"):
                row[column] = parent[column]
            worth = decided.get((DecisionKind.WORTH.value, parent["parent_id"]))
            if worth is not None:
                row["network_worth"], row["user_worth_note"] = worth["value"], worth["note"]
            out[PARENT_WORTH_PREFIX + parent["parent_id"]] = row
        return out

    def export_batons(self, review_csv: Path, synthetic_csv: Path | None = None) -> None:
        batons.write_override_rows(review_csv, self.rows())
        if synthetic_csv is not None:
            gates = {row["target"]: ("auto" if row["approved"] == "auto" else row["value"])
                     for row in self.query(
                         "SELECT target, value, approved FROM decisions WHERE kind = ?",
                         (DecisionKind.SYNTHETIC_GATE.value,))}
            batons.write_synthetic_gates(synthetic_csv, gates)
        stat = review_csv.stat()
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('review_csv_stat', ?)",
                         (f"{stat.st_mtime_ns}:{stat.st_size}",))


# Cells a parent-worth row may carry; anything else on one is a storage bug.
_PARENT_CELLS = {"public_identifier", "worth_person_ids", "llm_worth", "llm_worth_reason",
                 "network_worth", "user_worth_note", "source", "updated_at"}


def _derive(rows: dict[str, dict[str, str]],
            ) -> tuple[list[LinkRow], list[ParentRow], list[DecisionRow]]:
    """Baton rows -> typed table rows; refuses unrepresentable states by name."""
    errors: list[str] = []
    links: list[LinkRow] = []
    parents: list[ParentRow] = []
    decisions: list[DecisionRow] = []

    def cell(row: dict, column: str) -> str:
        return str(row.get(column) or "")

    for key, row in rows.items():
        kind = classify_review_key(key)
        source, action = cell(row, "source"), cell(row, "action")
        approved, worth = cell(row, "approved"), cell(row, "network_worth")
        for value, allowed, label in (
            (source, {""} | set(ReviewSource), "source"),
            (action, {""} | set(ReviewAction), "action"),
            (approved, {""} | set(ApprovedState), "approved"),
            (worth, {""} | set(HumanWorth), "network_worth"),
            (cell(row, "llm_worth"), {""} | set(MachineWorth), "llm_worth"),
            (cell(row, "llm_reject"), {""} | set(LLM_REJECT_VALUES), "llm_reject"),
        ):
            if value not in allowed:
                errors.append(f"{key}: unknown {label} '{value}'")
        if approved and not action:
            errors.append(f"{key}: approved without an action")
        if kind is not RowKind.PARENT and cell(row, "worth_person_ids"):
            errors.append(f"{key}: worth_person_ids outside a parent-worth row")
        if kind is RowKind.PARENT:
            stray = [c for c in batons.OVERRIDE_COLUMNS if cell(row, c) and c not in _PARENT_CELLS]
            if stray:
                errors.append(f"{key}: parent-worth row carries identity cells {stray}")
                continue
            parent_id = key.removeprefix(PARENT_WORTH_PREFIX)
            parents.append(ParentRow(
                parent_id=parent_id, public_identifier=cell(row, "public_identifier"),
                worth_person_ids=cell(row, "worth_person_ids"),
                llm_worth=cell(row, "llm_worth"), llm_worth_reason=cell(row, "llm_worth_reason"),
                source=source, updated_at=cell(row, "updated_at")))
            if worth:
                decisions.append(DecisionRow(
                    kind=DecisionKind.WORTH.value, target=parent_id, value=worth,
                    source=source, note=cell(row, "user_worth_note"),
                    decided_at=cell(row, "updated_at")))
            continue
        links.append(LinkRow(
            row_key=key, kind=kind.value, proposed_action=action,
            **{column: cell(row, column) for column in _LINK_COLUMNS}))
        if approved:
            decisions.append(DecisionRow(
                kind=DecisionKind.IDENTITY.value, target=key, value=action,
                approved=approved, source=source, decided_at=cell(row, "updated_at")))
        if worth:
            decisions.append(DecisionRow(
                kind=DecisionKind.WORTH.value, target=key, value=worth,
                source=source, note=cell(row, "user_worth_note"),
                decided_at=cell(row, "updated_at")))
    if errors:
        raise StoreError(
            f"{len(errors)} unrepresentable row(s) — fix or scrub before import: "
            + "; ".join(errors[:10]))
    return links, parents, decisions
