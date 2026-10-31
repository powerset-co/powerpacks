"""Open the v2 store. Creates it empty; upgrades an older store in place; refuses a newer one.

Created: 2026-10-06

Changelog:
  2026-10-07: schema 2 -> 3 in place (the share stage's tables and views; nothing existing changes),
    so a 3.18 install keeps its paid store when it updates.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db.schema import DDL, DELETED_DDL, FAMILIES_DDL, NAME_COLUMNS_DDL, RELATIONSHIP_KIND_COLUMN_DDL, REVIEW_QUEUE_DDL, SCHEMA_VERSION, SETS_DDL, PEOPLE_TABLES_DDL, TAGS_VIEW_DDL

STORE_RELATIVE_PATH = Path("deep-context") / "deep-context-v2.sqlite"


class StoreError(RuntimeError):
    pass


def store_path(data_root: Path) -> Path:
    """`data_root` is the `.powerpacks` directory of one checkout."""
    return data_root / STORE_RELATIVE_PATH


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def open_store(path: Path, *, shared: bool = False) -> sqlite3.Connection:
    """The store, created with the schema when absent. `shared` lets a threaded server use the one
    connection from any thread; the server serializes its requests on it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=not shared)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
    if has_meta is None:
        conn.executescript(DDL)
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
        ensure_store_id(conn)
        return conn
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    version = int(row["value"]) if row is not None else 0
    if version == 2:
        _migrate_2_to_3(conn)
        version = 3
    if version == 3:
        _migrate_3_to_4(conn)
        version = 4
    if version == 4:
        _migrate_4_to_5(conn)
        version = 5
    if version == 5:
        _migrate_5_to_6(conn)
        version = 6
    if version == 6:
        _migrate_6_to_7(conn)
        version = 7
    if version == 7:
        _migrate_7_to_8(conn)
        version = 8
    if version == 8:
        _migrate_8_to_9(conn)
        version = 9
    if version == 9:
        _migrate_9_to_10(conn)
        version = 10
    if version != SCHEMA_VERSION:
        conn.close()
        raise StoreError(f"{path} has schema version {version}, this code is {SCHEMA_VERSION}; move the file aside")
    ensure_store_id(conn)
    return conn


def ensure_store_id(conn: sqlite3.Connection) -> str:
    """The store's own id: it survives moving or copying the `.powerpacks` directory, so anything
    derived from this store (the memory directory) keys on it rather than on a file path."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
    if row is not None:
        return str(row["value"])
    store_id = uuid.uuid4().hex
    with conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('store_id', ?)", (store_id,))
    return store_id


def _migrate_2_to_3(conn: sqlite3.Connection) -> None:
    """Add person tags and labels to a schema-2 store."""
    with conn:
        conn.executescript(PEOPLE_TABLES_DDL + TAGS_VIEW_DDL)
        conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")


def _migrate_3_to_4(conn: sqlite3.Connection) -> None:
    """Schema 4 is schema 3 plus the review queue and the sets table; no row changes."""
    with conn:
        conn.executescript(REVIEW_QUEUE_DDL + SETS_DDL)
        conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")


def _migrate_4_to_5(conn: sqlite3.Connection) -> None:
    """Remove derived sharing state and the retired human tags; preserve all other tags."""
    with conn:
        conn.execute("DROP VIEW IF EXISTS current_share")
        conn.execute("DROP TABLE IF EXISTS share")
        for row in conn.execute("SELECT candidate_id, tags FROM person_tags").fetchall():
            tags = [tag for tag in row["tags"].split("|") if tag not in {"share", "private"}]
            conn.execute("UPDATE person_tags SET tags = ? WHERE candidate_id = ?",
                         ("|".join(tags), row["candidate_id"]))
        conn.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")


def _migrate_5_to_6(conn: sqlite3.Connection) -> None:
    """Add the human's relationship choice and the deleted families, without changing saved labels or tags."""
    # executescript commits first, so the table and trigger go in before the transaction below.
    conn.executescript(DELETED_DDL)
    with conn:
        # A schema-2 store gets this column when its person_tags table is created above.
        if "relationship_kind" not in {row["name"] for row in conn.execute("PRAGMA table_info(person_tags)")}:
            conn.execute(f"ALTER TABLE person_tags ADD COLUMN {RELATIONSHIP_KIND_COLUMN_DDL}")
        conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")


def _migrate_6_to_7(conn: sqlite3.Connection) -> None:
    """Keep every historical score and decision; new probabilities start NULL."""
    table = DDL[DDL.index("CREATE TABLE candidate_linkedins ("):DDL.index("CREATE TABLE research (")]
    views = DDL[DDL.index("CREATE VIEW current_linkedins AS"):DDL.index("CREATE VIEW current_tags AS")]
    with conn:
        conn.execute("BEGIN")
        conn.execute("DROP VIEW current_profile")
        conn.execute("DROP VIEW current_linkedins")
        conn.execute("ALTER TABLE candidate_linkedins RENAME TO old_candidate_linkedins")
        conn.execute("DROP INDEX candidate_linkedins_by_member")
        conn.execute("DROP INDEX candidate_linkedins_by_url")
        statements = "\n".join(line.split("--", 1)[0] for line in table.splitlines())
        for statement in statements.split(";"):
            if statement.strip():
                conn.execute(statement)
        columns = "seq, candidate_id, linkedin_url, member_id, origin, verdict, decided_by, judgment_fingerprint, confidence, reason, created_at"
        conn.execute(f"INSERT INTO candidate_linkedins ({columns}) SELECT {columns} FROM old_candidate_linkedins")
        conn.execute("DROP TABLE old_candidate_linkedins")
        conn.execute(views.strip().removesuffix(";"))
        profile = DDL[DDL.index("CREATE VIEW current_profile AS"):]
        conn.execute(profile.strip().removesuffix(";"))
        conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")


def _migrate_7_to_8(conn: sqlite3.Connection) -> None:
    """Add the families table, empty: the page server fills it from the store on its first People request
    (realize needs the data root for the profile cache, which the store opener does not have)."""
    conn.executescript(FAMILIES_DDL)
    with conn:
        conn.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")


def _migrate_8_to_9(conn: sqlite3.Connection) -> None:
    """Add the human's name for a person; nothing saved changes."""
    with conn:
        # A schema-2 store gets these columns when its person_tags table is created above.
        present = {row["name"] for row in conn.execute("PRAGMA table_info(person_tags)")}
        for column in NAME_COLUMNS_DDL:
            if column.split()[0] not in present:
                conn.execute(f"ALTER TABLE person_tags ADD COLUMN {column}")
        conn.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")


def _migrate_9_to_10(conn: sqlite3.Connection) -> None:
    """Allow LinkedIn sources without changing any candidate or decision history."""
    source = DDL[DDL.index("CREATE TABLE candidate_sources ("):DDL.index("CREATE TABLE connections (")]
    profile = DDL[DDL.index("CREATE VIEW current_profile AS"):]
    with conn:
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE candidate_sources RENAME TO old_candidate_sources")
        conn.execute("DROP INDEX candidate_sources_by_source")
        for statement in source.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.execute("INSERT INTO candidate_sources SELECT * FROM old_candidate_sources")
        conn.execute("DROP TABLE old_candidate_sources")
        conn.execute("DROP VIEW current_profile")
        conn.execute(profile.strip().removesuffix(";"))
        conn.execute("UPDATE meta SET value = '10' WHERE key = 'schema_version'")
