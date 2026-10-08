"""The routes the Check LinkedIn page calls, over the v2 store.

GET  /api/review/page                              the one screen and its count
GET  /api/review/linkedin-card?exclude=&index=      the next family's card, or the finished state
GET  /api/dossier?slug=                            the family's dossier as an HTML fragment
POST /api/review/decide   form pub, decision, parent_slug, new_url: keep = Yes, detach = Skip, fix = Retarget
                          to new_url, into the review queue (a later decision on the family replaces it);
                          answers with the next card
POST /retarget            form pub, parent_slug, guidance: Retarget from the reviewer's words into the queue;
                          the PAID research starts now in the background

Nothing here writes a ledger: the queue holds every decision until `finish` applies it (review/commit.py).
POST /feedback            form pub, parent_slug, comment, action: files the comment with Powerset
POST /auth/login          runs the Powerset sign-in on this machine

Bodies are form-encoded, a POST from another origin is refused, and an error is `{"error": text}`
(the page shows the text). Any other path is a JSON 404.

Created: 2026-10-07
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import urllib.parse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries_review
from packs.ingestion.primitives.deep_context_v2.db.queries_review import Queued
from packs.ingestion.primitives.deep_context_v2.db.schema import ReviewDecision
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.enrich import research
from packs.ingestion.primitives.deep_context_v2.enrich.research import ResearchSubject
from packs.ingestion.primitives.deep_context_v2.openai import load_env
from packs.ingestion.primitives.deep_context_v2.review import decisions, payloads
from packs.ingestion.primitives.deep_context_v2.review.decisions import DecisionError
from packs.ingestion.primitives.deep_context_v2.review.payloads import DecideResult, LinkedinCard, LinkedinCardPayload, LinkedinFinished, QueuePosition
from packs.ingestion.primitives.deep_context_v2.review.queue import Card, load_card, review_list
from packs.powerset.primitives.auth.auth import main as powerset_auth
from packs.powerset.primitives.send_feedback.send_feedback import FeedbackRequest, SendFeedback, default_set_id

MAX_FORM_BYTES = 32_768
MAX_TEXT_CHARS = 4_000   # a comment or a description
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


class ReviewApi:
    """The review over the store: the families with something to decide, in parent-id order, and the queue
    of decisions the reviewer has made so far (review_queue, edited in place; `finish` applies it)."""

    def __init__(self, conn: sqlite3.Connection, data_root: Path) -> None:
        self.conn = conn
        self.data_root = data_root

    def linkedin_card(self, params: Params) -> LinkedinCardPayload:
        """Browse the fixed order (`index`), or the next family without a decision after `after`."""
        order: list[str] = review_list(self.conn)
        queued: dict[str, Queued] = queries_review.review_queue(self.conn)
        pending: int = len([slug for slug in order if slug not in queued])
        asked: str = _value(params, "index")
        slug: str = ""
        if asked.isdigit() and int(asked) < len(order):
            slug = order[int(asked)]
        else:
            after: str = _value(params, "after")
            start: int = order.index(after) + 1 if after in order else 0
            excluded: list[str] = _value(params, "exclude").split(",")
            for key in order[start:] + order[:start]:
                if key not in queued and key not in excluded:
                    slug = key
                    break
        total: int = len(order) + (pending == 0)
        if not slug:
            position = QueuePosition(len(order), total) if order else None
            return LinkedinCardPayload(None, LinkedinFinished(False), pending, position)
        card: Card = load_card(self.conn, self.data_root, slug)
        status: str = queued[slug].decision if slug in queued else ""
        position = QueuePosition(order.index(slug), total, status)
        return LinkedinCardPayload(LinkedinCard(payloads.person(card), payloads.candidates(card)), None,
                                   pending, position)

    def decide(self, form: Params) -> DecideResult:
        """One decision on one family into the queue: Yes (keep), Skip (detach) or Retarget (fix); a later
        one replaces it. Answers with the next card."""
        pub: str = _value(form, "pub")
        decision: str = _value(form, "decision")
        slug: str = _value(form, "parent_slug")
        if slug not in review_list(self.conn):
            raise Refusal(HTTPStatus.CONFLICT, "This family is not in this review. Reload the page.")
        card: Card = load_card(self.conn, self.data_root, slug)
        key: str = ""
        try:
            if decision == ReviewDecision.KEEP:
                key = decisions.chosen(card, pub).key
            elif decision == ReviewDecision.FIX:
                key = decisions.fetch_profile(self.data_root, _value(form, "new_url"))[0]
            elif decision != ReviewDecision.DETACH:
                raise Refusal(HTTPStatus.BAD_REQUEST, f"unknown decision: {decision}")
        except DecisionError as error:
            raise Refusal(HTTPStatus.BAD_REQUEST, str(error)) from error
        with self.conn:
            queries_review.upsert_queued(self.conn, Queued(slug, decision, key, ""), now_iso())
        return DecideResult(True, self.linkedin_card({"after": [slug]}))

    def retarget(self, form: Params) -> None:
        """Retarget from the reviewer's words: the decision goes into the queue and the research starts now,
        in the background, so its answer is ready when `finish` applies the queue. The queue advances."""
        slug: str = _value(form, "parent_slug")
        guidance: str = _value(form, "guidance").strip()
        if not guidance or len(guidance) > MAX_TEXT_CHARS:
            raise Refusal(HTTPStatus.BAD_REQUEST, f"describe the person in 1-{MAX_TEXT_CHARS} characters")
        if slug not in review_list(self.conn):
            raise Refusal(HTTPStatus.CONFLICT, "This family is not in this review. Reload the page.")
        card: Card = load_card(self.conn, self.data_root, slug)
        subject: ResearchSubject = research.guided_subject(card.parent_id, card.facts, guidance)
        with self.conn:
            queries_review.upsert_queued(self.conn, Queued(slug, ReviewDecision.RESEARCH.value, subject.handle, guidance),
                                         now_iso())
        threading.Thread(target=self._research, args=(subject,), daemon=True).start()

    def _research(self, subject: ResearchSubject) -> None:
        # Its own connection: the server's is serialized on the request thread. The answer is a research row.
        conn: sqlite3.Connection = open_store(store_path(self.data_root))
        try:
            counts: dict[str, int] = research.submit(conn, [subject])
            print(f"[retarget] {subject.parent_id}: {counts}", file=sys.stderr, flush=True)
        except Exception as error:
            print(f"[retarget] {subject.parent_id} failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        finally:
            conn.close()

    def feedback(self, form: Params) -> dict[str, object]:
        """Files the reviewer's comment on a family with Powerset; the reply is Powerset's status."""
        comment: str = _value(form, "comment").strip()
        slug: str = _value(form, "parent_slug")
        if not comment or len(comment) > MAX_TEXT_CHARS:
            raise Refusal(HTTPStatus.BAD_REQUEST, f"comment must be 1-{MAX_TEXT_CHARS} characters")
        if slug not in review_list(self.conn):
            raise Refusal(HTTPStatus.NOT_FOUND, "This family is not in the review.")
        card: Card = load_card(self.conn, self.data_root, slug)
        pending: list[str] = []
        for item in card.pending:
            pending.append(item.linkedin_url)
        metadata: dict[str, object] = {"source": "powerpacks-directory", "action": _value(form, "action"),
                                       "person_name": card.members[0].display_name, "parent_slug": slug,
                                       "candidate_key": _value(form, "pub"), "pending_linkedin_urls": pending}
        load_env()
        request = FeedbackRequest(comment=comment, category="linkedin", metadata=metadata, set_id=default_set_id())
        return SendFeedback(request, env_file=self.data_root.parent / ".env").run()

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> None:
        """Route a GET: the dossier fragment, the page, or the next card."""
        params: Params = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/dossier":
            card: Card = load_card(self.conn, self.data_root, _value(params, "slug"))
            _send(handler, payloads.dossier(card).encode(), "text/html; charset=utf-8", HTTPStatus.OK)
        elif parsed.path == "/api/review/page":
            _send_json(handler, asdict(payloads.page(self.linkedin_card({}).pending)))
        elif parsed.path == "/api/review/linkedin-card":
            _send_json(handler, asdict(self.linkedin_card(params)))
        else:
            _send_json(handler, {"error": "not found"}, HTTPStatus.NOT_FOUND)

    def post(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> None:
        # A page on another site must not be able to decide for the user.
        """Route a POST: a decision, a re-research, feedback, or the sign-in."""
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
                self.retarget(form)
                _send_json(handler, {"ok": True}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/feedback":
                reply: dict[str, object] = self.feedback(form)
                submitted: bool = reply["status"] == "submitted"
                _send_json(handler, {"ok": submitted, **reply}, HTTPStatus.OK if submitted else HTTPStatus.BAD_GATEWAY)
            elif parsed.path == "/auth/login":
                load_env()
                if powerset_auth(["login"]) == 0:
                    _send_json(handler, {"ok": True, "status": "authenticated"})
                else:
                    _send_json(handler, {"status": "needs_auth", "error": "Sign-in did not complete. Try again."},
                               HTTPStatus.UNAUTHORIZED)
            else:
                raise Refusal(HTTPStatus.NOT_FOUND, "not found")
        except Refusal as refusal:
            _send_json(handler, {"error": refusal.text}, refusal.status)
