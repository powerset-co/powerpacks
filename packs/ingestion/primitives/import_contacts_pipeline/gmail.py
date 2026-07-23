#!/usr/bin/env python3
"""Import discovered Gmail contacts (directory-only — the only mode).

Free and local: apply the shared identity directory to the discovered Gmail
queues, materialize `import/gmail/people.csv`, and write the still-unresolved
contacts to `import/gmail/candidates.csv` for the deep-context processing
layer, which owns ALL resolution and enrichment: stored legacy resolutions
migrate into overrides/review.csv via `bin/deep-context migrate-legacy` (the
central source of truth the fan-in and the review flow read); new lookups run
through deep-context's judged, budget-gated stages.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Make `packs.*` importable whether this file runs as a module or as a script
# (uv run .../gmail.py). One upfront path bootstrap replaces the old duplicated
# try/except import block.
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.schemas.candidates_schema import (  # noqa: E402
    CANDIDATES_SCHEMA_COLUMNS,
    candidate_key_for,
    normalize_candidate_row,
)
from packs.ingestion.primitives.discover_contacts_pipeline.common import (  # noqa: E402
    DEFAULT_BASE_DIR,
    DEFAULT_DIRECTORY_CSV,
    emit,
    read_csv_rows,
    read_accounts,
    read_json,
    source_slug,
    write_csv_rows,
    write_json,
)
from packs.ingestion.primitives.import_contacts_pipeline.common import (  # noqa: E402
    DEFAULT_ACCOUNTS,
    DEFAULT_IMPORT_DIR,
    DEFAULT_PROFILE_CACHE_DIR,
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

GMAIL_IMPORT_CONTRACT = "gmail-directory-only-v2"


def _child_artifacts(child: dict[str, Any]) -> dict[str, Any]:
    """Flatten one discovery-manifest child into a single artifacts dict.

    Precedence (last wins): payload.artifacts < child.artifacts < the two
    top-level convenience keys (people_csv / linkedin_resolution_queue_csv)."""
    artifacts: dict[str, Any] = {}
    payload = child.get("payload") if isinstance(child.get("payload"), dict) else {}
    payload_artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    artifacts.update(payload_artifacts)
    direct_artifacts = child.get("artifacts") if isinstance(child.get("artifacts"), dict) else {}
    artifacts.update(direct_artifacts)
    if child.get("people_csv"):
        artifacts["people_csv"] = child.get("people_csv")
    if child.get("linkedin_resolution_queue_csv"):
        artifacts["linkedin_resolution_queue_csv"] = child.get("linkedin_resolution_queue_csv")
    return artifacts


def _valid_gmail_people_csv(path_text: Any) -> bool:
    """True when the path is a readable people CSV in the gmail-discovery
    schema (has `primary_email` + `interaction_counts` columns)."""
    path = Path(str(path_text or ""))
    if not path.exists() or not path.is_file():
        return False
    try:
        fields, _rows = read_csv_rows(path)
    except OSError:
        return False
    return "primary_email" in fields and "interaction_counts" in fields


def gmail_artifacts_from_discovery() -> dict[str, Any]:
    """Collect the import's inputs from the gmail DISCOVERY manifest.

    Reads only `.powerpacks/network-import/discover/gmail/manifest.json` (plus
    existence checks on the files it names) and returns per-account queue and
    people records; children with an invalid/missing people CSV are reported
    under `gmail_invalid_discovery_records` instead of being silently dropped."""
    manifest = read_json(DEFAULT_BASE_DIR / "discover" / "gmail" / "manifest.json", {}) or {}
    artifacts: dict[str, Any] = {}
    contacts_csv = str(manifest.get("contacts_csv") or DEFAULT_BASE_DIR / "discover" / "gmail" / "contacts.csv")
    stable_queue_csv = str(manifest.get("linkedin_resolution_queue_csv") or DEFAULT_BASE_DIR / "discover" / "gmail" / "linkedin_resolution_queue.csv")
    if Path(contacts_csv).exists():
        artifacts["gmail_contacts_csv"] = contacts_csv
    if Path(stable_queue_csv).exists():
        artifacts["gmail_linkedin_resolution_queue_csv"] = stable_queue_csv
    queue_records: list[dict[str, Any]] = []
    people_records: list[dict[str, Any]] = []
    invalid_records: list[dict[str, Any]] = []
    for child in manifest.get("children") or []:
        if not isinstance(child, dict):
            continue
        account_email = str(child.get("account_email") or "")
        child_artifacts = _child_artifacts(child)
        queue_csv = child_artifacts.get("linkedin_resolution_queue_csv")
        people_csv = child_artifacts.get("people_csv")
        slug = source_slug(account_email or "gmail")
        valid_people = _valid_gmail_people_csv(people_csv)
        if valid_people:
            people_records.append({"account_email": account_email, "people_csv": people_csv, "slug": slug})
        elif people_csv:
            invalid_records.append({
                "account_email": account_email,
                "people_csv": people_csv,
                "queue_csv": queue_csv or "",
                "reason": "missing_people_schema_or_interaction_counts",
            })
        if queue_csv and Path(str(queue_csv)).exists() and valid_people:
            queue_records.append({
                "account_email": account_email,
                "queue_csv": queue_csv,
                "people_csv": people_csv,
                "slug": slug,
            })
    if queue_records:
        artifacts["gmail_linkedin_resolution_queue_csvs"] = queue_records
    if people_records:
        artifacts["gmail_people_records"] = people_records
    if invalid_records:
        artifacts["gmail_invalid_discovery_records"] = invalid_records
    return artifacts


def queue_row_to_candidate(row: dict[str, str], *, cached_negative: bool) -> dict[str, str] | None:
    """Map one unresolved queue row to a candidates-schema row (None = no
    usable email). `cached_negative` marks contacts a prior resolution already
    answered "no LinkedIn found" for, so deep-context can deprioritize them."""
    primary_email = (row.get("primary_email") or "").strip().lower()
    if not primary_email or "@" not in primary_email:
        return None
    total_messages = 0
    try:
        total_messages = int(float(row.get("total_messages") or 0))
    except (TypeError, ValueError):
        total_messages = 0
    evidence: dict[str, Any] = {
        "handle": (row.get("handle") or "").strip(),
        "account_emails": (row.get("account_emails") or "").strip(),
        "primary_email_type": (row.get("primary_email_type") or "").strip(),
        "thread_count": (row.get("thread_count") or "").strip(),
        "cached_negative": cached_negative,
    }
    candidate = {
        "candidate_key": candidate_key_for(primary_email, ""),
        "source": "gmail",
        "full_name": (row.get("full_name") or row.get("display_name") or "").strip(),
        "primary_email": primary_email,
        "all_emails": json.dumps([primary_email], ensure_ascii=False),
        "company_guess": (row.get("company_guess") or "").strip(),
        "interaction_counts": (
            json.dumps({"gmail": total_messages}, ensure_ascii=False) if total_messages else ""
        ),
        "last_interaction": (row.get("last_interaction") or "").strip(),
        "evidence": evidence,
    }
    return normalize_candidate_row(candidate)


def write_gmail_candidates(artifacts: dict[str, Any], import_dir: Path) -> dict[str, Any]:
    """Union the post-directory unresolved (+ cached-negative, flagged) queues
    into import/gmail/candidates.csv for the deep-context processing layer."""
    candidates_csv = import_dir / "candidates.csv"
    by_key: dict[str, dict[str, str]] = {}
    skipped = {"no_email": 0, "duplicate_email": 0}
    groups = (
        (artifacts.get("gmail_unresolved_linkedin_resolution_queue_csvs") or [], False),
        (artifacts.get("gmail_cached_negative_linkedin_resolution_queue_csvs") or [], True),
    )
    for records, cached_negative in groups:
        for record in records:
            if not isinstance(record, dict) or not record.get("queue_csv"):
                continue
            queue_path = Path(str(record["queue_csv"]))
            if not queue_path.exists():
                continue
            for row in read_csv_rows(queue_path)[1]:
                candidate = queue_row_to_candidate(row, cached_negative=cached_negative)
                if candidate is None:
                    skipped["no_email"] += 1
                    continue
                key = candidate.get("candidate_key", "")
                if not key:
                    skipped["no_email"] += 1
                    continue
                if key in by_key:
                    skipped["duplicate_email"] += 1
                    continue
                by_key[key] = candidate
    rows = [by_key[key] for key in sorted(by_key)]
    write_csv_rows(candidates_csv, CANDIDATES_SCHEMA_COLUMNS, rows)
    return {
        "candidates_csv": str(candidates_csv),
        "candidates": len(rows),
        "skipped": skipped,
    }


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
    import_dir = DEFAULT_IMPORT_DIR / "gmail"
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
    """Exit 0 on success/skip, 1 on failure. (Exit 20 / blocked_approval is
    gone with the spend paths — nothing in this import can block on approval.)"""
    args = build_parser().parse_args()
    payload = run(args)
    emit(payload)
    return 1 if payload.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
