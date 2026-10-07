"""Open the v2 store. Creates it empty; refuses any other schema version. No migrations.

Created: 2026-10-06
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db.schema import DDL, SCHEMA_VERSION

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
        return conn
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    version = int(row["value"]) if row is not None else 0
    if version != SCHEMA_VERSION:
        conn.close()
        raise StoreError(f"{path} has schema version {version}, this code is {SCHEMA_VERSION}; move the file aside")
    return conn
