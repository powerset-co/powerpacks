"""Gmail import — directory-apply step and its pure directory-commit transforms.

`run_gmail_directory(imp, accounts)` is the first import step: it commits the
Gmail observations into the shared `directory.csv`, then splits every
per-account LinkedIn queue against it and RETURNS one `QueueSplit` per account.
The orchestrator (`importer.py`) owns the run loop and manifest; nothing here
mutates shared state.

The split policy itself is `classify_queue_row` — first rule wins, the whole
decision on one screen. The module-level functions are pure transforms:
queue↔directory row builders (`directory_rows_from_gmail_queue`,
`split_queue`), the two `commit_*_to_directory` writers, and the per-account
resolution combiner (`combine_gmail_resolution_records`). Everything
cross-source — the whole `directory.csv` contract, the resolution normalizers,
the people.csv materializers — is imported from `imports/directory.py`.

Changelog:
  2026-07-28 (typed steps): records became `GmailAccount` / `QueueSplit` /
    `ResolutionRecord` dataclasses; the step returns its splits instead of
    stuffing parallel lists into `imp.state["artifacts"]`. Deleted with the
    blob: `ordered_records` (its account-order argument had no writer since
    `accounts.json` died), `gmail_queue_records`' stage-level fallback (the
    orchestrator's gate already returns `skipped` in that exact case), and the
    in-step empty-queue skip branch (same gate). Split semantics, output
    filenames, and directory commits are byte-identical.
  2026-07-23 (steps split): extracted from the file-loaded gmail/import_steps.py
    (deleted). No behavior change: fixed output paths, split semantics, and
    manifest payloads are unchanged.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Repo-root bootstrap so `packs.*` imports resolve however this module is loaded.
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.contact_fields import (  # noqa: E402
    is_generic_or_non_person,
    is_likely_person_name,
)
from packs.ingestion.primitives.common.jsonio import now_iso, unique_strings  # noqa: E402
from packs.ingestion.primitives.discover.common import read_csv_rows  # noqa: E402
from packs.ingestion.primitives.imports.directory import (  # noqa: E402
    RESOLUTION_NEGATIVE_STATUSES,
    build_directory_checkpoint,
    commit_directory_rows,
    directory_match_for_queue_row,
    directory_row_is_found,
    directory_row_is_prior_negative,
    gmail_directory_source_key,
    load_directory_lookup,
    merge_resolution_rows,
    normalize_resolution_row,
    normalized_directory_row,
    parse_confidence,
    resolution_from_directory_match,
)
from packs.ingestion.primitives.common.proc import emit_progress  # noqa: E402
from packs.ingestion.primitives.imports.gmail.util import (  # noqa: E402
    GMAIL_IMPORT_PREFIX,
    GmailAccount,
)
from packs.ingestion.schemas.gmail_artifacts import LINKEDIN_RESOLUTION_COLUMNS  # noqa: E402
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402

if TYPE_CHECKING:
    from packs.ingestion.primitives.imports.gmail.importer import GmailImport


@dataclass(frozen=True)
class QueueSplit:
    """One account's queue split against the directory: the three output CSVs
    and their counts. `to_result()` renders the manifest's per-slug block."""

    account: GmailAccount
    directory_csv: str
    resolutions_csv: str
    unresolved_csv: str
    cached_negative_csv: str
    input_rows: int
    resolved: int
    unresolved: int
    cached_negative: int
    filtered_non_person: int

    def to_result(self) -> dict[str, Any]:
        return {
            "account_email": self.account.email,
            "queue_csv": self.account.queue_csv,
            "people_csv": self.account.people_csv,
            "slug": self.account.slug,
            "directory_csv": self.directory_csv,
            "directory_resolutions_csv": self.resolutions_csv,
            "unresolved_queue_csv": self.unresolved_csv,
            "cached_negative_queue_csv": self.cached_negative_csv,
            "input_rows": self.input_rows,
            "resolved": self.resolved,
            "unresolved": self.unresolved,
            "cached_negative": self.cached_negative,
            "filtered_non_person": self.filtered_non_person,
        }


@dataclass(frozen=True)
class ResolutionRecord:
    """One resolutions CSV to apply to one account's people.csv."""

    account_email: str
    slug: str
    people_csv: str
    resolutions_csv: str
    source: str = "directory"
    resolved: int = 0
    resolution_sources: tuple[str, ...] = ()


def _is_resolvable_person(row: dict[str, str]) -> bool:
    """Return True if the queue row looks like a real person worth resolving."""
    email = (row.get("primary_email") or row.get("email") or row.get("handle") or "").strip()
    name = (row.get("display_name") or row.get("full_name") or "").strip()
    if not email or not name:
        return False
    return not is_generic_or_non_person(email) and is_likely_person_name(name)


def classify_queue_row(
    row: dict[str, str], lookup: dict[str, dict[str, dict[str, str]]],
) -> tuple[str, dict[str, str] | None]:
    """First rule wins — the whole split policy on one screen.

    Returns (label, directory match). The match is non-None only for
    "resolved", where the caller turns it into a resolution row."""
    match = directory_match_for_queue_row(row, lookup)
    if match and directory_row_is_found(match):
        return "resolved", match
    if match and directory_row_is_prior_negative(match):
        return "cached_negative", None
    if not _is_resolvable_person(row):
        return "filtered", None
    return "unresolved", None


def split_queue(account: GmailAccount, directory_csv: Path, output_dir: Path) -> QueueSplit:
    """Split one account's LinkedIn queue against directory.csv into
    resolved / cached-negative / unresolved CSVs."""
    fields, rows = read_csv_rows(Path(account.queue_csv))
    lookup = load_directory_lookup(directory_csv)
    resolved: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []
    cached_negative: list[dict[str, str]] = []
    filtered_non_person = 0
    for row in rows:
        label, match = classify_queue_row(row, lookup)
        if label == "resolved":
            resolved.append(resolution_from_directory_match(row, match))
        elif label == "cached_negative":
            cached_negative.append(row)
        elif label == "filtered":
            filtered_non_person += 1
        else:
            unresolved.append(row)
    output_dir.mkdir(parents=True, exist_ok=True)
    resolutions_csv = output_dir / "directory_linkedin_resolutions.csv"
    unresolved_csv = output_dir / "unresolved_linkedin_resolution_queue.csv"
    cached_negative_csv = output_dir / "cached_negative_linkedin_resolution_queue.csv"
    CsvIO.write_dict_rows(resolutions_csv, LINKEDIN_RESOLUTION_COLUMNS, resolved)
    CsvIO.write_dict_rows(unresolved_csv, fields, unresolved)
    CsvIO.write_dict_rows(cached_negative_csv, fields, cached_negative)
    return QueueSplit(
        account=account,
        directory_csv=str(directory_csv),
        resolutions_csv=str(resolutions_csv),
        unresolved_csv=str(unresolved_csv),
        cached_negative_csv=str(cached_negative_csv),
        input_rows=len(rows),
        resolved=len(resolved),
        unresolved=len(unresolved),
        cached_negative=len(cached_negative),
        filtered_non_person=filtered_non_person,
    )


def parse_json_list(value: Any) -> list[str]:
    """Parse a JSON array or bare value into a de-duplicated string list."""
    if isinstance(value, list):
        return unique_strings(value)
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = []
    if isinstance(parsed, list):
        return unique_strings(parsed)
    return []


def directory_rows_from_gmail_queue(account: GmailAccount) -> list[dict[str, str]]:
    """Directory 'observed' rows for every account/email in one account's queue."""
    queue_csv = Path(account.queue_csv)
    if not queue_csv.exists():
        return []
    account_email = account.email.strip().lower()
    _fields, rows = read_csv_rows(queue_csv)
    output: list[dict[str, str]] = []
    for row in rows:
        email = str(row.get("primary_email") or row.get("handle") or "").strip().lower()
        if not email:
            continue
        accounts = parse_json_list(row.get("account_emails")) or unique_strings(account_email)
        source_ids = parse_json_list(row.get("source_ids"))
        if not accounts:
            accounts = [account_email or ""]
        if not source_ids:
            source_ids = [""]
        for account_entry in accounts:
            output.append(normalized_directory_row({
                "source": "gmail_msgvault",
                "source_key": gmail_directory_source_key(account_entry, email, row.get("id") or ""),
                "source_account": account_entry,
                "source_id": json.dumps(source_ids, ensure_ascii=False),
                "source_channels": row.get("source_channels") or "gmail_msgvault",
                "status": "observed",
                "email": email,
                "name": row.get("display_name") or row.get("full_name") or "",
                "confidence": "0",
                "evidence": json.dumps({
                    "source": "gmail_msgvault",
                    "queue_csv": str(queue_csv),
                    "account_email": account_entry,
                    "source_ids": source_ids,
                    "total_messages": row.get("total_messages", ""),
                    "thread_count": row.get("thread_count", ""),
                    "last_interaction": row.get("last_interaction", ""),
                }, sort_keys=True),
                "reasoning": "Observed in local Gmail metadata",
            }, source_artifact=str(queue_csv), updated_at=now_iso()))
    return [row for row in output if row]


def commit_gmail_observations_to_directory(
    directory_csv: Path, accounts: list[GmailAccount],
) -> dict[str, Any]:
    """Merge every Gmail queue's observed rows into directory.csv."""
    rows: list[dict[str, str]] = []
    for account in accounts:
        rows.extend(directory_rows_from_gmail_queue(account))
    result = commit_directory_rows(directory_csv, rows)
    result["gmail_observation_rows"] = len(rows)
    return result


def commit_gmail_resolutions_to_directory(
    directory_csv: Path, records: list[ResolutionRecord],
) -> dict[str, Any]:
    """Merge stored per-account Gmail LinkedIn resolutions (found + negative) into directory.csv."""
    rows: list[dict[str, str]] = []
    for record in records:
        account_email = record.account_email.strip().lower()
        resolution_path = Path(record.resolutions_csv)
        if not record.resolutions_csv or not resolution_path.exists():
            continue
        for raw_resolution in read_csv_rows(resolution_path)[1]:
            resolution = normalize_resolution_row(raw_resolution)
            email = str(resolution.get("handle") or "").strip().lower()
            if "@" not in email:
                continue
            linkedin_url = normalize_linkedin_url(resolution.get("linkedin_url") or "")
            public_identifier = extract_public_identifier(linkedin_url)
            status = str(resolution.get("status") or "").strip().lower()
            confidence = parse_confidence(resolution.get("confidence"), 0.0)
            if status == "found":
                if not public_identifier or confidence < 0.75:
                    continue
            elif status in (RESOLUTION_NEGATIVE_STATUSES | {"not_found"}):
                status = "not_found" if status not in {"failed", "error"} else status
                linkedin_url = ""
                confidence = max(confidence, 0.01)
            else:
                continue
            evidence = {
                "source": "gmail_linkedin_resolution",
                "account_email": account_email,
                "resolutions_csv": record.resolutions_csv,
                "resolution_evidence": resolution.get("evidence", ""),
            }
            rows.append(normalized_directory_row({
                "source": "gmail_msgvault",
                "source_key": gmail_directory_source_key(account_email, email),
                "source_account": account_email,
                "source_channels": "gmail_msgvault",
                "status": status,
                "email": email,
                "name": resolution.get("matched_name") or "",
                "linkedin_url": linkedin_url,
                "confidence": f"{confidence:.2f}",
                "matched_name": resolution.get("matched_name") or "",
                "matched_headline": resolution.get("matched_headline") or "",
                "evidence": json.dumps(evidence, sort_keys=True),
                "reasoning": resolution.get("reasoning") or "",
                "_priority": 82,
            }, source_artifact=record.resolutions_csv, updated_at=now_iso()))
    result = commit_directory_rows(directory_csv, rows)
    result["gmail_resolution_rows"] = len(rows)
    result["gmail_resolution_found_rows"] = sum(1 for row in rows if row.get("status") == "found")
    result["gmail_resolution_negative_rows"] = sum(1 for row in rows if row.get("status") in (RESOLUTION_NEGATIVE_STATUSES | {"not_found"}))
    return result


def combine_gmail_resolution_records(
    records: list[ResolutionRecord], run_dir: Path,
) -> list[ResolutionRecord]:
    """Group per-account resolution CSVs by (slug, people_csv) and merge each into one file."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if not record.people_csv or not record.resolutions_csv:
            continue
        key = (record.slug, record.people_csv)
        group = grouped.setdefault(key, {"account_email": record.account_email, "resolution_paths": []})
        group["resolution_paths"].append(Path(record.resolutions_csv))
    combined: list[ResolutionRecord] = []
    for (slug, people_csv), group in sorted(grouped.items(), key=lambda item: item[0]):
        rows = merge_resolution_rows(group["resolution_paths"])
        if not rows:
            continue
        out_dir = run_dir / f"gmail-combined-resolutions-{slug}"
        out_path = out_dir / "linkedin_resolutions.csv"
        CsvIO.write_dict_rows(out_path, LINKEDIN_RESOLUTION_COLUMNS, rows)
        combined.append(ResolutionRecord(
            account_email=group["account_email"],
            slug=slug,
            people_csv=people_csv,
            resolutions_csv=str(out_path),
            source="combined",
            resolved=len(rows),
            resolution_sources=tuple(str(path) for path in group["resolution_paths"]),
        ))
    return combined


def run_gmail_directory(imp: "GmailImport", accounts: list[GmailAccount]) -> list[QueueSplit]:
    """Apply directory.csv LinkedIn mappings to every Gmail account queue.

    Commits the Gmail observations into `directory.csv`, refreshes the
    directory checkpoint, splits each per-account queue, and returns one
    `QueueSplit` per account. The orchestrator gate guarantees `accounts` is
    non-empty."""
    imp._begin_step("gmail_directory", f"Applying directory LinkedIn mappings to {len(accounts)} Gmail queue(s).")
    observation_checkpoint = commit_gmail_observations_to_directory(imp.directory_csv, accounts)
    checkpoint = build_directory_checkpoint(
        {"linkedin_directory_csv": str(imp.directory_csv)}, {},
    )
    directory_csv = Path(checkpoint["directory_csv"])
    splits = [
        split_queue(account, directory_csv, imp.import_dir / f"gmail-directory-{account.slug}")
        for account in accounts
    ]
    total_resolved = sum(split.resolved for split in splits)
    total_unresolved = sum(split.unresolved for split in splits)
    total_cached_negative = sum(split.cached_negative for split in splits)
    imp._mark_step(
        "gmail_directory", "completed",
        checkpoint=checkpoint,
        observation_checkpoint=observation_checkpoint,
        resolved=total_resolved,
        unresolved=total_unresolved,
        cached_negative=total_cached_negative,
        payload={"results": [split.to_result() for split in splits]},
    )
    if total_cached_negative:
        emit_progress(f"Gmail directory mappings applied: {total_resolved} resolved, {total_cached_negative} already attempted, {total_unresolved} unresolved.", GMAIL_IMPORT_PREFIX)
    else:
        emit_progress(f"Gmail directory mappings applied: {total_resolved} resolved, {total_unresolved} unresolved.", GMAIL_IMPORT_PREFIX)
    return splits
