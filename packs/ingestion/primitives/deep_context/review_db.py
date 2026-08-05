"""review.sqlite — the typed decision store behind review.csv (phase 0).

Flow: `ReviewDb(db_path)` opens/creates the schema (WAL, checks generated from
the review_store enums). `import_stores(review_csv, synthetic_csv)` rebuilds
the tables from the CSVs in one transaction — every non-parent review row
becomes a `links` row (its key namespace typed in `kind`), `parent-worth:*`
rows become `parents`, and every terminal outcome (approved != '',
network_worth, the synthetic approved gate) becomes a `decisions` row.
`export_review_csv` / `export_synthetic_gates` recompose the CSVs
byte-identically through the same loader/writer pair the CSV world uses.

Cell homes (each review.csv column has exactly one):
  links    row_key(+verbatim public_identifier), kind, person_id, linkedin_url,
           proposed_action, new_linkedin_url, new_public_identifier,
           confidence(+generated REAL), reason, match_emails, match_phones,
           llm_reject(+confidence/reason), llm_judge_fingerprint (paid cache
           key — copied verbatim, never regenerated), llm_worth(+reason)
           mirror cells, source, updated_at
  parents  parent_id(+verbatim public_identifier), worth_person_ids,
           llm_worth(+reason), source, updated_at
  decisions  action/approved of settled identity rows; network_worth +
           user_worth_note (kind=worth); synthetic approved gate
           (kind=synthetic_gate). Absence of a row == pending.

Import is strict: an unknown source/action/worth value, an approved cell
without an action, worth membership outside a parent row, or identity cells on
a parent row raise with the offending keys named — unrepresentable states fail
at the door instead of becoming queue ghosts.

Changelog:
  2026-08-05: phase 0 (schema + import/export). Ground-truth revisions vs the
    2026-08-04 design doc: `approved` stays a decision column (real stores
    carry approved=auto with human sources, so approved-class is NOT derivable
    from source-class); uuid-keyed rows are links rows (jake has 1244 with
    identity cells), so there is no separate people table yet — reference
    identities from index.json arrive with the phase-1 read path;
    worth_person_ids lives on parents (machine membership bookkeeping), not on
    the worth decision.
"""
from __future__ import annotations

import csv
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.review_store import (
    ApprovedState,
    HumanWorth,
    MachineWorth,
    OVERRIDE_COLUMNS,
    PARENT_WORTH_PREFIX,
    ReviewAction,
    ReviewSource,
    load_override_rows,
    write_override_rows,
)

SCHEMA_VERSION = 1

# review.csv stores llm_reject as free text; the closed set observed across
# real stores plus the retired spam-screen value synthesis still scrubs.
LLM_REJECT_VALUES = ("yes", "no", "spam")


class DecisionKind(StrEnum):
    IDENTITY = "identity"          # settled action on a links row
    WORTH = "worth"                # human network_worth on a parent (or row)
    SYNTHETIC_GATE = "synthetic_gate"  # synthetic-people.csv approved gate


class RowKind(StrEnum):
    """The typed spelling of review.csv's key namespaces."""

    PUB = "pub"                          # plain LinkedIn public identifier
    PERSON_UUID = "person_uuid"          # bare directory person-id UUID
    CANDIDATE_EMAIL = "candidate_email"  # candidate:email:<addr>
    CANDIDATE_PHONE = "candidate_phone"  # candidate:phone:<digits>
    MESSAGE_LINKEDIN = "message_linkedin"  # message-linkedin:<hash> (no pub)
    PARENT = "parent"                    # parent-worth:<parent_id>


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def classify_review_key(key: str) -> RowKind:
    """THE namespace decision for a normalized review.csv key."""
    if key.startswith(PARENT_WORTH_PREFIX):
        return RowKind.PARENT
    if key.startswith("candidate:email:"):
        return RowKind.CANDIDATE_EMAIL
    if key.startswith("candidate:phone:"):
        return RowKind.CANDIDATE_PHONE
    if key.startswith("message-linkedin:"):
        return RowKind.MESSAGE_LINKEDIN
    if _UUID_RE.match(key):
        return RowKind.PERSON_UUID
    return RowKind.PUB


@dataclass(frozen=True)
class LinkRow:
    """One identity row (any non-parent review.csv key), parsed at the boundary."""

    row_key: str
    public_identifier: str
    kind: str
    person_id: str = ""
    linkedin_url: str = ""
    proposed_action: str = ""
    new_linkedin_url: str = ""
    new_public_identifier: str = ""
    confidence: str = ""
    reason: str = ""
    match_emails: str = ""
    match_phones: str = ""
    llm_reject: str = ""
    llm_reject_confidence: str = ""
    llm_reject_reason: str = ""
    llm_judge_fingerprint: str = ""
    llm_worth: str = ""
    llm_worth_reason: str = ""
    source: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ParentRow:
    """One parent-worth:* row: machine worth bookkeeping for a cluster."""

    parent_id: str
    public_identifier: str
    worth_person_ids: str = ""
    llm_worth: str = ""
    llm_worth_reason: str = ""
    source: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class DecisionRow:
    """One terminal outcome; absence of a row == pending."""

    kind: str
    target: str
    value: str
    approved: str = ""
    source: str = ""
    note: str = ""
    decided_at: str = ""


def _insert_sql(table: str, row_type: type) -> str:
    """INSERT statement generated from the dataclass fields — the dataclass is
    the one home for each table's writable columns (generated columns excluded
    by construction; a drift from the DDL fails the insert loudly)."""
    names = [field.name for field in fields(row_type)]
    return "INSERT INTO {} ({}) VALUES ({})".format(
        table, ", ".join(names), ", ".join(f":{name}" for name in names)
    )


def _sql_in(values) -> str:
    quoted = ", ".join("'{}'".format(str(getattr(v, "value", v))) for v in values)
    return f"({quoted})"


_LINK_KINDS = _sql_in([k for k in RowKind if k is not RowKind.PARENT])
_SOURCES = _sql_in([""] + list(ReviewSource))
_ACTIONS = _sql_in([""] + list(ReviewAction))
_MACHINE_WORTH = _sql_in([""] + list(MachineWorth))
_REJECTS = _sql_in(("",) + LLM_REJECT_VALUES)

SCHEMA_DDL = f"""
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS links (
  row_key           TEXT PRIMARY KEY
                      CHECK (row_key != '' AND row_key NOT LIKE '{PARENT_WORTH_PREFIX}%'),
  public_identifier TEXT NOT NULL,           -- verbatim CSV cell (row_key is its normalized form)
  kind              TEXT NOT NULL CHECK (kind IN {_LINK_KINDS}),
  person_id         TEXT NOT NULL DEFAULT '',
  linkedin_url      TEXT NOT NULL DEFAULT '',
  proposed_action   TEXT NOT NULL DEFAULT '' CHECK (proposed_action IN {_ACTIONS}),
  new_linkedin_url  TEXT NOT NULL DEFAULT '',
  new_public_identifier TEXT NOT NULL DEFAULT '',
  confidence        TEXT NOT NULL DEFAULT '',
  confidence_num    REAL GENERATED ALWAYS AS (CAST(NULLIF(confidence, '') AS REAL)) VIRTUAL,
  reason            TEXT NOT NULL DEFAULT '',
  match_emails      TEXT NOT NULL DEFAULT '',
  match_phones      TEXT NOT NULL DEFAULT '',
  llm_reject        TEXT NOT NULL DEFAULT '' CHECK (llm_reject IN {_REJECTS}),
  llm_reject_confidence TEXT NOT NULL DEFAULT '',
  llm_reject_confidence_num REAL GENERATED ALWAYS AS (CAST(NULLIF(llm_reject_confidence, '') AS REAL)) VIRTUAL,
  llm_reject_reason TEXT NOT NULL DEFAULT '',
  llm_judge_fingerprint TEXT NOT NULL DEFAULT '',
  llm_worth         TEXT NOT NULL DEFAULT '' CHECK (llm_worth IN {_MACHINE_WORTH}),
  llm_worth_reason  TEXT NOT NULL DEFAULT '',
  source            TEXT NOT NULL DEFAULT '' CHECK (source IN {_SOURCES}),
  updated_at        TEXT NOT NULL DEFAULT '',
  CHECK ((kind = 'candidate_email') = (row_key LIKE 'candidate:email:%')),
  CHECK ((kind = 'candidate_phone') = (row_key LIKE 'candidate:phone:%')),
  CHECK ((kind = 'message_linkedin') = (row_key LIKE 'message-linkedin:%'))
);
CREATE INDEX IF NOT EXISTS links_by_person ON links(person_id);

CREATE TABLE IF NOT EXISTS parents (
  parent_id         TEXT PRIMARY KEY CHECK (parent_id != ''),
  public_identifier TEXT NOT NULL,           -- verbatim CSV cell ('{PARENT_WORTH_PREFIX}<parent_id>')
  worth_person_ids  TEXT NOT NULL DEFAULT '',
  llm_worth         TEXT NOT NULL DEFAULT '' CHECK (llm_worth IN {_MACHINE_WORTH}),
  llm_worth_reason  TEXT NOT NULL DEFAULT '',
  source            TEXT NOT NULL DEFAULT '' CHECK (source IN {_SOURCES}),
  updated_at        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS decisions (
  kind       TEXT NOT NULL CHECK (kind IN {_sql_in(DecisionKind)}),
  target     TEXT NOT NULL CHECK (target != ''),
  value      TEXT NOT NULL,
  approved   TEXT NOT NULL DEFAULT '',
  source     TEXT NOT NULL DEFAULT '' CHECK (source IN {_SOURCES}),
  note       TEXT NOT NULL DEFAULT '',
  decided_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (kind, target),
  CHECK ((kind = 'identity' AND value IN {_sql_in(ReviewAction)})
         OR (kind != 'identity' AND value IN {_sql_in(HumanWorth)})),
  CHECK ((kind = 'identity' AND approved IN {_sql_in(ApprovedState)})
         OR (kind != 'identity' AND approved = ''))
);
"""

# Cells a parent-worth row may carry; anything else on one is a storage bug.
_PARENT_CELLS = {
    "public_identifier", "worth_person_ids", "llm_worth", "llm_worth_reason",
    "network_worth", "user_worth_note", "source", "updated_at",
}


class ReviewDbImportError(ValueError):
    """The CSV holds a state the schema refuses; offenders are named."""


class ReviewDb:
    """The review.sqlite store — a stateless facade over db_path.

    Every operation opens its own short-lived connection (WAL makes that
    cheap and correct), so one ReviewDb can be shared across the review
    server's handler threads with no locks and no check_same_thread games;
    a phase-2 decision transaction is simply `with db.connect() as conn:`
    on its own connection with BEGIN IMMEDIATE semantics.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect():
            pass  # fail early: creates the file and the schema

    @contextmanager
    def connect(self):
        """One connection: pragmas + schema ensured; commit on success,
        rollback on error, always closed."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(SCHEMA_DDL)
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

    def checkpoint(self) -> None:
        """Fold the WAL into the main file so review.sqlite is self-contained
        (clean server exit; before any file-level copy)."""
        with self.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def backup_to(self, path: Path) -> None:
        """Snapshot the live db (never rename a WAL db — this is the .bkup door)."""
        target = sqlite3.connect(str(path))
        try:
            with self.connect() as conn, target:
                conn.backup(target)
        finally:
            target.close()

    # -- import ------------------------------------------------------------

    def needs_import(self, review_csv: Path) -> bool:
        try:
            stat = review_csv.stat()
        except OSError:
            return False
        rows = self.query("SELECT value FROM meta WHERE key = 'review_csv_stat'")
        return not rows or rows[0]["value"] != f"{stat.st_mtime_ns}:{stat.st_size}"

    def import_stores(self, review_csv: Path, synthetic_csv: Path | None = None) -> dict:
        """Rebuild every table from the CSVs in one transaction (idempotent)."""
        rows = load_override_rows(review_csv)
        errors: list[str] = []
        links: list[LinkRow] = []
        parents: list[ParentRow] = []
        decisions: list[DecisionRow] = []

        def cell(row: dict, column: str) -> str:
            """Verbatim cell — storage never normalizes, only validation does."""
            return str(row.get(column) or "")

        for key, row in rows.items():
            kind = classify_review_key(key)
            source = cell(row, "source")
            action = cell(row, "action")
            approved = cell(row, "approved")
            worth = cell(row, "network_worth")
            if source and source not in set(ReviewSource):
                errors.append(f"{key}: unknown source '{source}'")
            if action and action not in set(ReviewAction):
                errors.append(f"{key}: unknown action '{action}'")
            if approved and approved not in set(ApprovedState):
                errors.append(f"{key}: unknown approved '{approved}'")
            if approved and not action:
                errors.append(f"{key}: approved without an action")
            if worth and worth not in set(HumanWorth):
                errors.append(f"{key}: unknown network_worth '{worth}'")
            llm_worth = cell(row, "llm_worth")
            if llm_worth and llm_worth not in set(MachineWorth):
                errors.append(f"{key}: unknown llm_worth '{llm_worth}'")
            reject = cell(row, "llm_reject")
            if reject and reject not in LLM_REJECT_VALUES:
                errors.append(f"{key}: unknown llm_reject '{reject}'")
            if kind is not RowKind.PARENT and cell(row, "worth_person_ids"):
                errors.append(f"{key}: worth_person_ids outside a parent-worth row")
            if kind is RowKind.PARENT:
                stray = [c for c in OVERRIDE_COLUMNS if cell(row, c) and c not in _PARENT_CELLS]
                if stray:
                    errors.append(f"{key}: parent-worth row carries identity cells {stray}")
                    continue
                parent_id = key.removeprefix(PARENT_WORTH_PREFIX)
                parents.append(ParentRow(
                    parent_id=parent_id,
                    public_identifier=cell(row, "public_identifier"),
                    worth_person_ids=cell(row, "worth_person_ids"),
                    llm_worth=cell(row, "llm_worth"),
                    llm_worth_reason=cell(row, "llm_worth_reason"),
                    source=source,
                    updated_at=cell(row, "updated_at"),
                ))
                if worth:
                    decisions.append(DecisionRow(
                        kind=DecisionKind.WORTH.value, target=parent_id,
                        value=worth, source=source,
                        note=cell(row, "user_worth_note"),
                        decided_at=cell(row, "updated_at"),
                    ))
                continue

            links.append(LinkRow(
                row_key=key,
                public_identifier=cell(row, "public_identifier"),
                kind=kind.value,
                person_id=cell(row, "person_id"),
                linkedin_url=cell(row, "linkedin_url"),
                proposed_action=action,
                new_linkedin_url=cell(row, "new_linkedin_url"),
                new_public_identifier=cell(row, "new_public_identifier"),
                confidence=cell(row, "confidence"),
                reason=cell(row, "reason"),
                match_emails=cell(row, "match_emails"),
                match_phones=cell(row, "match_phones"),
                llm_reject=cell(row, "llm_reject"),
                llm_reject_confidence=cell(row, "llm_reject_confidence"),
                llm_reject_reason=cell(row, "llm_reject_reason"),
                llm_judge_fingerprint=cell(row, "llm_judge_fingerprint"),
                llm_worth=cell(row, "llm_worth"),
                llm_worth_reason=cell(row, "llm_worth_reason"),
                source=source,
                updated_at=cell(row, "updated_at"),
            ))
            if approved:
                decisions.append(DecisionRow(
                    kind=DecisionKind.IDENTITY.value, target=key,
                    value=action, approved=approved, source=source,
                    decided_at=cell(row, "updated_at"),
                ))
            if worth:
                decisions.append(DecisionRow(
                    kind=DecisionKind.WORTH.value, target=key,
                    value=worth, source=source,
                    note=cell(row, "user_worth_note"),
                    decided_at=cell(row, "updated_at"),
                ))

        gates, gate_errors = self._read_synthetic_gates(synthetic_csv)
        errors.extend(gate_errors)
        decisions.extend(gates)
        if errors:
            shown = "; ".join(errors[:10])
            raise ReviewDbImportError(
                f"{len(errors)} unrepresentable row(s) — fix or scrub before import: {shown}"
            )

        with self.connect() as conn:
            conn.execute("DELETE FROM links")
            conn.execute("DELETE FROM parents")
            conn.execute("DELETE FROM decisions")
            conn.executemany(_insert_sql("links", LinkRow), [asdict(r) for r in links])
            conn.executemany(_insert_sql("parents", ParentRow), [asdict(r) for r in parents])
            conn.executemany(_insert_sql("decisions", DecisionRow), [asdict(r) for r in decisions])
            stat = review_csv.stat()
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES "
                "('schema_version', :v), ('imported_at', :t), ('review_csv_stat', :s)",
                {"v": str(SCHEMA_VERSION), "t": now_iso(), "s": f"{stat.st_mtime_ns}:{stat.st_size}"},
            )
        return {
            "links": len(links), "parents": len(parents),
            "decisions": len(decisions) - len(gates), "synthetic_gates": len(gates),
        }

    def _read_synthetic_gates(self, synthetic_csv: Path | None) -> tuple[list[DecisionRow], list[str]]:
        gates: list[DecisionRow] = []
        errors: list[str] = []
        if not synthetic_csv or not synthetic_csv.exists():
            return gates, errors
        with synthetic_csv.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                pub = str(row.get("public_identifier") or "").strip().lower()
                approved = str(row.get("approved") or "").strip().lower()
                if not approved:
                    continue
                if not pub:
                    errors.append("synthetic-people.csv: approved gate on a row without public_identifier")
                    continue
                if approved not in set(HumanWorth):
                    errors.append(f"synthetic:{pub}: unknown approved '{approved}'")
                    continue
                gates.append(DecisionRow(
                    kind=DecisionKind.SYNTHETIC_GATE.value, target=pub, value=approved,
                ))
        return gates, errors

    # -- export ------------------------------------------------------------

    def export_review_rows(self) -> dict[str, dict[str, str]]:
        """Recompose review.csv rows (the inverse of import_stores)."""
        decided = {
            (d["kind"], d["target"]): d
            for d in self.query("SELECT * FROM decisions")
        }
        rows: dict[str, dict[str, str]] = {}
        for link in self.query("SELECT * FROM links"):
            row = {column: "" for column in OVERRIDE_COLUMNS}
            for column in ("public_identifier", "person_id", "linkedin_url",
                           "new_linkedin_url", "new_public_identifier", "confidence",
                           "reason", "match_emails", "match_phones", "llm_reject",
                           "llm_reject_confidence", "llm_reject_reason",
                           "llm_judge_fingerprint", "llm_worth", "llm_worth_reason",
                           "source", "updated_at"):
                row[column] = link[column]
            identity = decided.get((DecisionKind.IDENTITY.value, link["row_key"]))
            if identity is not None:
                row["action"] = identity["value"]
                row["approved"] = identity["approved"]
                row["source"] = identity["source"]
                row["updated_at"] = identity["decided_at"]
            else:
                row["action"] = link["proposed_action"]
            worth = decided.get((DecisionKind.WORTH.value, link["row_key"]))
            if worth is not None:
                row["network_worth"] = worth["value"]
                row["user_worth_note"] = worth["note"]
            rows[link["row_key"]] = row
        for parent in self.query("SELECT * FROM parents"):
            row = {column: "" for column in OVERRIDE_COLUMNS}
            row["public_identifier"] = parent["public_identifier"]
            row["worth_person_ids"] = parent["worth_person_ids"]
            row["llm_worth"] = parent["llm_worth"]
            row["llm_worth_reason"] = parent["llm_worth_reason"]
            row["source"] = parent["source"]
            row["updated_at"] = parent["updated_at"]
            worth = decided.get((DecisionKind.WORTH.value, parent["parent_id"]))
            if worth is not None:
                row["network_worth"] = worth["value"]
                row["user_worth_note"] = worth["note"]
            rows[PARENT_WORTH_PREFIX + parent["parent_id"]] = row
        return rows

    def export_review_csv(self, path: Path) -> int:
        rows = self.export_review_rows()
        write_override_rows(path, rows)
        return len(rows)

    def export_synthetic_gates(self, synthetic_csv: Path) -> int:
        """Rewrite ONLY the approved column of synthetic-people.csv from decisions."""
        if not synthetic_csv.exists():
            return 0
        with synthetic_csv.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        if "approved" not in fieldnames:
            return 0
        gates = {
            row["target"]: row["value"]
            for row in self.query(
                "SELECT target, value FROM decisions WHERE kind = ?",
                (DecisionKind.SYNTHETIC_GATE.value,),
            )
        }
        changed = 0
        for row in rows:
            pub = str(row.get("public_identifier") or "").strip().lower()
            value = gates.get(pub, "")
            if str(row.get("approved") or "") != value:
                changed += 1
            row["approved"] = value
        with synthetic_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in fieldnames})
        return changed
