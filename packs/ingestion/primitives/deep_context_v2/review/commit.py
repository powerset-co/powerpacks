"""Apply the review queue: every decision the reviewer left, written to the ledgers once, in parent-id order.

`finish` calls this before realize. A queued Yes, Skip or pasted Retarget writes through decisions.py. A
Retarget from the reviewer's words is researched here, one Parallel batch for all of them (a stored answer
to the same facts and words is reused); a URL found is confirmed, nothing found rejects the pending profile.

A row whose family is no longer in the review (an earlier finish already applied it) is dropped. A row
that cannot be applied, which is a pasted URL whose profile RapidAPI did not return, stays in the queue
and finish says so; the next finish tries it again. Each applied row leaves the queue as it commits, so a
finish that stops midway resumes, and a row the reviewer writes while finish runs is never swept away.

Created: 2026-10-08
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries_enrich, queries_review
from packs.ingestion.primitives.deep_context_v2.db.queries_enrich import Research
from packs.ingestion.primitives.deep_context_v2.db.queries_review import Queued
from packs.ingestion.primitives.deep_context_v2.db.schema import ResearchStatus, ReviewDecision
from packs.ingestion.primitives.deep_context_v2.enrich import research
from packs.ingestion.primitives.deep_context_v2.enrich.research import ResearchSubject
from packs.ingestion.primitives.deep_context_v2.review import decisions
from packs.ingestion.primitives.deep_context_v2.review.decisions import DecisionError
from packs.ingestion.primitives.deep_context_v2.review.queue import Card, load_card, review_list


def guided_research(conn: sqlite3.Connection, cards: dict[str, Card], queue: dict[str, Queued]) -> dict[str, str]:
    """parent_id -> guided research handle for every Retarget from words, researched now unless stored."""
    handles: dict[str, str] = {}
    todo: list[ResearchSubject] = []
    stored: dict[str, Research] = queries_enrich.research_by_handle(conn)
    for parent_id, queued in queue.items():
        if queued.decision == ReviewDecision.FIX and queued.guidance and parent_id in cards:
            subject: ResearchSubject = research.guided_subject(parent_id, cards[parent_id].facts, queued.guidance)
            handles[parent_id] = subject.handle
            if subject.handle not in stored:
                todo.append(subject)
    if todo:
        print(f"commit: researching {len(todo)} retarget(s) from the reviewer's words", flush=True)
        research.submit(conn, todo)
    return handles


def apply(conn: sqlite3.Connection, data_root: Path, card: Card, queued: Queued, handle: str) -> str:
    """One family's decision onto the ledgers. Returns what happened, for the log."""
    if queued.decision == ReviewDecision.KEEP:
        return "yes " + decisions.yes(conn, card, queued.key)
    if queued.decision == ReviewDecision.DETACH:
        decisions.skip(conn, card)
        return "skip"
    if not queued.guidance:
        return "retarget " + decisions.retarget(conn, data_root, card, queued.key)
    found: Research | None = queries_enrich.research_by_handle(conn).get(handle)
    if found is None or found.status != ResearchStatus.COMPLETE.value:
        decisions.reject(conn, card)
        return "research found no profile; the pending one rejected"
    return "research " + decisions.retarget(conn, data_root, card, research.research_url(found))


def commit_review(conn: sqlite3.Connection, data_root: Path) -> int:
    """Every queued decision applied and removed from the queue. Returns how many were applied."""
    queue: dict[str, Queued] = queries_review.review_queue(conn)
    listed: set[str] = set(review_list(conn))
    cards: dict[str, Card] = {}
    for parent_id in sorted(queue):
        if parent_id in listed:
            cards[parent_id] = load_card(conn, data_root, parent_id)
    handles: dict[str, str] = guided_research(conn, cards, queue)
    applied: int = 0
    for parent_id in sorted(queue):
        outcome: str = "not in the review any more, dropped"
        if parent_id in cards:
            try:
                outcome = apply(conn, data_root, cards[parent_id], queue[parent_id], handles.get(parent_id, ""))
                applied += 1
            except DecisionError as error:
                print(f"commit: {parent_id}: kept in the queue, {error}", flush=True)
                continue
        with conn:
            queries_review.delete_queued(conn, parent_id)
        print(f"commit: {parent_id}: {outcome}", flush=True)
    return applied
