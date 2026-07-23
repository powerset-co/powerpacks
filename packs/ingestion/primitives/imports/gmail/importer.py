#!/usr/bin/env python3
"""Import discovered Gmail contacts (directory-only — the only mode).

Free and local: apply the shared identity directory to the discovered Gmail
queues, materialize `import/gmail/people.csv`, and write the still-unresolved
contacts to `import/gmail/candidates.csv` for the deep-context processing
layer, which owns ALL resolution and enrichment: stored legacy resolutions
migrate into overrides/review.csv via `bin/deep-context migrate-legacy` (the
central source of truth the fan-in and the review flow read); new lookups run
through deep-context's judged, budget-gated stages.

What it does (run()):
  1. No-op if the import manifest is current (unless --force); read accounts;
     collect the discovery queues into a GmailImportLedger. No queue -> skipped.
  2. Dispatch two ledger-backed steps via load_gmail_import_steps:
       - run_gmail_directory: apply the shared directory.csv to each account's
         resolution queue -> resolved / unresolved / cached-negative lanes, and
         commit the Gmail observations + directory resolutions into directory.csv.
       - run_gmail_apply_and_enrich: attach STORED resolutions only (directory +
         explicit; no Parallel/RapidAPI) onto each account people.csv via
         discover_engine apply-resolutions, then materialize one merged Gmail
         people.csv.
  3. Split the population: copy_people_csv -> import/gmail/people.csv (matched
     people), write_gmail_candidates -> import/gmail/candidates.csv (the
     still-unresolved research pool). Directory source-account quality gate ->
     typed manifest.
  Exit 0 completed/skipped, 1 failed. No approval gate: nothing here spends.

Changelog:
  2026-07-23 (audit):
    - One upfront repo-root path bootstrap replaced the duplicated try/except
      import block.
    - Exit 20 / blocked_approval removed with the spend paths; nothing in this
      import can block on approval.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so packs.* imports work in module AND script mode
# (uv run .../importer.py); must be in-file because script-mode never imports
# the package __init__.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.schemas.candidates_schema import (  # noqa: E402
    CANDIDATES_SCHEMA_COLUMNS,
    candidate_key_for,
    normalize_candidate_row,
)
from packs.ingestion.primitives.common.jsonio import emit, write_json  # noqa: E402
from packs.ingestion.primitives.common.paths import (  # noqa: E402
    DEFAULT_ACCOUNTS,
    DEFAULT_BASE_DIR,
    DEFAULT_DIRECTORY_CSV,
    DEFAULT_IMPORT_DIR,
    DEFAULT_PROFILE_CACHE_DIR,
    source_import_dir,
)
from packs.ingestion.primitives.discover.common import read_accounts  # noqa: E402
from packs.ingestion.primitives.imports.common import (  # noqa: E402
    GmailImportLedger,
    copy_people_csv,
    csv_count,
    directory_source_account_quality,
    import_manifest_current,
    linked_gmail_accounts,
    load_gmail_import_steps,
    normalize_directory_source_accounts,
    write_manifest,
)

from packs.ingestion.primitives.imports.gmail.util import (  # noqa: E402
    gmail_artifacts_from_discovery,
    write_gmail_candidates,
)

GMAIL_IMPORT_CONTRACT = "gmail-directory-only-v2"


def run(args: argparse.Namespace) -> dict[str, Any]:
    """The whole import: fingerprint no-op check -> ledger -> the two step
    functions (directory match, then apply + people materialization) ->
    candidates + directory quality checks -> the import manifest.

    A step failure ends the run with status `failed` (steps report their own
    error in the ledger); there is no approval gate — nothing here spends."""
    expected_input = {
        "pipeline_contract": GMAIL_IMPORT_CONTRACT,
        "mode": "directory-only",
    }
    current = import_manifest_current("gmail", expected_input, import_dir=DEFAULT_IMPORT_DIR)
    if current and not getattr(args, "force", False):
        return current
    accounts = read_accounts(args.accounts)
    steps_mod = load_gmail_import_steps()
    import_dir = source_import_dir("gmail")
    ledger_path = import_dir / "ledger.json"
    emails = linked_gmail_accounts(accounts)
    ledger = GmailImportLedger(
        artifact_dir=str(import_dir),
        input={
            "operator_id": args.operator_id,
            "from_accounts": str(args.accounts),
            "gmail_account_emails": emails,
            # Directory-only, always: this import applies the directory and any
            # STORED resolutions; resolution + enrichment live in deep-context
            # (migrate-legacy for the stored era, judged lookups for new people).
            "linkedin_directory_csv": str(DEFAULT_DIRECTORY_CSV),
            "profile_cache_dir": str(DEFAULT_PROFILE_CACHE_DIR),
        },
        artifacts=gmail_artifacts_from_discovery(),
    ).to_dict()
    if not ledger["artifacts"].get("gmail_linkedin_resolution_queue_csvs"):
        reason = "no Gmail discovery queue"
        status = "skipped"
        if ledger["artifacts"].get("gmail_linkedin_resolution_queue_csv") or ledger["artifacts"].get("gmail_invalid_discovery_records"):
            reason = "gmail_discovery_missing_per_account_people_csv"
        return write_manifest("gmail", {
            "status": status,
            "reason": reason,
            "artifact_dir": str(import_dir),
            "artifacts": ledger.get("artifacts", {}),
        }, import_dir=DEFAULT_IMPORT_DIR)
    write_json(ledger_path, ledger)
    for func_name in ("run_gmail_directory", "run_gmail_apply_and_enrich"):
        ok = getattr(steps_mod, func_name)(ledger_path, ledger)
        if not ok:
            return write_manifest("gmail", {
                "status": "failed",
                "ledger": str(ledger_path),
                "artifact_dir": str(import_dir),
                "steps": ledger.get("steps", {}),
                "artifacts": ledger.get("artifacts", {}),
            }, import_dir=DEFAULT_IMPORT_DIR)
        steps_mod.save_ledger(ledger_path, ledger)
    ledger["status"] = "completed"
    steps_mod.save_ledger(ledger_path, ledger)
    people_csv = copy_people_csv("gmail", str(ledger.get("artifacts", {}).get("gmail_merged_people_csv") or ledger.get("artifacts", {}).get("gmail_people_csv") or ""), import_dir=DEFAULT_IMPORT_DIR)
    candidates = write_gmail_candidates(ledger.get("artifacts", {}), import_dir)
    directory_normalization = normalize_directory_source_accounts("gmail")
    directory_quality = directory_source_account_quality("gmail")
    if directory_quality["status"] != "ok":
        return write_manifest("gmail", {
            "status": "failed",
            "reason": "directory_source_account_quality_failed",
            "ledger": str(ledger_path),
            "artifact_dir": str(import_dir),
            "outputs": {
                "people_csv": people_csv,
                "directory_csv": str(DEFAULT_DIRECTORY_CSV),
            },
            "directory_normalization": directory_normalization,
            "directory_quality": directory_quality,
            "steps": ledger.get("steps", {}),
            "artifacts": ledger.get("artifacts", {}),
        }, import_dir=DEFAULT_IMPORT_DIR)
    return write_manifest("gmail", {
        "status": "completed",
        "ledger": str(ledger_path),
        "artifact_dir": str(import_dir),
        "input": {
            **expected_input,
            "discovery_manifest": str(DEFAULT_BASE_DIR / "discover" / "gmail" / "manifest.json"),
            "contacts_csv": str(DEFAULT_BASE_DIR / "discover" / "gmail" / "contacts.csv"),
            "linkedin_resolution_queue_csv": str(DEFAULT_BASE_DIR / "discover" / "gmail" / "linkedin_resolution_queue.csv"),
        },
        "outputs": {
            "people_csv": people_csv,
            "candidates_csv": candidates["candidates_csv"],
            "directory_csv": str(DEFAULT_DIRECTORY_CSV),
        },
        "stats": {
            "people": csv_count(people_csv),
            "candidates": candidates["candidates"],
        },
        "candidates": candidates,
        "steps": ledger.get("steps", {}),
        "directory_normalization": directory_normalization,
        "directory_quality": directory_quality,
        "artifacts": ledger.get("artifacts", {}),
    }, import_dir=DEFAULT_IMPORT_DIR)


def build_parser() -> argparse.ArgumentParser:
    """CLI: one `run` command; `--force` bypasses the manifest no-op skip."""
    parser = argparse.ArgumentParser(description="Import discovered Gmail contacts (directory-only)")
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument("--operator-id", default="local")
    parser.add_argument("--force", action="store_true", help="Re-run even if the import manifest is current (no no-op skip)")
    return parser


def main() -> int:
    """Exit 0 on success/skip, 1 on failure."""
    args = build_parser().parse_args()
    payload = run(args)
    emit(payload)
    return 1 if payload.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
