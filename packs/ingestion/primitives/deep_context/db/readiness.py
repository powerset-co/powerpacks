"""Read whether canonical SQLite has parents before project dependencies are installed."""

from contextlib import closing
from pathlib import Path
import sqlite3

CANONICAL_DB = Path(".powerpacks/deep-context/deep-context.sqlite")


def has_parents(path: Path) -> bool:
    """A missing or empty store has no review; an unsupported store fails explicitly."""
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
            return connection.execute("SELECT 1 FROM parents LIMIT 1").fetchone() is not None
    except sqlite3.Error as error:
        raise ValueError(f"Cannot read Deep Context database: {error}") from error
