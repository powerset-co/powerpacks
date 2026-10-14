"""Where chat searches by default: one of the user's Powerset sets, or this Mac's local index.

`.env` holds the choice the way `$search` already reads it: POWERPACKS_DEFAULT_SET_ID names the
set, and POWERPACKS_LOCAL_SEARCH_DB, when set, makes the local index the default. The sets are the
signed-in user's, largest first, straight from Postgres (DATABASE_URL); with no sign-in there are
none. With no set of theirs in `.env` (none yet, or one an import brought from another account),
searches default to their largest set, written to `.env` so search and picker agree.

Changelog:
  2026-10-14: no set of yours in .env defaults to your largest set; counts come with the sets.
  2026-10-14: created; the chat's search picker reads and writes it through /api/search/scope.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import _read_env_file, write_env

SEARCH_LIB = Path(__file__).resolve().parents[2] / "search" / "primitives" / "lib"
if str(SEARCH_LIB) not in sys.path:
    sys.path.insert(0, str(SEARCH_LIB))

import postgres_client  # noqa: E402

LOCAL = "local"
LOCAL_INDEX = Path(".powerpacks/search-index/local-search.duckdb")
SET_KEY = "POWERPACKS_DEFAULT_SET_ID"
LOCAL_KEY = "POWERPACKS_LOCAL_SEARCH_DB"


def read(root: Path) -> dict[str, Any]:
    """The user's sets, whether a local index exists, and which one searches use now."""
    env = _read_env_file(root / ".env")
    subject = postgres_client.credentials_subject()
    sets = postgres_client.fetch_member_sets(subject, env["DATABASE_URL"]) if subject and env.get("DATABASE_URL") else []
    if env.get(LOCAL_KEY):
        selected = LOCAL
    elif env.get(SET_KEY) in {row["set_id"] for row in sets}:
        selected = env[SET_KEY]
    elif sets:
        return save(root, sets[0]["set_id"])
    else:
        selected = ""
    return {"sets": sets, "local": (root / LOCAL_INDEX).is_file(), "selected": selected}


def save(root: Path, selected: str) -> dict[str, Any]:
    """Search the local index, or a set (which also stops the local index being the default)."""
    if selected == LOCAL:
        write_env(root / ".env", {LOCAL_KEY: str(root / LOCAL_INDEX)})
    else:
        write_env(root / ".env", {SET_KEY: selected, LOCAL_KEY: None})
    return read(root)
