#!/usr/bin/env python3
"""Gmail import helpers shared across the importer and its step modules.

The boundary parser (`discovery_from_manifest` -> `GmailDiscovery`), the
candidate-people materializer (`candidate_people`), and `GMAIL_IMPORT_PREFIX`
(the stderr progress tag handed to `common.proc.emit_progress`).

Everything crossing into this stage is parsed HERE, once, into frozen
dataclasses; downstream code takes typed values and never re-defends. Children
with an invalid/missing people CSV are reported on `GmailDiscovery.invalid`
instead of being silently dropped.

Changelog:
  2026-07-28 (parse at the boundary): `gmail_artifacts_from_discovery`'s
    untyped artifacts dict became `GmailDiscovery` (frozen dataclasses,
    accounts pre-sorted). The one-letter-apart artifact keys and their
    accessors (`gmail_account_queue_records` / `gmail_stage_queue_csv`) are
    gone — a typed field cannot be confused with its sibling.
    `gmail_candidate_people` became `candidate_people` over explicit path
    lists; `artifact_dir_from_state` died with the state blob.
  2026-07-26 (declaration owns the path): the parser does not ask the manifest
    WHERE the stage queue is — both fixed paths default to gmail discovery's
    DECLARED constants (`GMAIL_STAGE_QUEUE_CSV`, `GMAIL_STAGE_MANIFEST_JSON`),
    imported from it. Both are keyword parameters so a caller (a test) runs it
    against its own directory instead of patching a module global.
  2026-07-24 (dedup): the local `emit_progress` wrapper was deleted — it only
    bound a prefix onto `common.proc.emit_progress`, which already takes one.
    Callers import that function directly and pass `GMAIL_IMPORT_PREFIX`.
  2026-07-23 (steps split): emit_progress + artifact_dir_from_state moved here
    from the old import_steps.py so the importer and both steps/ modules share
    one home instead of the file-loaded module owning them.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Repo-root bootstrap so packs.* imports work in module AND script mode
# (uv run .../util.py); must be in-file because script-mode never imports
# the package __init__.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.schemas.candidates_schema import candidate_key_for, normalize_candidate_row  # noqa: E402
from packs.ingestion.schemas.people_schema import normalize_people_row  # noqa: E402
from packs.ingestion.primitives.common.jsonio import read_json  # noqa: E402
from packs.ingestion.primitives.discover.common import (  # noqa: E402
    read_csv_rows,
    source_slug,
)
from packs.ingestion.primitives.discover.gmail.discover import (  # noqa: E402
    GMAIL_STAGE_MANIFEST_JSON,
    GMAIL_STAGE_QUEUE_CSV,
)

# stderr progress tag for the gmail import chain. The importer and both steps/
# modules pass it to common.proc.emit_progress, which is the one home for the
# "write a progress line" behavior — this vertical owns only its prefix.
GMAIL_IMPORT_PREFIX = "[gmail-import]"


@dataclass(frozen=True)
class GmailAccount:
    """One discovery account whose people.csv passed the schema check.

    `queue_csv` is "" for an account that produced a valid people.csv but no
    LinkedIn queue on disk — such accounts still feed the no-resolutions
    people merge."""

    email: str
    slug: str
    queue_csv: str
    people_csv: str


@dataclass(frozen=True)
class InvalidAccount:
    """A discovery child whose people.csv failed the schema check — reported,
    never silently dropped."""

    email: str
    people_csv: str
    queue_csv: str
    reason: str


@dataclass(frozen=True)
class GmailDiscovery:
    """Everything the import takes from gmail discovery, parsed once.

    `accounts` (queue + valid people.csv) is what the import iterates;
    `people_accounts` (valid people.csv, queue optional) is the superset the
    no-resolutions people merge preserves. Both are pre-sorted by
    (email, slug) — account order is deterministic from here on."""

    stage_queue_csv: str
    accounts: tuple[GmailAccount, ...]
    people_accounts: tuple[GmailAccount, ...]
    invalid: tuple[InvalidAccount, ...]


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


def _account_sort_key(account: GmailAccount) -> str:
    return account.email or account.slug or account.people_csv or account.queue_csv


def discovery_from_manifest(
    *,
    manifest_json: Path | None = None,
    queue_csv: Path | None = None,
) -> GmailDiscovery:
    """Parse the gmail DISCOVERY manifest into `GmailDiscovery` — the stage's
    one boundary.

    The manifest is read for ONE thing: the per-account children (which accounts
    discovery ran, and where each one's queue/people CSV landed). It is not asked
    where anything IS — both fixed paths default to gmail discovery's DECLARED
    ones, imported from it, so the two stages name one string each and a manifest
    written by an older version cannot point this import at a different file."""
    manifest_json = manifest_json or Path(GMAIL_STAGE_MANIFEST_JSON)
    queue_csv = queue_csv or Path(GMAIL_STAGE_QUEUE_CSV)
    manifest = read_json(manifest_json, {}) or {}
    stage_queue_csv = str(queue_csv) if queue_csv.exists() else ""
    accounts: list[GmailAccount] = []
    people_accounts: list[GmailAccount] = []
    invalid: list[InvalidAccount] = []
    for child in manifest.get("children") or []:
        if not isinstance(child, dict):
            continue
        email = str(child.get("account_email") or "")
        artifacts = _child_artifacts(child)
        child_queue = str(artifacts.get("linkedin_resolution_queue_csv") or "")
        people_csv = str(artifacts.get("people_csv") or "")
        slug = source_slug(email or "gmail")
        if not _valid_gmail_people_csv(people_csv):
            if people_csv:
                invalid.append(InvalidAccount(
                    email=email,
                    people_csv=people_csv,
                    queue_csv=child_queue,
                    reason="missing_people_schema_or_interaction_counts",
                ))
            continue
        account = GmailAccount(
            email=email,
            slug=slug,
            queue_csv=child_queue if child_queue and Path(child_queue).exists() else "",
            people_csv=people_csv,
        )
        people_accounts.append(account)
        if account.queue_csv:
            accounts.append(account)
    return GmailDiscovery(
        stage_queue_csv=stage_queue_csv,
        accounts=tuple(sorted(accounts, key=_account_sort_key)),
        people_accounts=tuple(sorted(people_accounts, key=_account_sort_key)),
        invalid=tuple(invalid),
    )


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


def candidate_people(unresolved_csvs: list[str], cached_negative_csvs: list[str]) -> dict[str, Any]:
    """Return unresolved Gmail contacts as ordinary no-LinkedIn people rows.

    Two passes in order — plain unresolved first, then cached-negative — so a
    contact present in both keeps the plain-unresolved evidence (first wins)."""
    by_key: dict[str, dict[str, str]] = {}
    skipped = {"no_email": 0, "duplicate_email": 0}
    groups = ((unresolved_csvs, False), (cached_negative_csvs, True))
    for paths, cached_negative in groups:
        for path_text in paths:
            queue_path = Path(str(path_text or ""))
            if not path_text or not queue_path.exists():
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
    rows = []
    for key in sorted(by_key):
        candidate = by_key[key]
        rows.append(normalize_people_row({
            "id": f"candidate:{key}",
            "full_name": candidate.get("full_name", ""),
            "primary_email": candidate.get("primary_email", ""),
            "all_emails": candidate.get("all_emails", ""),
            "primary_phone": candidate.get("primary_phone", ""),
            "all_phones": candidate.get("all_phones", ""),
            "summary": "selection=unresolved",
            "source_channels": "gmail_msgvault",
            "source_artifacts": "gmail resolution queue",
            "interaction_counts": candidate.get("interaction_counts", ""),
            "last_interaction": candidate.get("last_interaction", ""),
        }))
    return {
        "people": rows,
        "candidates": len(rows),
        "skipped": skipped,
    }
