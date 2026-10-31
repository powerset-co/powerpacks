"""Where chat can search: the user's Powerset sets, and this Mac's local index when it has one.

The chat page shows them in its Search-in picker; the pick is pinned to the chat and reaches every
command the agent runs through Codex (desktop codex.rs), never through `.env`. The sets are the
signed-in user's, largest first, straight from Postgres (DATABASE_URL); with no sign-in there are
none.

Changelog:
  2026-10-29: no choice lives here any more: the chat pins its scope through Codex.
  2026-10-27: no choice in .env defaults to the local index, not the largest set: a new install
      searches its own network first.
  2026-10-14: no set of yours in .env defaults to your largest set; counts come with the sets.
  2026-10-14: created; the chat's search picker reads and writes it through /api/search/scope.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import _read_env_file

SEARCH_LIB = Path(__file__).resolve().parents[2] / "search" / "primitives" / "lib"
if str(SEARCH_LIB) not in sys.path:
    sys.path.insert(0, str(SEARCH_LIB))

import postgres_client  # noqa: E402

LOCAL_INDEX = Path(".powerpacks/search-index/local-search.duckdb")


def read(root: Path) -> dict[str, Any]:
    """The user's sets, and the local index's path when this Mac has one (else null)."""
    env = _read_env_file(root / ".env")
    subject = postgres_client.credentials_subject()
    sets = postgres_client.fetch_member_sets(subject, env["DATABASE_URL"]) if subject and env.get("DATABASE_URL") else []
    index = root / LOCAL_INDEX
    return {"sets": sets, "local_db": str(index) if index.is_file() else None}
