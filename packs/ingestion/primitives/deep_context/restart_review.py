"""Restart the human review in the canonical Deep Context SQLite store.

``bin/deep-context restart`` clears only human-owned worth and identity
decisions, rewinds the staged review state, and removes stale spend approvals.
Machine verdicts, paid results, facts, dossiers, jobs, and cached profiles stay
intact. The default is a spend-free, read-only preview; ``--apply`` performs the
whole reset in one SQLite transaction.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from packs.ingestion.primitives.deep_context.common import ROOT
from packs.ingestion.primitives.deep_context.db.models import (
    HUMAN_DECISION_SOURCES,
    ResetReviewCounts,
    ReviewSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db

CANONICAL_DB = ROOT / "deep-context.sqlite"
REVIEW_STAGES = ("worth", "enrich", "enrichment", "linkedin", "review")
RESET_SOURCES = (*HUMAN_DECISION_SOURCES, ReviewSource.SIBLING_SETTLE.value)


def preview_reset(db: Db) -> ResetReviewCounts:
    """Count the rows the reset transaction will target without writing."""
    worth = db.query(
        "SELECT count(*) FROM parents WHERE human_worth IS NOT NULL "
        "OR human_worth_note IS NOT NULL OR human_worth_source IS NOT NULL "
        "OR human_worth_at IS NOT NULL"
    )[0][0]
    identity = db.query(
        "SELECT count(*) FROM links WHERE decision_source IN (?, ?, ?)",
        RESET_SOURCES,
    )[0][0]
    stages = db.query(
        "SELECT count(*) FROM stage_state WHERE stage IN (?, ?, ?, ?, ?)",
        REVIEW_STAGES,
    )[0][0]
    approvals = db.query(
        "SELECT count(*) FROM spend_approvals WHERE stage IN (?, ?, ?, ?, ?)",
        REVIEW_STAGES,
    )[0][0]
    return ResetReviewCounts(worth, identity, stages, approvals)


def _payload(
    db_path: Path,
    counts: ResetReviewCounts,
    *,
    review_rows: int,
    synthetic_rows: int,
    synthetic_cleared: int,
    applied: bool,
) -> dict[str, object]:
    status = "applied" if applied else "dry_run"
    return {
        "primitive": "restart_review",
        "db": str(db_path),
        "review_rows": review_rows,
        "human_worth_cleared": counts.human_worth_cleared,
        "human_identity_cleared": counts.human_identity_cleared,
        "stage_states_reset": counts.stage_states_reset,
        "spend_approvals_cleared": counts.spend_approvals_cleared,
        "synthetic": {
            "rows": synthetic_rows,
            "cleared": synthetic_cleared,
            "status": "ok" if synthetic_rows else "missing",
        },
        "manifest": {
            "status": (
                "reset" if applied else "would_reset"
            ) if counts.stage_states_reset else "missing",
        },
        "status": status,
        "next": (
            "rerun `bin/deep-context review --stage worth --fresh` — the queue "
            "re-opens with the machine verdicts intact"
            if applied
            else "pass --apply to clear the human review state atomically"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clear human review decisions; keep all machine work"
    )
    parser.add_argument("--db", type=Path, default=CANONICAL_DB)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="clear human review state in one SQLite transaction",
    )
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error(f"Deep Context SQLite store does not exist: {args.db}")

    db = Db(args.db)
    review_rows = len(db.rows())
    synthetic_rows = db.query("SELECT count(*) FROM synthetic_profiles")[0][0]
    synthetic_cleared = db.query(
        "SELECT count(*) FROM synthetic_profiles s JOIN links l "
        "ON l.row_key=s.candidate_key WHERE l.decision_source IN (?, ?, ?)",
        RESET_SOURCES,
    )[0][0]
    counts = db.reset_review() if args.apply else preview_reset(db)
    print(json.dumps(_payload(
        args.db,
        counts,
        review_rows=review_rows,
        synthetic_rows=synthetic_rows,
        synthetic_cleared=synthetic_cleared,
        applied=args.apply,
    ), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
