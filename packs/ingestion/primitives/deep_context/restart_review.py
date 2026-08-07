"""Restart the human review in the canonical Deep Context SQLite store.

``bin/deep-context restart`` clears only human-owned worth and identity
decisions. Machine verdicts, paid results, facts, dossiers, jobs, manifest
receipts, and cached profiles stay intact. The default is a spend-free,
read-only preview; ``--apply`` performs the whole reset in one SQLite
transaction.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from packs.ingestion.primitives.deep_context.common import CANONICAL_DB
from packs.ingestion.primitives.deep_context.db.models import (
    HUMAN_DECISION_SOURCES,
    ResetReviewCounts,
    ReviewSource,
)
from packs.ingestion.primitives.deep_context.db import identity_queries, queries
from packs.ingestion.primitives.deep_context.db.store import open_existing_db

RESET_SOURCES = (*HUMAN_DECISION_SOURCES, ReviewSource.SIBLING_SETTLE.value)


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
        "synthetic": {
            "rows": synthetic_rows,
            "cleared": synthetic_cleared,
            "status": "ok" if synthetic_rows else "missing",
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
    parser = argparse.ArgumentParser(description="Clear human review decisions; keep all machine work")
    parser.add_argument("--db", type=Path, default=CANONICAL_DB)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="clear human review state in one SQLite transaction",
    )
    args = parser.parse_args(argv)
    db = open_existing_db(args.db)
    parents = queries.parents(db)
    identity_links = identity_queries.links(db)
    synthetic_profiles = identity_queries.synthetic_profiles(db)
    review_rows = len(parents) + len(identity_links)
    synthetic_rows = len(synthetic_profiles)
    links = {row.row_key: row for row in identity_links}
    synthetic_cleared = sum(
        links[row.candidate_key].decision_source in RESET_SOURCES
        for row in synthetic_profiles
        if row.candidate_key in links
    )
    counts = db.reset_review(apply=args.apply)
    print(
        json.dumps(
            _payload(
                args.db,
                counts,
                review_rows=review_rows,
                synthetic_rows=synthetic_rows,
                synthetic_cleared=synthetic_cleared,
                applied=args.apply,
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
