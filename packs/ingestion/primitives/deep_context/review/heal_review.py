"""Heal stale LinkedIn identities from SQLite before the review UI serves.

Fresh-hydrate judge-skipped links, reuse the shared judge, detach confirmed dead
links, stand an existing synthetic identity, and stamp the fixed review manifest.

Flow: SQLite selection -> RapidAPI hydrate -> shared judge -> SQLite settlement
-> fixed manifest.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import asdict
from pathlib import Path

from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB, PROFILE_CACHE_DIR, REVIEW_MANIFEST, emit,
)
from packs.ingestion.primitives.enrich import rapidapi_client
from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_progress
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile import healing
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
    HealCandidate,
    HealFetchResult,
    HealProfileCounts,
    HealRejudgeResult,
    HealReviewSummary,
    HealSelection,
    HealTerminationResult,
)
from packs.ingestion.primitives.imports.common import write_manifest


HEAL_BATCH_CAP: int | None = None
_FETCH_WORKERS = 8


def _say(line: str) -> None:
    print(f"[heal] {line}", file=sys.stderr, flush=True)


class HealReview:
    """Construct and run the SQLite-backed self-heal policy."""

    def __init__(self, *, db: Db, profile_cache_dir: Path | None = None,
                 review_manifest: Path | None = None,
                 cap: int | None = HEAL_BATCH_CAP) -> None:
        self.db = db
        self.profile_cache_dir = Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.review_manifest = Path(review_manifest or REVIEW_MANIFEST)
        self.cap = None if cap is None else max(1, int(cap))

    def select_candidates(self) -> HealSelection:
        return healing.select_candidates(self.db, self.cap, _say)

    def fetch_states(self, candidates: tuple[HealCandidate, ...]) -> HealFetchResult:
        return healing.fetch_states(
            self.db,
            candidates,
            self.profile_cache_dir,
            max_workers=_FETCH_WORKERS,
            say=_say,
        )

    def rejudge(self, candidates: tuple[HealCandidate, ...]) -> HealRejudgeResult:
        return healing.rejudge(self.db, candidates, concurrency=_FETCH_WORKERS)

    def terminate(
        self,
        candidates: tuple[HealCandidate, ...],
    ) -> HealTerminationResult:
        return healing.terminate(self.db, candidates)

    def run(self) -> HealReviewSummary:
        started = time.monotonic()
        queue_before = linkedin_progress(self.db).pending
        selection = self.select_candidates()
        candidates = selection.candidates
        states = self.fetch_states(candidates)
        content = tuple(
            row for row in candidates
            if states.state_for(row.candidate_key).state
            == rapidapi_client.PROFILE_CONTENT
        )
        empty = tuple(
            row
            for row in candidates
            if states.state_for(row.candidate_key).state
            == rapidapi_client.PROFILE_EMPTY
            and states.state_for(row.candidate_key).fetched
        )
        empty_unfetched = sum(
            states.state_for(row.candidate_key).state
            == rapidapi_client.PROFILE_EMPTY
            and not states.state_for(row.candidate_key).fetched
            for row in candidates
        )
        rejudge = self.rejudge(content)
        terminated = self.terminate(empty)
        profiles = HealProfileCounts(
            content=len(content),
            empty_fetched=len(empty),
            empty_unfetched=empty_unfetched,
            error=len(candidates) - len(content) - len(empty) - empty_unfetched,
            fetched=sum(row.fetched for row in states.states),
            from_cache=sum(row.from_cache for row in states.states),
        )
        summary = HealReviewSummary(
            primitive="heal_review",
            status="completed",
            queue_pending_before=queue_before,
            queue_pending_after=linkedin_progress(self.db).pending,
            candidates=len(candidates),
            candidates_uncapped=selection.uncapped,
            capped=len(candidates) < selection.uncapped,
            cap=self.cap,
            skipped_pending_retarget=selection.skipped_pending_retarget,
            profiles=profiles,
            rejudge=rejudge,
            terminated=terminated,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        write_manifest(
            self.review_manifest.parent.name,
            {"heal": asdict(summary)},
            import_dir=self.review_manifest.parent.parent,
        )
        tail = " (nothing to do)" if not candidates else ""
        judged = 0 if rejudge.skipped_no_openai_key else rejudge.candidates
        _say(f"fetched {profiles.fetched} · judged {judged} · "
             f"dead-links {terminated.detached}{tail}")
        return summary

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Heal stale LinkedIn review candidates")
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--cap", type=int, default=HEAL_BATCH_CAP)
    args = parser.parse_args(argv)
    emit(asdict(HealReview(db=open_existing_db(args.db), cap=args.cap).run()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
