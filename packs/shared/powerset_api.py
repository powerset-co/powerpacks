"""One call to the Powerset API (network-search-api) as the signed-in user.

Everything cloud in powerpacks goes through here: no install reads Postgres or TurboPuffer.

Changelog:
  2026-11-16: created; the cloud's people, sets, attribution and companies come only from the API.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys


def call(env_file: Path | None, path: str, body: Any = None, *, method: str | None = None, timeout: int = 60) -> Any:
    """GET, or POST with a JSON body; {} for a reply with no body (a DELETE's 204). HTTP errors raise."""
    request = urllib.request.Request(
        pull_runtime_keys.api_base(env_file) + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {pull_runtime_keys.bearer_token(env_file)}",
                 "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def signed_in_subject(credentials_path: Path | None = None) -> str | None:
    """The Auth0 subject of the saved sign-in (POWERPACKS_CREDENTIALS_PATH, else ~/.powerpacks), or None.
    Reads the local token only; no network."""
    path = credentials_path or Path(os.environ.get("POWERPACKS_CREDENTIALS_PATH") or Path.home() / ".powerpacks" / "credentials.json")
    try:
        token = str(json.loads(path.read_text()).get("access_token") or "")
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("sub")
    except Exception:
        return None


def personal_set_id(env_file: Path | None) -> str | None:
    """The signed-in user's personal set (GET /v2/sets): the scope of a search that named no set."""
    for row in call(env_file, "/v2/sets"):
        if row.get("is_personal") and row.get("role") == "owner":
            return str(row["id"])
    return None


def person_attribution(env_file: Path | None, set_id: str, person_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Set-scoped source badges and members per person (POST /v2/sets/{id}/person-attribution)."""
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(person_ids), 500):
        rows = call(env_file, f"/v2/sets/{quote(set_id)}/person-attribution", {"person_ids": person_ids[start:start + 500]})
        out.update({str(row["person_id"]): row for row in rows})
    return out
