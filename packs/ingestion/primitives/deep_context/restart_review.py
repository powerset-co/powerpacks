"""Restart the human review while preserving every machine (LLM) verdict.

``bin/deep-context restart`` clears the HUMAN-owned decisions so the FULL
staged journey (People review -> Enrich -> Check LinkedIn) can be taken from
the top without re-spending anything:

- review.csv ``network_worth`` cells are blanked (the sticky human worth marks);
- review.csv HUMAN identity decisions are blanked — rows a person clicked in
  Check LinkedIn (``approved`` yes/no, plus their ``action`` and any pasted
  ``new_linkedin_url``). Machine auto-verify/auto-detach rows
  (``approved=auto``) are LLM work and stay applied, exactly as a brand-new
  user would see them;
- synthetic-people.csv ``approved`` cells are reset to pending (they mirror
  human worth clicks and are re-derived / re-clicked for free).

The restart also resets the review manifest's completed-stage ladder (stage
back to ``worth``, nothing completed — which also clears the Enrich Continue
handoff). The ladder ONLY ever moves forward during a flow, so this explicit
restart is the single sanctioned backward move and owns the reset. The
enrichment selection sha then mismatches, so the flow re-previews and re-runs
FROM CACHE (paid Parallel results are
reused). Machine columns (``llm_worth`` / reject columns), facts, dossiers,
verdicts, deep-research artifacts, and profile caches are untouched — the
machine's work survives; only the human's answers are cleared. This is the
complement of ``rejudge`` (which refreshes the MACHINE's verdicts and never
touches the human columns).

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

from packs.ingestion.primitives.deep_context.common import (
    LINKEDIN_OVERRIDES_CSV,
    REVIEW_MANIFEST,
)
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


def clear_human_identity_decisions(rows: dict[str, dict[str, str]]) -> int:
    """Blank every HUMAN Check-LinkedIn decision in place; returns how many.

    Human clicks write ``approved`` yes/no (keep / detach / fix / an approved
    exclude); the fix form additionally stores a pasted ``new_linkedin_url``.
    Machine auto-verify/auto-detach writes ``approved=auto`` — that is LLM work
    and is preserved so the re-run starts exactly where a new user would."""
    cleared = 0
    for row in rows.values():
        approved = (row.get("approved") or "").strip().lower()
        pasted_url = (row.get("new_linkedin_url") or "").strip()
        human = approved in {"yes", "no"} or (pasted_url and approved != "auto")
        if human:
            row["action"] = ""
            row["approved"] = ""
            row["new_linkedin_url"] = ""
            cleared += 1
    return cleared


def reset_review_manifest(path: Path, *, apply: bool) -> dict[str, str]:
    """Reset the completed-stage ladder: stage back to worth, nothing completed.

    During a flow the ladder only moves forward (awaiting_user writes never
    demote it), so the explicit restart is the ONLY place it rewinds. Clearing
    ``completed_stages`` also clears the Enrich Continue handoff ("enrich" is
    recorded there)."""
    if not path.exists():
        return {"status": "missing"}
    if not apply:
        return {"status": "would_reset"}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        manifest = {}
    manifest.update({"stage": "worth", "status": "awaiting_user",
                     "completed_stages": []})
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"status": "reset"}


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
    parser.add_argument("--manifest", type=Path, default=REVIEW_MANIFEST)
    parser.add_argument("--apply", action="store_true",
                        help="write the cleared files (default is a dry-run "
                             "report); current files are copied to .bkup-* first")
    args = parser.parse_args()

    payload: dict[str, object] = {"primitive": "restart_review",
                                  "review": str(args.review),
                                  "synthetic_people": str(args.synthetic_people)}
    rows = load_override_rows(args.review)
    would_clear = clear_human_worth(rows)
    identity_cleared = clear_human_identity_decisions(rows)
    payload["review_rows"] = len(rows)
    payload["human_worth_cleared"] = would_clear
    payload["human_identity_cleared"] = identity_cleared

    if args.apply:
        if args.review.exists() and (would_clear or identity_cleared):
            payload["review_backup"] = str(_backup(args.review))
            write_override_rows(args.review, rows)
        payload["synthetic"] = clear_synthetic_approvals(
            args.synthetic_people, apply=True)
        payload["manifest"] = reset_review_manifest(args.manifest, apply=True)
        payload["status"] = "applied"
        payload["next"] = ("rerun `bin/deep-context review --fresh` — the queue "
                           "re-opens with the machine verdicts intact")
    else:
        payload["synthetic"] = clear_synthetic_approvals(
            args.synthetic_people, apply=False)
        payload["manifest"] = reset_review_manifest(args.manifest, apply=False)
        payload["status"] = "dry_run"
        payload["next"] = "pass --apply to clear (files are backed up first)"

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
