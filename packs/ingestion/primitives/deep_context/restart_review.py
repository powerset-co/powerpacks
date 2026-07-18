"""Restart the human review while preserving every machine (LLM) verdict.

``bin/deep-context restart`` clears the HUMAN-owned decisions so the staged
review can be taken from the top without re-spending anything:

- review.csv ``network_worth`` cells are blanked (the sticky human worth marks);
- synthetic-people.csv ``approved`` cells are reset to pending (they mirror
  human worth clicks and are re-derived / re-clicked for free).

Machine columns (``llm_worth`` / ``llm_worth_reason`` / reject columns), facts,
dossiers, enrichment results, and profile caches are untouched — the machine's
work survives; only the human's answers are cleared. This is the complement of
``rejudge`` (which refreshes the MACHINE's verdicts and never touches the human
column).

Default is a spend-free dry run reporting what would clear. Pass ``--apply`` to
write; the current files are first copied to timestamped ``.bkup-*`` siblings —
nothing is ever deleted.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from packs.ingestion.primitives.deep_context.common import LINKEDIN_OVERRIDES_CSV
from packs.ingestion.primitives.deep_context.review_store import (
    load_override_rows,
    write_override_rows,
)

SYNTHETIC_PEOPLE_CSV = LINKEDIN_OVERRIDES_CSV.parent / "synthetic-people.csv"
HUMAN_WORTH_VALUES = {"yes", "no", "maybe"}


def _backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.bkup-{stamp}")
    shutil.copy2(path, target)
    return target


def clear_human_worth(rows: dict[str, dict[str, str]]) -> int:
    """Blank every human network_worth mark in place; returns how many cleared."""
    cleared = 0
    for row in rows.values():
        if (row.get("network_worth") or "").strip().lower() in HUMAN_WORTH_VALUES:
            row["network_worth"] = ""
            cleared += 1
    return cleared


def clear_synthetic_approvals(path: Path, *, apply: bool) -> dict[str, int | str]:
    """Reset synthetic-people.csv ``approved`` to pending. Returns counts."""
    if not path.exists():
        return {"rows": 0, "cleared": 0, "status": "missing"}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    cleared = sum(1 for row in rows if (row.get("approved") or "").strip())
    if apply and cleared:
        _backup(path)
        for row in rows:
            row["approved"] = ""
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    return {"rows": len(rows), "cleared": cleared, "status": "ok"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clear HUMAN review decisions; keep all machine (LLM) work")
    parser.add_argument("--review", type=Path, default=LINKEDIN_OVERRIDES_CSV)
    parser.add_argument("--synthetic-people", type=Path, default=SYNTHETIC_PEOPLE_CSV)
    parser.add_argument("--apply", action="store_true",
                        help="write the cleared files (default is a dry-run "
                             "report); current files are copied to .bkup-* first")
    args = parser.parse_args()

    payload: dict[str, object] = {"primitive": "restart_review",
                                  "review": str(args.review),
                                  "synthetic_people": str(args.synthetic_people)}
    rows = load_override_rows(args.review)
    would_clear = clear_human_worth(rows)
    payload["review_rows"] = len(rows)
    payload["human_worth_cleared"] = would_clear

    if args.apply:
        if args.review.exists() and would_clear:
            payload["review_backup"] = str(_backup(args.review))
            write_override_rows(args.review, rows)
        payload["synthetic"] = clear_synthetic_approvals(
            args.synthetic_people, apply=True)
        payload["status"] = "applied"
        payload["next"] = ("rerun `bin/deep-context review --fresh` — the queue "
                           "re-opens with the machine verdicts intact")
    else:
        payload["synthetic"] = clear_synthetic_approvals(
            args.synthetic_people, apply=False)
        payload["status"] = "dry_run"
        payload["next"] = "pass --apply to clear (files are backed up first)"

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
