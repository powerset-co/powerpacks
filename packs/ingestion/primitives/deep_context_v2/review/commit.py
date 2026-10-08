"""Apply the review queue: every decision the reviewer left, written to the ledgers once, in parent-id order.

`finish` calls this before realize. A queued Yes, Skip or pasted Retarget writes through decisions.py. A
described Retarget waits for its guided research (started by the page when the words were typed; a few
minutes at most) and then confirms the URL it found, or rejects the pending profile when it found none.

A row whose family is no longer in the review (a rerun merged it away, or an earlier finish already applied
it) is dropped; a decision that cannot be applied any more (its pending profile is gone) is logged and
dropped. The queue is emptied at the end, whatever happened, so the next review starts over.

Created: 2026-10-08
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries_enrich, queries_review
from packs.ingestion.primitives.deep_context_v2.db.queries_enrich import Research
from packs.ingestion.primitives.deep_context_v2.db.queries_review import Queued
from packs.ingestion.primitives.deep_context_v2.db.schema import ResearchStatus, ReviewDecision
from packs.ingestion.primitives.deep_context_v2.enrich.research import research_url
from packs.ingestion.primitives.deep_context_v2.review import decisions
from packs.ingestion.primitives.deep_context_v2.review.decisions import DecisionError
from packs.ingestion.primitives.deep_context_v2.review.queue import Card, load_card, review_list

RESEARCH_WAIT_SECONDS = 600   # a guided research still running when finish starts
RESEARCH_POLL_SECONDS = 5


def wait_for_research(conn: sqlite3.Connection, handles: list[str]) -> dict[str, Research]:
    """The research rows for the handles, waiting for the ones still running."""
    deadline: float = time.monotonic() + RESEARCH_WAIT_SECONDS
    while True:
        found: dict[str, Research] = queries_enrich.research_by_handle(conn)
        missing: list[str] = []
        for handle in handles:
            if handle not in found:
                missing.append(handle)
        if not missing or time.monotonic() >= deadline:
            return found
        print(f"commit: waiting for {len(missing)} research run(s)", flush=True)
        time.sleep(RESEARCH_POLL_SECONDS)


def apply(conn: sqlite3.Connection, data_root: Path, card: Card, queued: Queued, research: dict[str, Research]) -> str:
    """One family's decision onto the ledgers. Returns what happened, for the log."""
    if queued.decision == ReviewDecision.KEEP:
        return "yes " + decisions.yes(conn, card, queued.key)
    if queued.decision == ReviewDecision.DETACH:
        decisions.skip(conn, card)
        return "skip"
    if queued.decision == ReviewDecision.FIX:
        return "retarget " + decisions.retarget(conn, data_root, card, queued.key)
    found: Research | None = research.get(queued.key)
    decisions.reject(conn, card)
    if found is None or found.status != ResearchStatus.COMPLETE.value:
        return "research found no profile"
    return "research " + decisions.retarget(conn, data_root, card, research_url(found))


def commit_review(conn: sqlite3.Connection, data_root: Path) -> int:
    """Every queued decision applied, then the queue emptied. Returns how many were applied."""
    queue: dict[str, Queued] = queries_review.review_queue(conn)
    listed: set[str] = set(review_list(conn))
    handles: list[str] = []
    for queued in queue.values():
        if queued.decision == ReviewDecision.RESEARCH and queued.parent_id in listed:
            handles.append(queued.key)
    research: dict[str, Research] = wait_for_research(conn, handles) if handles else {}
    applied: int = 0
    for parent_id in sorted(queue):
        if parent_id not in listed:
            print(f"commit: {parent_id}: not in the review any more, dropped", flush=True)
            continue
        card: Card = load_card(conn, data_root, parent_id)
        try:
            outcome: str = apply(conn, data_root, card, queue[parent_id], research)
            applied += 1
        except DecisionError as error:
            outcome = f"not applied: {error}"
        print(f"commit: {parent_id}: {outcome}", flush=True)
    with conn:
        queries_review.clear_queue(conn)
    return applied
