"""Apply the review queue: every decision the reviewer left, written to the ledgers once, in parent-id order.

`finish` calls this before realize. A queued Yes, Skip or pasted Retarget writes through decisions.py. A
described Retarget waits for its guided research (started by the page when the words were typed; a few
minutes at most) and then confirms the URL it found, or rejects the pending profile when it found none.
Each family's row leaves the queue as its rows commit, so a finish that stops midway resumes where it was.

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
from packs.ingestion.primitives.deep_context_v2.review.queue import Card, load_card

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
    """Every queued decision, applied and removed from the queue. Returns how many."""
    queue: dict[str, Queued] = queries_review.review_queue(conn)
    handles: list[str] = []
    for queued in queue.values():
        if queued.decision == ReviewDecision.RESEARCH:
            handles.append(queued.key)
    research: dict[str, Research] = wait_for_research(conn, handles) if handles else {}
    for parent_id in sorted(queue):
        card: Card = load_card(conn, data_root, parent_id)
        outcome: str = apply(conn, data_root, card, queue[parent_id], research)
        with conn:
            queries_review.delete_queued(conn, parent_id)
        print(f"commit: {parent_id}: {outcome}", flush=True)
    return len(queue)
