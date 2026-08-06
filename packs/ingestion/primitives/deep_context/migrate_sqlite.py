"""Import the fixed pre-SQLite Deep Context artifacts exactly once."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    DEEP_RESEARCH_DIR,
    DOSSIERS_MANIFEST,
    ENRICH_MANIFEST,
    FACTS_DIR,
    FACTS_MANIFEST,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    MERGE_MANIFEST,
    PARENTS_MANIFEST,
    RAW_MANIFEST,
    RECONCILE_DIR,
    REVIEW_DIR,
    REVIEW_MANIFEST,
    ROOT,
    VERDICTS_JSONL,
)
from packs.ingestion.primitives.deep_context.db.legacy import LegacyImportError, import_legacy
from packs.ingestion.primitives.deep_context.db.store import Db


CANONICAL_DB = ROOT / "deep-context.sqlite"
SYNTHETIC_PEOPLE_CSV = LINKEDIN_OVERRIDES_CSV.parent / "synthetic-people.csv"
LEGACY_MANIFESTS = (
    RAW_MANIFEST,
    FACTS_MANIFEST,
    DOSSIERS_MANIFEST,
    MERGE_MANIFEST,
    PARENTS_MANIFEST,
    RECONCILE_DIR / "manifest.json",
    ENRICH_MANIFEST,
    REVIEW_MANIFEST,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import the fixed legacy Deep Context artifacts into fresh SQLite once."
    )
    parser.add_argument("--db", type=Path, default=CANONICAL_DB)
    args = parser.parse_args(argv)
    try:
        counts = import_legacy(
            Db(args.db),
            review_csv=LINKEDIN_OVERRIDES_CSV,
            synthetic_csv=SYNTHETIC_PEOPLE_CSV,
            index_json=INDEX_JSON,
            facts_dir=FACTS_DIR,
            verdicts_jsonl=VERDICTS_JSONL,
            research_dir=DEEP_RESEARCH_DIR,
            merged_people_csv=DEFAULT_PEOPLE_CSV,
            avatar_dir=REVIEW_DIR / "avatars",
            manifests=LEGACY_MANIFESTS,
        )
    except LegacyImportError as exc:
        print(json.dumps({
            "primitive": "migrate_deep_context_sqlite",
            "status": "refused",
            "database": str(args.db),
            "error": str(exc),
        }), file=sys.stderr)
        return 1
    print(json.dumps({
        "primitive": "migrate_deep_context_sqlite",
        "status": "completed",
        "database": str(args.db),
        "counts": counts,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
