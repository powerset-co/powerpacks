"""Gmail import — apply STORED resolutions to each account people.csv, then
materialize the merged Gmail people artifact.

`run_gmail_apply_and_enrich(imp, splits, people_accounts)` is the second import
step. It commits the directory-derived resolutions into `directory.csv`,
combines them per account, applies them to each account's people.csv via an
in-process `GmailExtractor().apply_resolutions(...)` call, and materializes one
merged Gmail `people.gmail.csv`. It runs NO Parallel resolution and NO RapidAPI
hydration — deep-context owns all resolution and enrichment.

Returns an `ApplyOutcome`; on an apply failure it records the failed step,
emits the failed payload, and returns `ok=False` so the orchestrator writes a
failed manifest. The directory-commit / resolution-combine transforms live in
`steps/directory.py`; the cross-source `materialize_gmail_merged_people_csv`
comes from `imports/directory.py`.

Changelog:
  2026-07-28 (typed steps): inputs are the directory step's `QueueSplit`s and
    the boundary's `people_accounts`; the result is a returned `ApplyOutcome`
    instead of eleven `imp.state["artifacts"]` keys. Deleted the two dead
    record feeders: the "explicit resolutions" branch keyed off
    `gmail_resolutions_csv` (no writer since the CLI flag died) and the
    `gmail_linkedin_resolutions_csvs` read (its only repo mention was the read
    itself). The live flow — commit, combine, apply, materialize, fallback
    people merge — is byte-identical.
  2026-07-25 (declared contract): this step records the people file it actually
    materialized on the orchestrator as `imp.gmail_people_csv` (one plain
    attribute). The importer reads that instead of guessing between state keys.
  2026-07-23 (in-process engine): the apply-resolutions step no longer spawns a
    subprocess; a direct `GmailExtractor().apply_resolutions(...)` call
    branches on the RETURNED payload's status.
  2026-07-23 (steps split): extracted from the file-loaded gmail/import_steps.py
    (deleted). No behavior change.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Repo-root bootstrap so `packs.*` imports resolve however this module is loaded.
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR  # noqa: E402
from packs.ingestion.primitives.discover.gmail.extract_gmail import GmailExtractor  # noqa: E402
from packs.ingestion.primitives.imports.directory import (  # noqa: E402
    materialize_gmail_merged_people_csv,
)
from packs.ingestion.primitives.imports.gmail.steps.directory import (  # noqa: E402
    QueueSplit,
    ResolutionRecord,
    combine_gmail_resolution_records,
    commit_gmail_resolutions_to_directory,
)
from packs.ingestion.primitives.common.proc import emit_progress  # noqa: E402
from packs.ingestion.primitives.imports.gmail.util import (  # noqa: E402
    GMAIL_IMPORT_PREFIX,
    GmailAccount,
)

if TYPE_CHECKING:
    from packs.ingestion.primitives.imports.gmail.importer import GmailImport

GMAIL_MERGED_PEOPLE_CSV = DEFAULT_BASE_DIR / "gmail" / "people.gmail.csv"


@dataclass
class ApplyOutcome:
    """What the apply step produced, returned to the orchestrator."""

    ok: bool
    results: list[dict[str, Any]] = field(default_factory=list)
    merge: dict[str, Any] = field(default_factory=dict)
    combined: list[ResolutionRecord] = field(default_factory=list)
    error: dict[str, Any] | None = None


def _record_sort_key(record: ResolutionRecord) -> str:
    return record.account_email or record.slug or record.people_csv


def run_gmail_apply_and_enrich(
    imp: "GmailImport",
    splits: list[QueueSplit],
    people_accounts: tuple[GmailAccount, ...],
) -> ApplyOutcome:
    """Apply the combined directory resolutions to each account's people.csv
    and materialize the merged Gmail people artifact.

    A run with no resolutions is `skipped`, not empty: the discovered account
    rows are preserved so the fan-in can mint candidate:<contact-key>
    identities for Deep Context."""
    records = [
        ResolutionRecord(
            account_email=split.account.email,
            slug=split.account.slug,
            people_csv=split.account.people_csv,
            resolutions_csv=split.resolutions_csv,
            source="directory",
            resolved=split.resolved,
        )
        for split in splits
        if split.resolved > 0
    ]
    if records:
        commit_gmail_resolutions_to_directory(imp.directory_csv, records)
    combined = combine_gmail_resolution_records(records, imp.import_dir)
    if not combined:
        people_csvs = [
            account.people_csv for account in people_accounts
            if Path(account.people_csv).exists()
        ]
        merge = materialize_gmail_merged_people_csv(people_csvs, GMAIL_MERGED_PEOPLE_CSV)
        if merge.get("status") == "completed" and merge.get("people_csv"):
            imp.gmail_people_csv = Path(str(merge["people_csv"]))
        imp._mark_step("gmail_apply_enrich", "skipped", reason="no gmail resolutions")
        return ApplyOutcome(ok=True, merge=merge)
    combined = sorted(combined, key=_record_sort_key)
    imp._begin_step("gmail_apply_enrich", f"Applying Gmail LinkedIn matches for {len(combined)} account file(s).")
    results: list[dict[str, Any]] = []
    final_people_csvs: list[str] = []
    for record in combined:
        resolved_dir = Path(record.people_csv).parent / "resolved"
        # In-process engine call (no subprocess): GmailExtractor.apply_resolutions
        # attaches the STORED resolutions and RETURNS the payload the CLI used to
        # emit. A ValueError surfaces the way the old subprocess CLI did (exit 2 ->
        # error payload -> failed step).
        try:
            payload = GmailExtractor().apply_resolutions(
                people_csv=record.people_csv,
                resolutions_csv=record.resolutions_csv,
                output_dir=resolved_dir,
            )
        except ValueError as exc:
            payload = {"status": "error", "error": str(exc)}
        if payload.get("status") != "completed":
            imp._mark_step("gmail_apply_enrich", "failed", error=payload)
            emit({"status": "failed", "step_id": "gmail_apply_enrich", "error": payload})
            return ApplyOutcome(ok=False, results=results, combined=combined, error=payload)
        resolved_people = str(payload.get("people_csv") or record.people_csv)
        final_people_csvs.append(resolved_people)
        # Fallback source for the import's people.csv when the merge below writes
        # nothing (no account produced a row): the LAST account's resolved file.
        # Overwritten by the merge output when it completes.
        imp.gmail_people_csv = Path(resolved_people)
        results.append({
            "account_email": record.account_email,
            "slug": record.slug,
            "apply": payload,
            "people_csv": resolved_people,
            "final_people_csv": resolved_people,
        })
    merge = materialize_gmail_merged_people_csv(final_people_csvs, GMAIL_MERGED_PEOPLE_CSV)
    if merge.get("status") == "completed" and merge.get("people_csv"):
        imp.gmail_people_csv = Path(str(merge["people_csv"]))
    imp._mark_step("gmail_apply_enrich", "completed", payload={"results": results, "gmail_merged_people": merge})
    emit_progress("Gmail LinkedIn matches applied and enrichment completed.", GMAIL_IMPORT_PREFIX)
    return ApplyOutcome(
        ok=True,
        results=results,
        merge=merge,
        combined=combined,
    )
