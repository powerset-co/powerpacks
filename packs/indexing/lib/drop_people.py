"""Take people out of the local search index without a rebuild.

The People page's Delete hides a family in the store; the index is rebuilt from the store only on the next
refresh, and the cloud copy follows the index on the next sync. `drop_people` removes the family's LinkedIn
person from every person table of the index right away, on a copy that then replaces it (as the download does).

Changelog:
  2026-11-24: created; Delete on the People page reaches search at once.
"""
from __future__ import annotations

import shutil
from contextlib import closing
from pathlib import Path

import duckdb

INDEX_RELATIVE = Path("search-index/local-search.duckdb")  # under the data root (.powerpacks)


def drop_people(db: Path, slugs: set[str]) -> int:
    """Remove the people with these LinkedIn slugs from every table with a person_id (views follow them). Returns how many
    profiles went; an index that holds none of them is left untouched."""
    if not slugs or not db.exists():
        return 0
    with closing(duckdb.connect(str(db), read_only=True)) as conn:
        ids = [row[0] for row in conn.execute(
            "SELECT CAST(person_id AS VARCHAR) FROM local_person_profiles WHERE lower(public_identifier) IN (SELECT unnest(?))",
            [sorted(slugs)]).fetchall()]
    if not ids:
        return 0
    working = db.with_name(db.name + ".tmp")
    shutil.copyfile(db, working)
    with closing(duckdb.connect(str(working))) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.columns JOIN information_schema.tables USING (table_schema, table_name) "
            "WHERE column_name = 'person_id' AND table_type = 'BASE TABLE' ORDER BY table_name").fetchall()]
        for table in tables:
            conn.execute(f'DELETE FROM "{table}" WHERE CAST(person_id AS VARCHAR) IN (SELECT unnest(?))', [ids])
    working.replace(db)
    return len(ids)
