"""Import the fixed pre-SQLite Deep Context artifacts exactly once."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    DEFAULT_PEOPLE_CSV,
    DEEP_RESEARCH_DIR,
    FACTS_DIR,
    LINKEDIN_OVERRIDES_CSV,
    MERGE_CSV,
    OWNER_JSON,
    PROFILE_CACHE_DIR,
    RAW_DIR,
    REVIEW_DIR,
    VERDICTS_JSONL,
)
from packs.ingestion.primitives.deep_context.db.legacy import (
    LEGACY_INDEX_JSON,
    LEGACY_MERGE_VERDICTS_CSV,
    LegacyImportError,
    import_legacy,
)
from packs.ingestion.primitives.deep_context.db.store import Db

SYNTHETIC_PEOPLE_CSV = LINKEDIN_OVERRIDES_CSV.parent / "synthetic-people.csv"


def legacy_artifacts_present(deep_context_dir: Path, review_csv: Path) -> bool:
    """Detect a pre-SQLite install without opening or mutating its artifacts."""
    root = Path(deep_context_dir)
    if Path(review_csv).is_file() or (root / "index.json").is_file():
        return True
    return any(
        next((directory.glob(pattern)), None) is not None
        for directory, pattern in (
            (root / "facts", "*.jsonl"),
            (root / "raw", "*.json"),
            (root / "dossiers", "*.md"),
            (root / "reconcile", "verdicts.jsonl"),
        )
        if directory.is_dir()
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
            index_json=LEGACY_INDEX_JSON,
            facts_dir=FACTS_DIR,
            verdicts_jsonl=VERDICTS_JSONL,
            research_dir=DEEP_RESEARCH_DIR,
            merged_people_csv=DEFAULT_PEOPLE_CSV,
            owner_json=OWNER_JSON,
            profile_cache_dir=PROFILE_CACHE_DIR,
            avatar_dir=REVIEW_DIR / "avatars",
            merge_verdicts_csv=LEGACY_MERGE_VERDICTS_CSV,
            merge_csv=MERGE_CSV,
            raw_dir=RAW_DIR,
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
