"""Narrow projector and domain-transaction API for Deep Context SQLite."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.identity_policy import IdentityPolicy
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactProjection,
    ArtifactReplacement,
    ArtifactRow,
    CandidatePeopleProjection,
    DerivedResetCounts,
    FactRow,
    GuidanceRow,
    HUMAN_DECISION_SOURCES,
    HumanWorth,
    IdentityMachineProjection,
    JobRow,
    JobStatus,
    LinkRow,
    MergeVerdictRow,
    OwnerContextRow,
    ParentRow,
    PersonIdentifiersProjection,
    PersonRow,
    PersonSourcesProjection,
    ResearchRow,
    ResetReviewCounts,
    ReviewAction,
    ReviewSource,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db.schema import (
    DDL,
    SCHEMA_VERSION,
    TABLE_BY_TYPE,
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


_signature_db = sqlite3.connect(":memory:")
_signature_db.executescript(DDL)
EXPECTED_SCHEMA_SIGNATURE = _schema_signature(_signature_db)
_signature_db.close()


_CHILD_KEYS = {
    "person_identifiers": "person_id",
    "person_sources": "person_id",
    "candidate_people": "row_key",
}

_CHILD_TABLES = {
    PersonIdentifiersProjection: "person_identifiers",
    PersonSourcesProjection: "person_sources",
    CandidatePeopleProjection: "candidate_people",
}

_MACHINE_FIELDS = tuple(
    name for name in IdentityMachineProjection.__dataclass_fields__ if name != "row_key"
)
_IDENTITY_UPDATE = "UPDATE links SET {} WHERE row_key=:row_key".format(
    ", ".join(f"{name}=:{name}" for name in _MACHINE_FIELDS)
)
_DIRECT_HUMAN_SOURCES = tuple(sorted(HUMAN_DECISION_SOURCES))
_HUMAN_SOURCES = (*_DIRECT_HUMAN_SOURCES, ReviewSource.SIBLING_SETTLE.value)
_HUMAN_SOURCE_SLOTS = ",".join("?" for _ in _HUMAN_SOURCES)
_CLEAR_IDENTITY = (
    "UPDATE links SET decision_action=NULL, decision_approved=NULL, decision_source=NULL, "
    "decision_note=NULL, decided_at=NULL, replacement_url=NULL, "
    f"replacement_public_identifier=NULL WHERE decision_source IN ({_HUMAN_SOURCE_SLOTS})"
)
_HAS_HUMAN_WORTH = (
    "human_worth IS NOT NULL OR human_worth_note IS NOT NULL OR "
    "human_worth_source IS NOT NULL OR human_worth_at IS NOT NULL"
)
_PARENT_OWNER_TABLES = (
    "people", "links", "candidate_people", "artifacts", "facts",
    "research", "guidance", "jobs",
)


class Db:
    """Open an exact current store or create one; failures never change existing files."""

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

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.transaction() as conn:
            return conn.execute(sql, params).fetchall()

    def _write(self, table: str, row: object, conn: sqlite3.Connection) -> None:
        conn.execute(UPSERTS[table], asdict(row))

    def _replace_children(
        self, table: str, key: str, rows: tuple,
        *, conn: sqlite3.Connection,
    ) -> None:
        key_column = _CHILD_KEYS[table]
        if any(getattr(row, key_column) != key for row in rows):
            raise StoreError(f"{table} owner does not match {key_column}")
        conn.execute(f"DELETE FROM {table} WHERE {key_column}=?", (key,))
        conn.executemany(UPSERTS[table], [asdict(row) for row in rows])

    def merge_parents(self, survivor_parent_id: str, absorbed_parent_id: str) -> None:
        """Atomically absorb one parent family into a surviving parent."""
        if survivor_parent_id == absorbed_parent_id:
            raise StoreError("cannot merge a parent into itself")
        with self.transaction() as conn:
            conn.execute("PRAGMA defer_foreign_keys=ON")
            if not conn.in_transaction:
                conn.execute("BEGIN DEFERRED")
            parents = {
                row["parent_id"]: row
                for row in conn.execute(
                    "SELECT * FROM parents WHERE parent_id IN (?, ?)",
                    (survivor_parent_id, absorbed_parent_id),
                )
            }
            missing = [
                parent_id for parent_id in (survivor_parent_id, absorbed_parent_id)
                if parent_id not in parents
            ]
            if missing:
                raise StoreError(f"unknown parent: {missing[0]}")

            survivor = parents[survivor_parent_id]
            absorbed = parents[absorbed_parent_id]
            if absorbed["human_worth"] is not None and (
                survivor["human_worth"] is None
                or (absorbed["human_worth_at"] or "")
                > (survivor["human_worth_at"] or "")
            ):
                conn.execute(
                    "UPDATE parents SET human_worth=?, human_worth_note=?, "
                    "human_worth_source=?, human_worth_at=? WHERE parent_id=?",
                    (
                        absorbed["human_worth"],
                        absorbed["human_worth_note"],
                        absorbed["human_worth_source"],
                        absorbed["human_worth_at"],
                        survivor_parent_id,
                    ),
                )
            for table in _PARENT_OWNER_TABLES:
                conn.execute(
                    f"UPDATE {table} SET parent_id=? WHERE parent_id=?",
                    (survivor_parent_id, absorbed_parent_id),
                )
            conn.execute(
                "UPDATE people SET parent_slug=? WHERE parent_id=?",
                (survivor["display_slug"], survivor_parent_id),
            )
            IdentityPolicy.settle_human_families(conn, (survivor_parent_id,))
            IdentityPolicy.clear_machine_winner_conflicts(conn, (survivor_parent_id,))
            conn.execute("DELETE FROM parents WHERE parent_id=?", (absorbed_parent_id,))
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise StoreError(f"parent merge violates foreign keys: {violations[0]}")

    def _project_candidate(self, row: LinkRow, *, conn: sqlite3.Connection) -> None:
        current = conn.execute(
            "SELECT parent_id, kind FROM links WHERE row_key=?", (row.row_key,)
        ).fetchone()
        if current and tuple(current) != (row.parent_id, row.kind):
            raise StoreError(f"candidate owner/kind changed: {row.row_key}")
        conn.execute(UPSERTS["links"], asdict(row))
        source_slots = ",".join("?" for _ in _DIRECT_HUMAN_SOURCES)
        winner = conn.execute(
            "SELECT row_key, decided_at FROM links WHERE parent_id=? "
            f"AND decision_action IS NOT NULL AND decision_source IN ({source_slots}) "
            "ORDER BY decided_at DESC, row_key LIMIT 1",
            (row.parent_id, *_DIRECT_HUMAN_SOURCES),
        ).fetchone()
        if winner is not None and winner["row_key"] != row.row_key:
            IdentityPolicy.settle_siblings(
                conn,
                row.parent_id,
                winner["row_key"],
                winner["decided_at"],
            )

    def _project_artifact(self, row: ArtifactRow, *, conn: sqlite3.Connection) -> bool:
        columns = (
            "kind", "parent_id", "person_id", "candidate_key", "path",
            "content_fingerprint", "input_fingerprint", "status", "error", "payload_json",
        )
        current = conn.execute(
            f"SELECT {', '.join(columns)} FROM artifacts WHERE artifact_key=?",
            (row.artifact_key,),
        ).fetchone()
        values = asdict(row)
        if current and all(current[column] == values[column] for column in columns):
            return False
        conn.execute(UPSERTS["artifacts"], asdict(row))
        return True

    def project_rows(
        self,
        rows: tuple[
            OwnerContextRow
            | ParentRow
            | PersonRow
            | PersonIdentifiersProjection
            | PersonSourcesProjection
            | LinkRow
            | CandidatePeopleProjection
            | IdentityMachineProjection
            | ArtifactProjection
            | ArtifactReplacement
            | ArtifactRow
            | FactRow
            | SyntheticProfileRow
            | ResearchRow
            | GuidanceRow
            | JobRow,
            ...,
        ],
    ) -> int:
        """Atomically project a closed union of frozen domain row models."""
        artifact_keys = [
            artifact.artifact_key
            for row in rows
            if isinstance(row, ArtifactProjection)
            for artifact in (row.artifact, row.raw_artifact)
            if artifact is not None
        ]
        if len(artifact_keys) != len(set(artifact_keys)):
            raise StoreError("artifact projections contain duplicate keys")
        changed = 0
        identity_parents: set[str] = set()
        with self.transaction() as conn:
            for row in rows:
                simple_table = TABLE_BY_TYPE.get(type(row))
                child_table = _CHILD_TABLES.get(type(row))
                if simple_table in {
                    "owner_context", "parents", "people", "facts",
                    "synthetic_profiles", "research", "guidance", "jobs",
                }:
                    self._write(simple_table, row, conn)
                    continue
                if child_table:
                    key_column = _CHILD_KEYS[child_table]
                    self._replace_children(
                        child_table, getattr(row, key_column), row.rows, conn=conn,
                    )
                    continue
                match row:
                    case LinkRow():
                        self._project_candidate(row, conn=conn)
                        identity_parents.add(row.parent_id)
                    case IdentityMachineProjection():
                        owner = conn.execute(
                            "SELECT parent_id FROM links WHERE row_key=?", (row.row_key,),
                        ).fetchone()
                        if owner is None:
                            raise StoreError(f"unknown candidate: {row.row_key}")
                        if conn.execute(_IDENTITY_UPDATE, asdict(row)).rowcount != 1:
                            raise StoreError(f"unknown candidate: {row.row_key}")
                        identity_parents.add(owner["parent_id"])
                    case ArtifactProjection():
                        artifact = row.artifact
                        current = conn.execute(
                            "SELECT content_fingerprint, status FROM artifacts "
                            "WHERE artifact_key=?",
                            (artifact.artifact_key,),
                        ).fetchone()
                        content_changed = not current or tuple(current) != (
                            artifact.content_fingerprint,
                            artifact.status,
                        )
                        if content_changed and row.candidate is not None:
                            self._project_candidate(row.candidate, conn=conn)
                        if row.raw_artifact is not None:
                            changed += int(self._project_artifact(row.raw_artifact, conn=conn))
                        content_changed = self._project_artifact(artifact, conn=conn)
                        changed += int(content_changed)
                        if not content_changed:
                            continue
                        if row.candidate_people is not None:
                            self._replace_children(
                                "candidate_people",
                                row.candidate_people.row_key,
                                row.candidate_people.rows,
                                conn=conn,
                            )
                        for table, dependent in (
                            ("facts", row.fact),
                            ("research", row.research),
                            ("synthetic_profiles", row.synthetic_profile),
                        ):
                            if dependent is not None:
                                self._write(table, dependent, conn)
                    case ArtifactReplacement():
                        if row.person_id is not None and row.parent_id is not None:
                            raise StoreError("artifact replacement has two owners")
                        keys = {item.artifact_key for item in row.rows}
                        parent_owned = row.parent_id is not None
                        invalid = len(keys) != len(row.rows) or any(
                            item.kind != row.kind or item.candidate_key
                            or (
                                parent_owned
                                and (item.parent_id != row.parent_id or item.person_id is not None)
                            )
                            or (
                                not parent_owned
                                and (
                                    not item.person_id
                                    or (
                                        row.person_id is not None
                                        and item.person_id != row.person_id
                                    )
                                )
                            )
                            for item in row.rows
                        )
                        if invalid:
                            raise StoreError("artifact replacement has invalid keys or owners")
                        if parent_owned:
                            scope = "parent_id=? AND person_id IS NULL AND candidate_key IS NULL"
                            params = (row.parent_id,)
                        else:
                            scope = "person_id=?" if row.person_id is not None else "person_id IS NOT NULL"
                            params = (row.person_id,) if row.person_id is not None else ()
                        existing = {
                            item["artifact_key"]
                            for item in conn.execute(
                                f"SELECT artifact_key FROM artifacts WHERE kind=? AND {scope}",
                                (row.kind, *params),
                            )
                        }
                        stale = existing - keys
                        if stale:
                            conn.executemany(
                                "DELETE FROM artifacts WHERE artifact_key=?",
                                [(key,) for key in sorted(stale)],
                            )
                            changed += len(stale)
                        changed += sum(self._project_artifact(item, conn=conn) for item in row.rows)
                    case ArtifactRow():
                        changed += int(self._project_artifact(row, conn=conn))
                    case _:
                        raise TypeError(f"unsupported projection row: {type(row).__name__}")
            IdentityPolicy.clear_machine_winner_conflicts(conn, identity_parents)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise StoreError(f"projection violates foreign keys: {violations[0]}")
        return changed

    def start_job(self, row: JobRow) -> bool:
        """Atomically start one named job unless that job is already running."""
        if row.status != JobStatus.RUNNING.value:
            raise StoreError("started job must have running status")
        with self.transaction() as conn:
            changed = conn.execute(
                f"{UPSERTS['jobs']} WHERE jobs.status != 'running'", asdict(row)
            ).rowcount
        return changed == 1

    def replace_merge_verdicts(self, rows: tuple[MergeVerdictRow, ...]) -> None:
        """Upsert the current merge survey without evicting unrelated paid cache."""
        keys = [(row.person_a, row.person_b) for row in rows]
        if any(left >= right for left, right in keys):
            raise StoreError("merge verdict people must be ordered and distinct")
        if len(keys) != len(set(keys)):
            raise StoreError("merge verdict survey contains duplicate pairs")
        with self.transaction() as conn:
            conn.executemany(
                UPSERTS["merge_verdicts"], [asdict(row) for row in rows],
            )

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

    def decide_identity(
        self, candidate_key: str, action: str | None, *, approved: str = "yes",
        replacement_url: str | None = None,
        replacement_public_identifier: str | None = None,
        source: str = ReviewSource.REVIEW.value, note: str | None = None,
        decided_at: str | None = None,
    ) -> list[str]:
        """Settle one candidate family, or reset its human identity decisions."""
        if action is None:
            with self.transaction() as conn:
                row = conn.execute(
                    "SELECT parent_id FROM links WHERE row_key=?", (candidate_key,),
                ).fetchone()
                if row is None:
                    raise StoreError(f"unknown candidate: {candidate_key}")
                reset = [item["row_key"] for item in conn.execute(
                    "SELECT row_key FROM links WHERE parent_id=? "
                    f"AND decision_source IN ({_HUMAN_SOURCE_SLOTS})",
                    (row["parent_id"], *_HUMAN_SOURCES),
                )]
                conn.execute(
                    f"{_CLEAR_IDENTITY} AND parent_id=?",
                    (*_HUMAN_SOURCES, row["parent_id"]),
                )
                IdentityPolicy.clear_machine_winner_conflicts(conn, (row["parent_id"],))
            return reset
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
            clicked = conn.execute(
                "SELECT * FROM links WHERE row_key=?", (candidate_key,),
            ).fetchone()
            if clicked is None:
                raise StoreError(f"unknown candidate: {candidate_key}")
            conn.execute(
                "UPDATE links SET decision_action=?, decision_approved=?, decision_source=?, "
                "decision_note=?, decided_at=?, replacement_url=?, "
                "replacement_public_identifier=? WHERE row_key=?",
                (action, approved, source, note, at, replacement_url,
                 replacement_public_identifier, candidate_key),
            )
            siblings = IdentityPolicy.settle_siblings(
                conn,
                clicked["parent_id"],
                candidate_key,
                at,
            )
        return [candidate_key, *siblings]

    def reset_review(self, *, apply: bool = True) -> ResetReviewCounts:
        """Clear human review state atomically while preserving every machine artifact."""
        with self.transaction() as conn:
            if not apply:
                worth = conn.execute(
                    f"SELECT count(*) FROM parents WHERE {_HAS_HUMAN_WORTH}"
                ).fetchone()[0]
                identity = conn.execute(
                    f"SELECT count(*) FROM links WHERE decision_source IN ({_HUMAN_SOURCE_SLOTS})",
                    _HUMAN_SOURCES,
                ).fetchone()[0]
                return ResetReviewCounts(worth, identity)
            worth = conn.execute(
                "UPDATE parents SET human_worth=NULL, human_worth_note=NULL, "
                f"human_worth_source=NULL, human_worth_at=NULL WHERE {_HAS_HUMAN_WORTH}"
            ).rowcount
            identity = conn.execute(_CLEAR_IDENTITY, _HUMAN_SOURCES).rowcount
        return ResetReviewCounts(worth, identity)


def open_existing_db(db_path: str | Path) -> Db:
    """Open the current canonical store without ever creating a missing one."""
    path = Path(db_path)
    if not path.is_file():
        raise SystemExit(
            f"Deep Context database is missing: {path}; "
            "run the explicit legacy import first"
        )
    try:
        return Db(path)
    except StoreError as exc:
        raise SystemExit(
            f"Deep Context database is unsupported: {path}: {exc}"
        ) from exc


class DbMaintenance:
    """Canonical SQLite maintenance for destructive setup workflows."""

    def __init__(self, db: Db):
        self.db = db

    def backup_to(self, destination: Path) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(f"file:{self.db.db_path}?mode=ro", uri=True)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def reset_scrubbed_artifacts(
        self, scrubbed_paths: tuple[Path, ...],
    ) -> DerivedResetCounts:
        roots = tuple(path.resolve() for path in scrubbed_paths)
        with self.db.transaction() as conn:
            artifacts = [
                row for row in conn.execute("SELECT artifact_key, path FROM artifacts")
                if any(Path(row["path"]).resolve().is_relative_to(root) for root in roots)
            ]
            keys = [row["artifact_key"] for row in artifacts]
            facts = research = 0
            if keys:
                placeholders = ",".join("?" for _ in keys)
                facts = conn.execute(
                    f"SELECT COUNT(*) FROM facts WHERE artifact_key IN ({placeholders})",
                    keys,
                ).fetchone()[0]
                research = conn.execute(
                    f"SELECT COUNT(*) FROM research WHERE artifact_key IN ({placeholders})",
                    keys,
                ).fetchone()[0]
                conn.execute(
                    f"DELETE FROM research WHERE artifact_key IN ({placeholders})", keys,
                )
                conn.execute(
                    f"DELETE FROM artifacts WHERE artifact_key IN ({placeholders})", keys,
                )
            jobs = conn.execute("DELETE FROM jobs").rowcount
            guidance = conn.execute("DELETE FROM guidance").rowcount
        return DerivedResetCounts(len(keys), facts, research, jobs, guidance)
