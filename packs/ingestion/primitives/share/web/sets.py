"""The sets the owner belongs to, for the People page: read from the cloud, kept in the store, created
and deleted through the cloud's `/v2/sets`.

A set is who can see the owner's shared network: every member of every set the owner is in. The owner
shares one network (the share list); sets are joined, created or deleted here and in the cloud app.

Created: 2026-10-08
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context_v2.db import queries_share
from packs.ingestion.primitives.deep_context_v2.db.queries_share import SetRow
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso
from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import api_base, bearer_token

SETS_PATH = "/v2/sets"
TIMEOUT_SECONDS = 30


class NeedsSignIn(Exception):
    """Powerset has no usable sign-in on this machine."""


class CloudError(Exception):
    """The cloud refused or could not be reached; the words are what the page shows."""


@dataclass(frozen=True)
class Member:
    name: str
    email: str
    role: str


@dataclass(frozen=True)
class SetView:
    set_id: str
    name: str
    role: str
    is_personal: bool
    member_count: int
    person_count: int
    members: tuple[Member, ...]
    refreshed_at: str


def _members(payload: list[dict[str, Any]]) -> list[Member]:
    found: list[Member] = []
    for row in payload:
        found.append(Member(str(row.get("name") or ""), str(row.get("email") or ""), str(row.get("role") or "")))
    return found


class Sets:
    def __init__(self, conn: sqlite3.Connection, env_file: Path) -> None:
        self.conn = conn
        self.env_file = env_file

    # ---- the cloud

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            token: str = bearer_token(self.env_file)
        except SystemExit as error:
            raise NeedsSignIn(str(error)) from error
        request = urllib.request.Request(
            api_base(self.env_file) + path, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw: bytes = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise NeedsSignIn("Powerset rejected the sign-in") from error
            raise CloudError(f"Powerset answered {error.code} for {path}") from error
        except urllib.error.URLError as error:
            raise CloudError(f"Couldn't reach Powerset: {error.reason}") from error

    def refresh(self) -> list[SetView]:
        """The cloud's sets, with members, written over the kept ones."""
        now: str = now_iso()
        rows: list[SetRow] = []
        for item in self._call("GET", SETS_PATH):
            detail: dict[str, Any] = self._call("GET", f"{SETS_PATH}/{urllib.parse.quote(str(item['id']), safe='')}")
            members: list[Member] = _members(detail.get("members") or [])
            rows.append((str(item["id"]), str(item["name"]), str(item.get("role") or ""), int(bool(item.get("is_personal"))),
                         int(item.get("member_count") or len(members)), int(item.get("person_count") or 0),
                         json.dumps([member.__dict__ for member in members], ensure_ascii=False), now))
        queries_share.replace_sets(self.conn, rows)
        return self.kept()

    def create(self, name: str) -> list[SetView]:
        self._call("POST", SETS_PATH, {"name": name, "description": None})
        return self.refresh()

    def delete(self, set_id: str) -> list[SetView]:
        self._call("DELETE", f"{SETS_PATH}/{urllib.parse.quote(set_id, safe='')}")
        return self.refresh()

    # ---- the store

    def kept(self) -> list[SetView]:
        found: list[SetView] = []
        for row in queries_share.sets(self.conn):
            members: list[Member] = _members(json.loads(row["members_json"]))
            found.append(SetView(row["set_id"], row["name"], row["role"], bool(row["is_personal"]), row["member_count"],
                                 row["person_count"], tuple(members), row["refreshed_at"]))
        return found


def payload(sets: list[SetView], shared: int, default_set_id: str) -> dict[str, Any]:
    """The page's answer: the sets, how many people the owner shares, and the set searches default to."""
    items: list[dict[str, Any]] = []
    for view in sets:
        items.append({"set_id": view.set_id, "name": view.name, "role": view.role, "is_personal": view.is_personal,
                      "member_count": view.member_count, "person_count": view.person_count,
                      "members": [member.__dict__ for member in view.members], "refreshed_at": view.refreshed_at})
    return {"sets": items, "shared": shared, "default_set_id": default_set_id}
