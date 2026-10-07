"""The People page's data routes: people, tags, and upload progress.

Flow: `ShareRoutes` mounts under `/api/people/` in the one page server
(packs/shared/web/server.py, `/people`), after `AppRoutes` (the page and its
assets); `make_handler` serves the two alone for tests. GET `/api/people/rows` ->
`SharePeople.load()` as one columnar payload, one row per family; POST
`/api/people/tags` carries the tags each selected family should hold (absolute
sets, so undo re-posts the previous sets), writes the tag rows for every member
of those families and re-decides their share rows through
`labels.share_decision` from `person_labels`, in one transaction.
GET `/api/people/upload` reads progress; POST `/api/people/upload/check` previews;
POST `/api/people/upload` confirms and starts one shared upload.

Changelog:
  2026-10-07: v2 store; a decision writes every member of the family.
  2026-09-26: created.
  2026-09-26: serves the React build from web/dist; legacy page and vendor/ removed.
  2026-09-26: the page and its assets moved to packs/shared/web/app.py.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import sys
import urllib.parse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context_v2.db import queries_share
from packs.ingestion.primitives.deep_context_v2.db.queries_share import ShareRow, TagRow
from packs.ingestion.primitives.deep_context_v2.db.store import store_path
from packs.ingestion.primitives.deep_context_v2.realize.realize import PEOPLE_CSV_RELATIVE_PATH
from packs.ingestion.primitives.share.labels import label_row_from_export, share_decision
from packs.ingestion.primitives.share.models import HumanTags, PersonLabelRow, ShareDecisionRow
from packs.ingestion.primitives.share.store import TAG_VOCABULARY, TagStore, join_tags
from packs.ingestion.primitives.share.web.model import SharePeople, people_payload
from packs.ingestion.primitives.share.web.upload import ShareUpload
from packs.indexing.primitives.upload_powerset.upload_powerset import DEFAULT_DB, DEFAULT_OUT_DIR
from packs.shared.web.app import AppRoutes

API_PREFIX = "/api/people/"
# The hosts this machine answers on: a same-origin request must name one of them.
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_TAGS_REQUEST_BYTES = 4 * 1024 * 1024
GZIP_MIN_BYTES = 8 * 1024

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


def decide_tags(conn: sqlite3.Connection, people: SharePeople, changes: TagChanges) -> dict[str, ShareDecisionRow]:
    """Tag every member of each family and re-decide its share row, all in one transaction. Returns
    the re-decided row by family."""
    for row in people.load():
        if row.parent_id in changes and row.in_progress:
            raise ValueError(f"{row.name} is being updated; finish the run first")
    held = TagStore(conn).load()
    labels = queries_share.labels_by_candidate(conn)
    members: dict[str, list[str]] = {}
    for row in conn.execute("SELECT parent_id, candidate_id FROM current_parent"):
        members.setdefault(row["parent_id"], []).append(row["candidate_id"])
    updated_at = now_iso()
    tag_rows: list[TagRow] = []
    share_rows: list[ShareRow] = []
    decided: dict[str, ShareDecisionRow] = {}
    for parent_id, tags in changes.items():
        prior = held.get(parent_id)
        human = HumanTags(person_id=parent_id, tags=tags, note=prior.note if prior else "", updated_at=updated_at)
        family = members[parent_id]
        label = labels[family[0]]
        decision = share_decision(label_row_from_export(PersonLabelRow(
            parent_id, label.public_identifier, label.full_name, label.worth, label.flag, label.labels_json,
            label.updated_at)), human, updated_at=updated_at)
        decided[parent_id] = decision
        for candidate_id in family:
            tag_rows.append((candidate_id, join_tags(tags), human.note, updated_at))
            share_rows.append((candidate_id, decision.public_identifier, decision.share, decision.reason,
                               decision.labels, decision.source, updated_at))
    queries_share.decide_share(conn, tag_rows, share_rows)
    return decided


class ShareRoutes:
    """The People page's data GET and POST routes, mountable in any stdlib handler."""

    def __init__(self, conn: sqlite3.Connection, people: SharePeople, load: Callable[[], tuple], upload: ShareUpload) -> None:
        self.conn = conn
        self.people = people
        self.load = load
        self.upload = upload

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == f"{API_PREFIX}rows":
            self._send_json(handler, people_payload(self.load()))
        elif parsed.path == f"{API_PREFIX}upload":
            self._send_json(handler, self.upload.status())
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
        if parsed.path not in {f"{API_PREFIX}tags", f"{API_PREFIX}upload", f"{API_PREFIX}upload/check"}:
            return False
        origin = (handler.headers.get("Origin") or "").strip()
        host = (handler.headers.get("Host") or "").strip()
        scheme = "https" if getattr(handler.connection, "cipher", None) else "http"
        hostname = (urllib.parse.urlsplit(f"//{host}").hostname or "").lower()
        # The page's own origin only: same scheme, host and port, and that host is this machine.
        if origin and (origin != f"{scheme}://{host}" or hostname not in LOCAL_HOSTS):
            self._send(handler, b"cross-origin request rejected", "text/plain", status=HTTPStatus.FORBIDDEN)
            return True
        if parsed.path in {f"{API_PREFIX}upload", f"{API_PREFIX}upload/check"}:
            length = int(handler.headers.get("Content-Length") or 0)
            try:
                body = json.loads(handler.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json(handler, {"error": "request body must be JSON"}, status=HTTPStatus.BAD_REQUEST)
                return True
            checked = body.get("checked") if isinstance(body, dict) else None
            try:
                status = self.upload.start(dry_run=parsed.path.endswith("/check"),
                                           checked=checked if isinstance(checked, str) else None)
            except ValueError as exc:
                self._send_json(handler, {"error": str(exc)}, status=HTTPStatus.CONFLICT)
            else:
                self._send_json(handler, status)
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
        try:
            decided = decide_tags(self.conn, self.people, changes)
        except ValueError as exc:
            self._send_json(handler, {"error": str(exc)}, status=HTTPStatus.CONFLICT)
            return True
        self._send_json(handler, {"rows": [
            {"parent_id": parent_id, "share": row.share, "reason": row.reason,
             "share_source": row.source, "tags": sorted(changes[parent_id])}
            for parent_id, row in decided.items()]})
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


def share_routes(conn: sqlite3.Connection, data_root: Path, *, upload_db: Path | None = None,
                 upload_dir: Path | None = None) -> ShareRoutes:
    """The routes over one store, rows re-read whenever the store changes."""
    people = SharePeople(conn, data_root)
    cache: dict[str, Any] = {}

    def load() -> tuple:
        stamp = conn.execute("PRAGMA data_version").fetchone()[0], people.export_rows() is not None and people._stamp
        if cache.get("stamp") != stamp:
            cache["rows"] = people.load()
            cache["stamp"] = stamp
        return cache["rows"]

    upload = ShareUpload(store_path(data_root), data_root / PEOPLE_CSV_RELATIVE_PATH, index_db=upload_db or DEFAULT_DB,
                         out_dir=upload_dir or DEFAULT_OUT_DIR)
    return ShareRoutes(conn, people, load, upload)


def make_handler(routes: ShareRoutes) -> type[BaseHTTPRequestHandler]:
    """A handler that serves only the People page and its routes (tests)."""
    app = AppRoutes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/healthz":
                routes._send_json(self, {"primitive": "people_web", "ok": True, "people": len(routes.load())})
            elif parsed.path == "/":
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/people")
                self.end_headers()
            elif not (app.get(self, parsed) or routes.get(self, parsed)):
                routes._send(self, b"not found", "text/plain", status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if not routes.post(self, urllib.parse.urlparse(self.path)):
                routes._send(self, b"not found", "text/plain", status=HTTPStatus.NOT_FOUND)

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler
