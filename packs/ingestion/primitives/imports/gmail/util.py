#!/usr/bin/env python3
"""Gmail import helpers shared across the importer and its step modules.

Discovery-artifact collection (`gmail_artifacts_from_discovery`), candidate
writing (`write_gmail_candidates`), and two tiny cross-cutting things the
`importer.py` orchestrator and both `steps/` modules lean on:
`GMAIL_IMPORT_PREFIX` (the stderr progress tag they hand to
`common.proc.emit_progress`) and `artifact_dir_from_state` (the
intermediate-artifact directory).

Free and local: apply the shared identity directory to the discovered Gmail
queues, materialize `import/gmail/people.csv`, and write the still-unresolved
contacts to `import/gmail/candidates.csv` for the deep-context processing
layer, which owns ALL resolution and enrichment: stored legacy resolutions
migrate into overrides/review.csv via `bin/deep-context migrate-legacy` (the
central source of truth the fan-in and the review flow read); new lookups run
through deep-context's judged, budget-gated stages.

Changelog:
  2026-07-24 (dedup): the local `emit_progress` wrapper was deleted — it only
    bound a prefix onto `common.proc.emit_progress`, which already takes one.
    Callers import that function directly and pass `GMAIL_IMPORT_PREFIX`.
  2026-07-23 (steps split): emit_progress + artifact_dir_from_state moved here
    from the old import_steps.py so the importer and both steps/ modules share
    one home instead of the file-loaded module owning them.
  2026-07-23 (audit):
    - One upfront repo-root path bootstrap replaced the duplicated try/except
      import block.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so packs.* imports work in module AND script mode
# (uv run .../util.py); must be in-file because script-mode never imports
# the package __init__.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.schemas.candidates_schema import (  # noqa: E402
    CANDIDATES_SCHEMA_COLUMNS,
    candidate_key_for,
    normalize_candidate_row,
)
from packs.ingestion.primitives.common.jsonio import read_json  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR, DEFAULT_DISCOVER_DIR  # noqa: E402
from packs.ingestion.primitives.discover.common import (  # noqa: E402
    read_csv_rows,
    source_slug,
    write_csv_rows,
)

# stderr progress tag for the gmail import chain. The importer and both steps/
# modules pass it to common.proc.emit_progress, which is the one home for the
# "write a progress line" behavior — this vertical owns only its prefix.
GMAIL_IMPORT_PREFIX = "[gmail-import]"


def artifact_dir_from_state(state: dict[str, Any]) -> Path:
    """Directory the import writes intermediate artifacts into."""
    return Path(str(state.get("artifact_dir") or DEFAULT_DISCOVER_DIR))


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


