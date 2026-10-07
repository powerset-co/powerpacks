"""08 Review: serve the Check LinkedIn page over the v2 store, or `--check` the queue and one card.

The page is the built React app, served unchanged (its shell at `/`, its assets under /app/assets/); the
routes it calls are review/api.py. One process, one connection, one request at a time.

`--check` serves nothing: it runs the list and the heaviest family's card against the store and prints the
counts, the wall times and the query plans. It writes nothing.

Created: 2026-10-07
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries_review
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.review import payloads
from packs.ingestion.primitives.deep_context_v2.review.api import ReviewApi
from packs.ingestion.primitives.deep_context_v2.review.queue import Card, load_card, review_list
from packs.shared.web.app import AppRoutes

DEFAULT_PORT = 8777


def make_handler(api: ReviewApi) -> type[BaseHTTPRequestHandler]:
    app = AppRoutes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if not app.get(self, parsed):
                api.get(self, parsed)

        def do_POST(self) -> None:  # noqa: N802
            api.post(self, urllib.parse.urlparse(self.path))

    return Handler


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def check(conn: sqlite3.Connection, data_root: Path) -> dict[str, object]:
    """The list and one card, timed, with the plans of the SQL each runs. Counts only: no names."""
    started: float = time.perf_counter()
    rows: list[queries_review.QueueRow] = queries_review.queue(conn)
    queue_sql_ms: float = _ms(started)
    started = time.perf_counter()
    order: list[str] = review_list(conn)
    queue_ms: float = _ms(started)
    pending: int = 0
    for row in rows:
        if row.needs_review or row.has_card:
            pending += 1
    # The heaviest family, queued or not, so the card path runs on a store whose list is empty.
    parent_id: str = queries_review.first_family(conn)
    started = time.perf_counter()
    card: Card = load_card(conn, data_root, parent_id)
    card_payload: payloads.LinkedinCard = payloads.LinkedinCard(payloads.person(card), payloads.candidates(card))
    card_ms: float = _ms(started)
    started = time.perf_counter()
    fragment: str = payloads.dossier(card)
    dossier_ms: float = _ms(started)
    ids: list[str] = []
    for member in card.members:
        ids.append(member.candidate_id)
    marks: str = ", ".join(["?"] * len(ids))
    return {
        "queue": {"families": len(order), "with_pending_or_card_row": pending, "sql_ms": queue_sql_ms,
                  "total_ms": queue_ms, "plan": queries_review.query_plan(conn, queries_review.QUEUE_SQL, [])},
        "card": {
            "members": len(card.members), "identifiers": len(card.identifiers),
            "messages": sum(card.messages.values()), "pending": len(card.pending),
            "candidates_shown": len(card_payload.candidates), "dossier_bytes": len(fragment),
            "card_ms": card_ms, "dossier_ms": dossier_ms,
            "plan_members": queries_review.query_plan(conn, queries_review.MEMBERS_SQL, [parent_id]),
            "plan_verdicts": queries_review.query_plan(
                conn, f"SELECT * FROM current_linkedins WHERE candidate_id IN ({marks})", ids),
            "plan_messages": queries_review.query_plan(
                conn, "SELECT json_extract(m.value, '$.channel'), COUNT(*) FROM bundles b, "
                      f"json_each(b.payload_json, '$.messages') m WHERE b.candidate_id IN ({marks}) GROUP BY 1", ids),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="08 Review: the Check LinkedIn page over the v2 store.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--check", action="store_true", help="time the list and one card; print counts and plans")
    args = parser.parse_args(argv)
    conn: sqlite3.Connection = open_store(store_path(args.data_root))
    if args.check:
        print(json.dumps(check(conn, args.data_root), indent=2))
        return 0
    server = HTTPServer(("127.0.0.1", args.port), make_handler(ReviewApi(conn, args.data_root)))
    print(f"Check LinkedIn: http://127.0.0.1:{args.port}/?stage=linkedin")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
