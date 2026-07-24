"""Gmail import — directory-apply step and its pure directory-commit transforms.

`run_gmail_directory(imp)` is the first import step: for every per-account Gmail
LinkedIn queue it splits rows against the shared `directory.csv` into
resolved / cached-negative / unresolved, commits the Gmail observations and the
directory resolutions back into `directory.csv`, and records the per-slug
results on the orchestrator's transient `imp.state`. It mutates `imp.state` in
place (no state dict is threaded) and returns True; the orchestrator
(`importer.py`) owns the run loop and manifest.

The module-level functions are pure transforms the step (and the enrich step)
call: queue↔directory row builders (`directory_rows_from_gmail_queue`,
`apply_directory_to_gmail_queue`), the two `commit_*_to_directory` writers, the
per-account resolution combiner (`combine_gmail_resolution_records`), and the
small record normalizers (`gmail_queue_records`, `ordered_records`,
`_is_resolvable_person`, `parse_json_list`). Everything cross-source — the whole
`directory.csv` contract, the resolution normalizers, the people.csv
materializers — is imported from `imports/directory.py`; `emit_progress` comes
from `common/proc.py` and its `GMAIL_IMPORT_PREFIX` tag plus
`artifact_dir_from_state` from `imports/gmail/util.py`.

Changelog:
  2026-07-23 (steps split): extracted from the file-loaded gmail/import_steps.py
    (deleted). The directory-apply step body became this module-level
    `run_gmail_directory(imp)` taking the GmailImport orchestrator; the pure
    directory-commit / queue helpers moved here verbatim so `importer.py` and
    `steps/enrich.py` import them from their concrete home. No behavior change:
    fixed output paths, split semantics, and manifest payloads are unchanged.
"""
from __future__ import annotations

import json
import sys
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
from packs.ingestion.primitives.common.paths import DEFAULT_DIRECTORY_CSV  # noqa: E402
from packs.ingestion.primitives.discover.common import read_csv_rows, source_slug  # noqa: E402
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
    artifact_dir_from_state,
)
from packs.ingestion.schemas.gmail_artifacts import LINKEDIN_RESOLUTION_COLUMNS  # noqa: E402
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402

if TYPE_CHECKING:
    from packs.ingestion.primitives.imports.gmail.importer import GmailImport


def _is_resolvable_person(row: dict[str, str]) -> bool:
    """Return True if the queue row looks like a real person worth resolving."""
    email = (row.get("primary_email") or row.get("email") or row.get("handle") or "").strip()
    name = (row.get("display_name") or row.get("full_name") or "").strip()
    if not email or not name:
        return False
    return not is_generic_or_non_person(email) and is_likely_person_name(name)


def apply_directory_to_gmail_queue(record: dict[str, Any], directory_csv: Path, output_dir: Path) -> dict[str, Any]:
    """Split a Gmail LinkedIn queue against directory.csv into resolved / cached-negative / unresolved."""
    queue_csv = Path(str(record.get("queue_csv") or ""))
    fields, rows = read_csv_rows(queue_csv)
    lookup = load_directory_lookup(directory_csv)
    resolved: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []
    cached_negative: list[dict[str, str]] = []
    filtered_non_person = 0
    for row in rows:
        match = directory_match_for_queue_row(row, lookup)
        if match and directory_row_is_found(match):
            resolved.append(resolution_from_directory_match(row, match))
        elif match and directory_row_is_prior_negative(match):
            cached_negative.append(row)
        elif not _is_resolvable_person(row):
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
    result = dict(record)
    result.update({
        "directory_csv": str(directory_csv),
        "directory_resolutions_csv": str(resolutions_csv),
        "unresolved_queue_csv": str(unresolved_csv),
        "cached_negative_queue_csv": str(cached_negative_csv),
        "input_rows": len(rows),
        "resolved": len(resolved),
        "unresolved": len(unresolved),
        "cached_negative": len(cached_negative),
        "filtered_non_person": filtered_non_person,
    })
    return result


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


def directory_rows_from_gmail_queue(record: dict[str, Any]) -> list[dict[str, str]]:
    """Directory 'observed' rows for every account/email in a Gmail queue record."""
    queue_csv = Path(str(record.get("queue_csv") or ""))
    if not queue_csv.exists():
        return []
    account_email = str(record.get("account_email") or "").strip().lower()
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
        for account in accounts:
            output.append(normalized_directory_row({
                "source": "gmail_msgvault",
                "source_key": gmail_directory_source_key(account, email, row.get("id") or ""),
                "source_account": account,
                "source_id": json.dumps(source_ids, ensure_ascii=False),
                "source_channels": row.get("source_channels") or "gmail_msgvault",
                "status": "observed",
                "email": email,
                "name": row.get("display_name") or row.get("full_name") or "",
                "confidence": "0",
                "evidence": json.dumps({
                    "source": "gmail_msgvault",
                    "queue_csv": str(queue_csv),
                    "account_email": account,
                    "source_ids": source_ids,
                    "total_messages": row.get("total_messages", ""),
                    "thread_count": row.get("thread_count", ""),
                    "last_interaction": row.get("last_interaction", ""),
                }, sort_keys=True),
                "reasoning": "Observed in local Gmail metadata",
            }, source_artifact=str(queue_csv), updated_at=now_iso()))
    return [row for row in output if row]


def commit_gmail_observations_to_directory(input_cfg: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    """Merge every Gmail queue's observed rows into directory.csv."""
    directory_csv = Path(input_cfg.get("linkedin_directory_csv") or artifacts.get("directory_csv") or DEFAULT_DIRECTORY_CSV)
    rows: list[dict[str, str]] = []
    for record in gmail_queue_records(artifacts):
        if isinstance(record, dict):
            rows.extend(directory_rows_from_gmail_queue(record))
    result = commit_directory_rows(directory_csv, rows)
    result["gmail_observation_rows"] = len(rows)
    artifacts["directory_csv"] = str(directory_csv)
    artifacts["gmail_directory_observation_checkpoint"] = result
    return result


def commit_gmail_resolutions_to_directory(input_cfg: dict[str, Any], artifacts: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge stored per-account Gmail LinkedIn resolutions (found + negative) into directory.csv."""
    directory_csv = Path(input_cfg.get("linkedin_directory_csv") or artifacts.get("directory_csv") or DEFAULT_DIRECTORY_CSV)
    rows: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, dict) or not record.get("resolutions_csv"):
            continue
        account_email = str(record.get("account_email") or "").strip().lower()
        resolution_path = Path(str(record["resolutions_csv"]))
        if not resolution_path.exists():
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
                "resolutions_csv": record.get("resolutions_csv"),
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
            }, source_artifact=str(record.get("resolutions_csv") or ""), updated_at=now_iso()))
    result = commit_directory_rows(directory_csv, rows)
    result["gmail_resolution_rows"] = len(rows)
    result["gmail_resolution_found_rows"] = sum(1 for row in rows if row.get("status") == "found")
    result["gmail_resolution_negative_rows"] = sum(1 for row in rows if row.get("status") in (RESOLUTION_NEGATIVE_STATUSES | {"not_found"}))
    artifacts["directory_csv"] = str(directory_csv)
    artifacts["gmail_directory_resolution_checkpoint"] = result
    return result


def combine_gmail_resolution_records(records: list[dict[str, Any]], run_dir: Path) -> list[dict[str, Any]]:
    """Group per-account resolution CSVs by (slug, people_csv) and merge each into one file."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not record.get("people_csv") or not record.get("resolutions_csv"):
            continue
        slug = source_slug(record.get("account_email") or record.get("slug") or record.get("people_csv") or "all")
        key = (slug, str(record["people_csv"]))
        group = grouped.setdefault(key, {"account_email": record.get("account_email", ""), "slug": slug, "people_csv": record["people_csv"], "resolution_paths": []})
        group["resolution_paths"].append(Path(str(record["resolutions_csv"])))
    combined: list[dict[str, Any]] = []
    for (slug, people_csv), group in sorted(grouped.items(), key=lambda item: item[0]):
        rows = merge_resolution_rows(group["resolution_paths"])
        if not rows:
            continue
        out_dir = run_dir / f"gmail-combined-resolutions-{slug}"
        out_path = out_dir / "linkedin_resolutions.csv"
        CsvIO.write_dict_rows(out_path, LINKEDIN_RESOLUTION_COLUMNS, rows)
        combined.append({
            "account_email": group.get("account_email", ""),
            "slug": slug,
            "people_csv": people_csv,
            "resolutions_csv": str(out_path),
            "resolution_sources": [str(path) for path in group["resolution_paths"]],
            "resolved": len(rows),
        })
    return combined


def ordered_records(records: list[dict[str, Any]], account_order: list[str] | None = None) -> list[dict[str, Any]]:
    """Sort account records by the configured account order, then by a stable key."""
    order = {email: index for index, email in enumerate(account_order or []) if email}
    return sorted(
        records,
        key=lambda record: (
            order.get(str(record.get("account_email") or ""), len(order)),
            str(record.get("account_email") or record.get("slug") or record.get("people_csv") or record.get("queue_csv") or ""),
        ),
    )


def gmail_queue_records(artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the per-account Gmail queue records out of the run artifacts."""
    queue_records = artifacts.get("gmail_linkedin_resolution_queue_csvs") or []
    if not queue_records and artifacts.get("gmail_linkedin_resolution_queue_csv"):
        queue_records = [{"account_email": "", "queue_csv": artifacts.get("gmail_linkedin_resolution_queue_csv"), "people_csv": artifacts.get("gmail_people_csv"), "slug": "all"}]
    return [record for record in queue_records if isinstance(record, dict) and record.get("queue_csv")]


def run_gmail_directory(imp: "GmailImport") -> bool:
    """Apply directory.csv LinkedIn mappings to every Gmail queue, recording
    resolved/unresolved/cached-negative on `imp.state`.

    Commits the Gmail observations and directory resolutions back into
    `directory.csv`, then splits each per-account queue and accumulates the
    per-slug resolution / unresolved / cached-negative records the enrich step
    and the candidates writer read. Returns True (this step never fails hard —
    an empty queue is recorded as `skipped`)."""
    input_cfg = imp.state.get("input", {})
    artifacts = imp.state.setdefault("artifacts", {})
    queue_records = ordered_records(gmail_queue_records(artifacts), unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")))
    if not queue_records:
        checkpoint = build_directory_checkpoint(input_cfg, artifacts)
        artifacts["directory_csv"] = checkpoint["directory_csv"]
        imp._mark_step("gmail_directory", "skipped", reason="no Gmail LinkedIn queue", checkpoint=checkpoint)
        return True
    imp._begin_step("gmail_directory", f"Applying directory LinkedIn mappings to {len(queue_records)} Gmail queue(s).")
    observation_checkpoint = commit_gmail_observations_to_directory(input_cfg, artifacts)
    checkpoint = build_directory_checkpoint(input_cfg, artifacts)
    directory_csv = Path(checkpoint["directory_csv"])
    artifacts["directory_csv"] = str(directory_csv)
    artifacts["gmail_directory_by_slug"] = {}
    by_slug = artifacts["gmail_directory_by_slug"]
    artifacts["gmail_directory_resolution_records"] = []
    artifacts["gmail_unresolved_linkedin_resolution_queue_csvs"] = []
    artifacts["gmail_cached_negative_linkedin_resolution_queue_csvs"] = []
    results = []
    total_resolved = 0
    total_unresolved = 0
    total_cached_negative = 0
    for index, record in enumerate(queue_records):
        slug = source_slug(record.get("account_email") or record.get("slug") or f"queue-{index}")
        out_dir = artifact_dir_from_state(imp.state) / f"gmail-directory-{slug}"
        result = apply_directory_to_gmail_queue(record, directory_csv, out_dir)
        result["slug"] = slug
        by_slug[slug] = result
        total_resolved += int(result.get("resolved") or 0)
        total_unresolved += int(result.get("unresolved") or 0)
        total_cached_negative += int(result.get("cached_negative") or 0)
        if int(result.get("resolved") or 0) > 0:
            artifacts["gmail_directory_resolution_records"].append({
                "account_email": record.get("account_email", ""),
                "resolutions_csv": result.get("directory_resolutions_csv"),
                "people_csv": record.get("people_csv"),
                "slug": slug,
                "source": "directory",
                "resolved": result.get("resolved"),
            })
        if int(result.get("unresolved") or 0) > 0:
            artifacts["gmail_unresolved_linkedin_resolution_queue_csvs"].append({
                "account_email": record.get("account_email", ""),
                "queue_csv": result.get("unresolved_queue_csv"),
                "people_csv": record.get("people_csv"),
                "slug": slug,
                "source": "directory_unresolved",
                "unresolved": result.get("unresolved"),
            })
        if int(result.get("cached_negative") or 0) > 0:
            artifacts["gmail_cached_negative_linkedin_resolution_queue_csvs"].append({
                "account_email": record.get("account_email", ""),
                "queue_csv": result.get("cached_negative_queue_csv"),
                "people_csv": record.get("people_csv"),
                "slug": slug,
                "source": "directory_cached_negative",
                "cached_negative": result.get("cached_negative"),
            })
        results.append(result)
    imp._mark_step("gmail_directory", "completed", checkpoint=checkpoint, observation_checkpoint=observation_checkpoint, resolved=total_resolved, unresolved=total_unresolved, cached_negative=total_cached_negative, payload={"results": results})
    if total_cached_negative:
        emit_progress(f"Gmail directory mappings applied: {total_resolved} resolved, {total_cached_negative} already attempted, {total_unresolved} unresolved.", GMAIL_IMPORT_PREFIX)
    else:
        emit_progress(f"Gmail directory mappings applied: {total_resolved} resolved, {total_unresolved} unresolved.", GMAIL_IMPORT_PREFIX)
    return True
