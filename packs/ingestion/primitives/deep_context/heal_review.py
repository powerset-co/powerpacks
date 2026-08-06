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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB, PROFILE_CACHE_DIR, REVIEW_MANIFEST, emit,
)
from packs.ingestion.primitives.deep_context import profile_projection
from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_review
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.identity_reconcile import healing
from packs.ingestion.primitives.imports.common import write_manifest


HEAL_BATCH_CAP: int | None = None
_FETCH_WORKERS = 8
@dataclass(frozen=True)
class HealCandidate:
    parent_id: str
    parent_slug: str
    name: str
    candidate_key: str
    pub: str
    url: str


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

    def select_candidates(self) -> tuple[list[HealCandidate], int, int]:
        return healing.select_candidates(self.db, self.cap, HealCandidate, _say)

    def fetch_states(self, candidates: list[HealCandidate]) -> dict[str, dict[str, Any]]:
        return healing.fetch_states(
            self.db,
            candidates,
            self.profile_cache_dir,
            max_workers=_FETCH_WORKERS,
            say=_say,
        )

    def rejudge(self, candidates: list[HealCandidate]) -> dict[str, Any]:
        return healing.rejudge(self.db, candidates, concurrency=_FETCH_WORKERS)

    def terminate(self, candidates: list[HealCandidate]) -> dict[str, Any]:
        return healing.terminate(self.db, candidates)

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        queue_before = int(linkedin_review(self.db, "progress")["pending"])
        candidates, skipped_retarget, uncapped = self.select_candidates()
        states = self.fetch_states(candidates)
        content = [
            row for row in candidates
            if states[row.candidate_key]["state"] == profile_projection.PROFILE_CONTENT
        ]
        empty = [row for row in candidates if states[row.candidate_key]["state"] == profile_projection.PROFILE_EMPTY
                 and states[row.candidate_key].get("fetched")]
        empty_unfetched = sum(
            states[row.candidate_key]["state"] == profile_projection.PROFILE_EMPTY
            and not states[row.candidate_key].get("fetched")
            for row in candidates
        )
        rejudge = self.rejudge(content)
        terminated = self.terminate(empty)
        summary = {
            "primitive": "heal_review", "status": "completed",
            "owner_phones_backfilled": False, "legacy_scrub": {},
            "queue_pending_before": queue_before,
            "queue_pending_after": int(linkedin_review(self.db, "progress")["pending"]),
            "candidates": len(candidates), "candidates_uncapped": uncapped,
            "capped": len(candidates) < uncapped, "cap": self.cap,
            "skipped_pending_retarget": skipped_retarget,
            "profiles": {
                "content": len(content), "empty_fetched": len(empty),
                "empty_unfetched": empty_unfetched,
                "error": len(candidates) - len(content) - len(empty) - empty_unfetched,
                "fetched": sum(bool(row.get("fetched")) for row in states.values()),
                "from_cache": sum(bool(row.get("from_cache")) for row in states.values()),
            },
            "rejudge": rejudge, "terminated": terminated,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        write_manifest(
            self.review_manifest.parent.name,
            {"heal": summary},
            import_dir=self.review_manifest.parent.parent,
        )
        tail = " (nothing to do)" if not candidates else ""
        judged = 0 if rejudge["skipped_no_openai_key"] else rejudge["candidates"]
        _say(f"fetched {summary['profiles']['fetched']} · judged {judged} · "
             f"dead-links {terminated['detached']}{tail}")
        return summary

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Heal stale LinkedIn review candidates")
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--cap", type=int, default=HEAL_BATCH_CAP)
    args = parser.parse_args(argv)
    emit(HealReview(db=open_existing_db(args.db), cap=args.cap).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
