"""The routes the Check LinkedIn page calls, over the v2 store.

GET  /api/review/page                              the one screen and its count
GET  /api/review/linkedin-card?exclude=&index=&debug=  the next family's card, or the finished state
GET  /api/dossier?slug=                            the family's dossier as an HTML fragment
POST /api/review/decide   form pub, decision, parent_slug, new_url:
                          keep = Yes, detach = Skip, fix = Retarget to new_url; answers with the next card
POST /retarget            describe-the-person re-research: refused, a pasted URL is the Retarget

Bodies are form-encoded, a POST from another origin is refused, and an error is `{"error": text}`
(the page shows the text). Any other path is a JSON 404.

Created: 2026-10-07
"""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.review import decisions, payloads
from packs.ingestion.primitives.deep_context_v2.review.decisions import DecisionError
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso
from packs.ingestion.primitives.deep_context_v2.node import MANIFEST_RELATIVE_DIR
from packs.ingestion.primitives.deep_context_v2.review.payloads import DecideResult, LinkedinCard, LinkedinCardPayload, LinkedinFinished, QueuePosition
from packs.ingestion.primitives.deep_context_v2.review.queue import Card, load_card, review_list

MAX_FORM_BYTES = 32_768
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
Params = dict[str, list[str]]


class Refusal(Exception):
    def __init__(self, status: HTTPStatus, text: str) -> None:
        super().__init__(text)
        self.status = status
        self.text = text


def _value(params: Params, key: str) -> str:
    values: list[str] = params.get(key, [""])
    return values[0]


def _send(handler: BaseHTTPRequestHandler, body: bytes, content_type: str, status: int) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def _send_json(handler: BaseHTTPRequestHandler, payload: dict[str, object], status: int = HTTPStatus.OK) -> None:
    _send(handler, json.dumps(payload).encode(), "application/json; charset=utf-8", status)


REVIEW_MANIFEST = MANIFEST_RELATIVE_DIR / "review.json"


def record_review_complete(data_root: Path) -> None:
    """The review's manifest, written the moment the queue is empty: what the advisor watches to run
    `finish`. `run` removes it before a new review starts."""
    path: Path = data_root / REVIEW_MANIFEST
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"stage": "review", "status": "completed", "started_at": "", "finished_at": now_iso(),
                "counts": {"pending": 0}, "error": None}
    path.write_text(json.dumps(manifest, indent=2) + "\n")


class ReviewApi:
    def __init__(self, conn: sqlite3.Connection, data_root: Path) -> None:
        self.conn = conn
        self.data_root = data_root

    def linkedin_card(self, params: Params) -> LinkedinCardPayload:
        """The family at `index` among the list minus `exclude` (the card on screen, a save in flight)."""
        excluded: set[str] = set()
        for slug in _value(params, "exclude").split(","):
            if slug.strip():
                excluded.add(slug.strip())
        order: list[str] = review_list(self.conn)
        if not order:
            record_review_complete(self.data_root)
        shown: list[str] = []
        for parent_id in order:
            if parent_id not in excluded:
                shown.append(parent_id)
        if not shown:
            return LinkedinCardPayload(None, LinkedinFinished(False), len(order), None)
        index: int = 0
        if _value(params, "index").isdigit():
            index = int(_value(params, "index")) % len(shown)
        card: Card = load_card(self.conn, self.data_root, shown[index])
        position: QueuePosition | None = None
        if _value(params, "debug") == "1":
            position = QueuePosition(index, len(shown))
        return LinkedinCardPayload(LinkedinCard(payloads.person(card), payloads.candidates(card)), None,
                                   len(order), position)

    def decide(self, form: Params) -> DecideResult:
        """One human decision on one family: Yes (keep), Skip (detach) or Retarget (fix); answers with the next card."""
        pub: str = _value(form, "pub")
        decision: str = _value(form, "decision")
        slug: str = _value(form, "parent_slug")
        # A stale tab can post for a family already decided or moved onto li:; it is no longer in the list.
        if slug not in review_list(self.conn):
            raise Refusal(HTTPStatus.CONFLICT, "This family was already decided. Reload the page.")
        card: Card = load_card(self.conn, self.data_root, slug)
        try:
            if decision == "keep":
                decisions.yes(self.conn, card, pub)
            elif decision == "detach":
                decisions.skip(self.conn, card)
            elif decision == "fix":
                decisions.retarget(self.conn, self.data_root, card, _value(form, "new_url"))
            else:
                raise Refusal(HTTPStatus.BAD_REQUEST, f"unknown decision: {decision}")
        except DecisionError as error:
            raise Refusal(HTTPStatus.BAD_REQUEST, str(error)) from error
        # The next card is read after the write committed, so the decided family is never served back.
        return DecideResult(True, self.linkedin_card({"exclude": [slug]}))

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> None:
        """Route a GET: the dossier fragment, the page, or the next card."""
        params: Params = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/dossier":
            card: Card = load_card(self.conn, self.data_root, _value(params, "slug"))
            _send(handler, payloads.dossier(card).encode(), "text/html; charset=utf-8", HTTPStatus.OK)
        elif parsed.path == "/api/review/page":
            pending: int = len(review_list(self.conn))
            if not pending:
                record_review_complete(self.data_root)  # a queue empty before the first card is a complete review
            _send_json(handler, asdict(payloads.page(pending)))
        elif parsed.path == "/api/review/linkedin-card":
            _send_json(handler, asdict(self.linkedin_card(params)))
        else:
            _send_json(handler, {"error": "not found"}, HTTPStatus.NOT_FOUND)

    def post(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> None:
        # A page on another site must not be able to decide for the user.
        """Route a POST: a decision, or the refused describe-the-person retarget."""
        origin: str = handler.headers.get("Origin") or ""
        if origin and urllib.parse.urlparse(origin).hostname not in LOCAL_HOSTS:
            _send_json(handler, {"error": "cross-origin request rejected"}, HTTPStatus.FORBIDDEN)
            return
        length: int = min(int(handler.headers.get("Content-Length", "0")), MAX_FORM_BYTES)
        form: Params = urllib.parse.parse_qs(handler.rfile.read(length).decode())
        try:
            if parsed.path == "/api/review/decide":
                _send_json(handler, asdict(self.decide(form)))
            elif parsed.path == "/retarget":
                raise Refusal(HTTPStatus.NOT_IMPLEMENTED,
                              "Re-research from a description is not available. Paste the LinkedIn URL instead.")
            else:
                raise Refusal(HTTPStatus.NOT_FOUND, "not found")
        except Refusal as refusal:
            _send_json(handler, {"error": refusal.text}, refusal.status)
