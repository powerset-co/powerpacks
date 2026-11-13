"""Where chat can search: the user's Powerset sets, and this Mac's local index when it has one.

The chat page shows them in its Search-in picker; the pick is pinned to the chat and reaches every
command the agent runs through Codex (desktop codex.rs), never through `.env`. The sets are the
signed-in user's, largest first, from the Powerset API (GET /v2/sets); with no sign-in there are
none.

Changelog:
  2026-11-13: ANY failure listing the sets still offers the local index; an expired sign-in
      (SystemExit, not an Exception) used to end the request and take Local out of the picker.
  2026-11-12: a failed sets call leaves the sets empty and still offers the local index; the picker
      used to vanish with the whole route a 502 (Jake, during the Supabase connection incident).
  2026-11-12: read() lists the sets through the Powerset API, not Postgres, like every other set call.
  2026-11-11: set_detail(), invite(), create_set(), delete_set(): the Sets page's reads and writes through
      the Powerset API.
  2026-10-29: no choice lives here any more: the chat pins its scope through Codex.
  2026-10-27: no choice in .env defaults to the local index, not the largest set: a new install
      searches its own network first.
  2026-10-14: no set of yours in .env defaults to your largest set; counts come with the sets.
  2026-10-14: created; the chat's search picker reads and writes it through /api/search/scope.
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys

SEARCH_LIB = Path(__file__).resolve().parents[2] / "search" / "primitives" / "lib"
if str(SEARCH_LIB) not in sys.path:
    sys.path.insert(0, str(SEARCH_LIB))

import postgres_client  # noqa: E402

LOCAL_INDEX = Path(".powerpacks/search-index/local-search.duckdb")


def read(root: Path) -> dict[str, Any]:
    """The user's sets, and the local index's path when this Mac has one (else null).

    The two are independent: when the API cannot be reached the sets are empty and the local index
    is still offered, so the picker shows it and a search can start on this Mac."""
    sets: list[dict[str, Any]] = []
    try:  # anything at all (an expired sign-in raises SystemExit): no sets, the local index stays
        if postgres_client.credentials_subject():
            sets = _member_sets(root)
    except BaseException:
        logging.getLogger(__name__).exception("Listing the sets failed; offering the local index")
    index = root / LOCAL_INDEX
    return {"sets": sets, "local_db": str(index) if index.is_file() else None}


def _member_sets(root: Path) -> list[dict[str, Any]]:
    """The sets you created or belong to, largest first. An app owner's list also carries every
    other set, with role "admin"; those are not theirs, so they are left out."""
    rows = [row for row in _api(root, "/v2/sets") if row.get("role") != "admin"]
    sets = [{"set_id": row["id"], "name": row["name"], "is_personal": bool(row.get("is_personal")),
             "owned": row.get("role") == "owner", "person_count": int(row.get("person_count") or 0)}
            for row in rows]
    return sorted(sets, key=lambda row: (-row["person_count"], row["name"].lower()))


def _api(root: Path, path: str, body: dict[str, Any] | None = None, method: str | None = None) -> Any:
    """One call to the Powerset API as the signed-in user: GET, or POST with a JSON body; {} for a
    reply with no body (a DELETE's 204)."""
    env_file = root / ".env"
    request = urllib.request.Request(
        pull_runtime_keys.api_base(env_file) + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {pull_runtime_keys.bearer_token(env_file)}",
                 "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"{}")


def set_detail(root: Path, set_id: str) -> dict[str, Any]:
    """One set as the Powerset API shows it (GET /v2/sets/{id}): its counts, your role, and its
    members with their role and how many people each brings."""
    return _api(root, "/v2/sets/" + urllib.parse.quote(set_id))


def invite(root: Path, set_id: str) -> dict[str, Any]:
    """A new invite link to a set you own (POST /v2/sets/{id}/invites), with the API's defaults:
    one use, 72 hours."""
    return _api(root, "/v2/sets/" + urllib.parse.quote(set_id) + "/invites", {})


def create_set(root: Path, name: str) -> dict[str, Any]:
    """A new set you own (POST /v2/sets), with you as its one member."""
    return _api(root, "/v2/sets", {"name": name})


def delete_set(root: Path, set_id: str) -> dict[str, Any]:
    """Delete a set you own (DELETE /v2/sets/{id}; the API keeps the row, inactive)."""
    return _api(root, "/v2/sets/" + urllib.parse.quote(set_id), method="DELETE")
