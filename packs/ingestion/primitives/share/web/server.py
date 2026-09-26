"""The People page's routes: the page, its assets, the people payload, one write.

Flow: `ShareRoutes` mounts under `/people` and `/api/people/` in the review
server (`bin/deep-context review people`); `make_handler` serves the same
routes alone for tests. GET `/api/people/rows` ->
`SharePeople.load()` as one columnar payload, one row per parent; POST
`/api/people/tags` carries the tags each selected parent should hold (absolute
sets, so undo re-posts the previous sets), writes the tag rows for every person
under those parents and re-decides their share rows through
`labels.share_decision` from `person_labels`, in one transaction.

Changelog:
  2026-09-26: created.
  2026-09-26: serves the React build from web/dist; legacy page and vendor/ removed.
"""

from __future__ import annotations

import gzip
import json
import sys
import urllib.parse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db import share_views
from packs.ingestion.primitives.deep_context.db.models import PersonTagRow, ShareDecisionRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.shared.common import DEFAULT_PEOPLE_CSV
from packs.ingestion.primitives.share.labels import label_row_from_export, share_decision
from packs.ingestion.primitives.share.models import HumanTags
from packs.ingestion.primitives.share.store import TAG_VOCABULARY, TagStore, join_tags
from packs.ingestion.primitives.share.web import PEOPLE_CSS, PEOPLE_HTML, PEOPLE_JS
from packs.ingestion.primitives.share.web.model import SharePeople, people_payload

PAGE_PATH = "/people"
API_PREFIX = "/api/people/"
ASSET_PREFIX = "/people/assets/"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_TAGS_REQUEST_BYTES = 4 * 1024 * 1024
GZIP_MIN_BYTES = 8 * 1024

# The page is the React build (`web/dist`); its people.css carries the tokens.
ASSETS = {
    "people.css": (PEOPLE_CSS, "text/css; charset=utf-8"),
    "people.js": (PEOPLE_JS, "text/javascript; charset=utf-8"),
}

TagChanges = dict[str, frozenset[str]]


def parse_tag_request(body: bytes, known_ids: set[str]) -> TagChanges:
    """The one write the UI makes: the tags each selected parent should hold."""
    try:
        request = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    people = request.get("people") if isinstance(request, dict) else None
    if not isinstance(people, list) or not people:
        raise ValueError("people must be a non-empty list of {parent_id, tags}")
    changes: TagChanges = {}
    for entry in people:
        if not isinstance(entry, dict) or not isinstance(entry.get("parent_id"), str) \
                or not isinstance(entry.get("tags"), list) \
                or not all(isinstance(tag, str) for tag in entry["tags"]):
            raise ValueError("each entry needs a parent_id and a list of tags")
        bad = sorted(set(entry["tags"]) - TAG_VOCABULARY)
        if bad:
            raise ValueError(f"unknown tags: {', '.join(bad)}")
        changes[entry["parent_id"]] = frozenset(entry["tags"])
    unknown = sorted(set(changes) - known_ids)
    if unknown:
        raise ValueError(f"{len(unknown)} person ids are not on the share list")
    return changes


def decide_tags(db: Db, people: SharePeople, changes: TagChanges) -> dict[str, tuple[ShareDecisionRow, ...]]:
    """Write the tags and the share rows they re-decide, together, for every
    person under each parent. Returns the re-decided rows by parent.

    The tags land on the roster ids. A person whose only tags were inherited
    from a merged-away id keeps that note, and from now on the roster row is
    the one the node reads first (`share_list` resolves the survivor first).
    """
    held = TagStore(db).load()
    labels = {row.person_id: row for row in share_views.person_labels(db)}
    families = people.families()
    updated_at = now_iso()
    tag_rows: list[PersonTagRow] = []
    decided: dict[str, tuple[ShareDecisionRow, ...]] = {}
    for parent_id, tags in changes.items():
        share_rows = []
        for member in families[parent_id]:
            prior = held.get(member.person_id) or next(
                (held[old] for old in member.superseded_person_ids if old in held), None)
            human = HumanTags(person_id=member.person_id, tags=tags, note=prior.note if prior else None,
                              updated_at=updated_at)
            tag_rows.append(PersonTagRow(person_id=member.person_id, tags=join_tags(tags), note=human.note,
                                         updated_at=updated_at))
            share_rows.append(share_decision(label_row_from_export(labels[member.person_id]), human,
                                             updated_at=updated_at))
        decided[parent_id] = tuple(share_rows)
    db.decide_share(tuple(tag_rows), tuple(row for rows in decided.values() for row in rows))
    return decided


class ShareRoutes:
    """The People page's GET and POST routes, mountable in any stdlib handler."""

    def __init__(self, db: Db, people: SharePeople, load: Callable[[], tuple]) -> None:
        self.db = db
        self.people = people
        self.load = load

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == PAGE_PATH:
            self._send(handler, PEOPLE_HTML.read_bytes())
        elif parsed.path.startswith(ASSET_PREFIX) and parsed.path[len(ASSET_PREFIX):] in ASSETS:
            path, kind = ASSETS[parsed.path[len(ASSET_PREFIX):]]
            self._send(handler, path.read_bytes(), kind, cache="no-cache")
        elif parsed.path == f"{API_PREFIX}rows":
            self._send_json(handler, people_payload(self.load()))
        elif parsed.path == f"{API_PREFIX}person":
            detail = self.people.detail((query.get("id") or [""])[0])
            if detail is None:
                self._send_json(handler, {"error": "person not found"}, status=HTTPStatus.NOT_FOUND)
            else:
                self._send_json(handler, asdict(detail))
        elif parsed.path == f"{API_PREFIX}avatar":
            url = self.people.avatar_url((query.get("id") or [""])[0])
            if not url:
                self._send(handler, b"", "text/plain", status=HTTPStatus.NOT_FOUND)
                return True
            handler.send_response(HTTPStatus.FOUND)
            handler.send_header("Location", url)
            handler.send_header("Referrer-Policy", "no-referrer")
            handler.end_headers()
        else:
            return False
        return True

    def post(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if parsed.path != f"{API_PREFIX}tags":
            return False
        origin = (handler.headers.get("Origin") or "").strip()
        if origin and (urllib.parse.urlparse(origin).hostname or "").lower() not in LOCAL_HOSTS:
            self._send(handler, b"cross-origin request rejected", "text/plain", status=HTTPStatus.FORBIDDEN)
            return True
        length = int(handler.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_TAGS_REQUEST_BYTES:
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if length > 0 else HTTPStatus.BAD_REQUEST
            self._send_json(handler, {"error": "request body must be 1 byte to 4 MiB"}, status=status)
            return True
        known = {row.parent_id for row in self.load()}
        try:
            changes = parse_tag_request(handler.rfile.read(length), known)
        except ValueError as exc:
            self._send_json(handler, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return True
        decided = decide_tags(self.db, self.people, changes)
        self._send_json(handler, {"rows": [
            {"parent_id": parent_id, "share": rows[0].share, "reason": rows[0].reason,
             "share_source": rows[0].source, "tags": sorted(changes[parent_id])}
            for parent_id, rows in decided.items()]})
        return True

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, body: bytes, content_type: str = "text/html; charset=utf-8",
              status: int = HTTPStatus.OK, *, cache: str = "no-store") -> None:
        encoding = ""
        if len(body) >= GZIP_MIN_BYTES and "gzip" in (handler.headers.get("Accept-Encoding") or ""):
            body, encoding = gzip.compress(body, 6), "gzip"
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", cache)
        handler.send_header("X-Content-Type-Options", "nosniff")
        if encoding:
            handler.send_header("Content-Encoding", encoding)
        handler.end_headers()
        handler.wfile.write(body)

    def _send_json(self, handler: BaseHTTPRequestHandler, payload: dict[str, Any],
                   status: int = HTTPStatus.OK) -> None:
        self._send(handler, json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                   "application/json; charset=utf-8", status=status)


def share_routes(db: Db, people_csv: Path = DEFAULT_PEOPLE_CSV) -> ShareRoutes:
    """The routes over one store, rows re-read whenever the store file changes."""
    people = SharePeople(db, people_csv=people_csv)
    cache: dict[str, Any] = {}

    def load() -> tuple:
        stamp = db.db_path.stat().st_mtime_ns
        if cache.get("stamp") != stamp:
            cache["rows"] = people.load()
            cache["stamp"] = stamp
        return cache["rows"]

    return ShareRoutes(db, people, load)


def make_handler(routes: ShareRoutes) -> type[BaseHTTPRequestHandler]:
    """A handler that serves only the People routes (tests)."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/healthz":
                routes._send_json(self, {"primitive": "people_web", "ok": True, "people": len(routes.load())})
            elif parsed.path == "/":
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", PAGE_PATH)
                self.end_headers()
            elif not routes.get(self, parsed):
                routes._send(self, b"not found", "text/plain", status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if not routes.post(self, urllib.parse.urlparse(self.path)):
                routes._send(self, b"not found", "text/plain", status=HTTPStatus.NOT_FOUND)

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler

