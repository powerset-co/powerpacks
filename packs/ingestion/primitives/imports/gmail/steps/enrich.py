"""Gmail import — apply STORED resolutions to each account people.csv, then
materialize the merged Gmail people artifact.

`run_gmail_apply_and_enrich(imp)` is the second import step. It gathers the
explicit + directory + prior stored resolution records, commits them into
`directory.csv`, combines the per-account resolution CSVs, applies them to each
account's people.csv via an in-process `GmailDiscoverEngine().apply_resolutions(...)`
call, and materializes one merged Gmail `people.gmail.csv`. It runs NO Parallel
resolution and NO RapidAPI hydration — deep-context owns all resolution and
enrichment (stored legacy resolutions migrate via `bin/deep-context migrate-legacy`).

It mutates the orchestrator's transient `imp.state` in place; a non-completed
apply-resolutions payload ends the run (`imp.state["status"] = "failed"`, returns
False). The directory-commit / resolution-combine transforms live in
`steps/directory.py`; the cross-source `materialize_gmail_merged_people_csv`
comes from `imports/directory.py`; the shared `emit_progress` /
`artifact_dir_from_state` from `imports/gmail/util.py`.

Changelog:
  2026-07-23 (in-process engine): the apply-resolutions step no longer spawns
    gmail/discover_engine.py as a subprocess. The `run_cmd(py_cmd(...))` call was
    replaced by a direct `GmailDiscoverEngine().apply_resolutions(...)` call that
    branches on the RETURNED payload's status (a ValueError is mirrored into an
    error payload); the now unused `py_cmd`/`run_cmd` import was dropped. Fixed
    output paths and the failed-step payload are unchanged.
  2026-07-23 (steps split): extracted from the file-loaded gmail/import_steps.py
    (deleted). The apply-and-enrich step body became this module-level
    `run_gmail_apply_and_enrich(imp)` taking the GmailImport orchestrator. No
    behavior change: the apply-resolutions child target, fixed output paths, and
    manifest payloads are unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Repo-root bootstrap so `packs.*` imports resolve however this module is loaded.
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit, unique_strings  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR  # noqa: E402
from packs.ingestion.primitives.discover.common import source_slug  # noqa: E402
from packs.ingestion.primitives.discover.gmail.discover_engine import GmailDiscoverEngine  # noqa: E402
from packs.ingestion.primitives.imports.directory import (  # noqa: E402
    build_directory_checkpoint,
    materialize_gmail_merged_people_csv,
)
from packs.ingestion.primitives.imports.gmail.steps.directory import (  # noqa: E402
    combine_gmail_resolution_records,
    commit_gmail_resolutions_to_directory,
    ordered_records,
)
from packs.ingestion.primitives.imports.gmail.util import (  # noqa: E402
    artifact_dir_from_state,
    emit_progress,
)

if TYPE_CHECKING:
    from packs.ingestion.primitives.imports.gmail.importer import GmailImport


def run_gmail_apply_and_enrich(imp: "GmailImport") -> bool:
    """Apply the combined stored Gmail resolutions to each account's people.csv
    and materialize the merged Gmail people artifact.

    Returns True on success (or a `skipped` no-resolutions run); on an
    apply-resolutions child failure it records the failure on `imp.state`, emits
    the failed step payload, and returns False so the orchestrator writes a
    failed manifest."""
    input_cfg = imp.state.get("input", {})
    artifacts = imp.state.setdefault("artifacts", {})
    raw_resolution_records: list[dict[str, Any]] = []
    if input_cfg.get("gmail_resolutions_csv"):
        people_records = [
            record for record in ordered_records(
                artifacts.get("gmail_people_records") or [],
                unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")),
            )
            if isinstance(record, dict) and record.get("people_csv")
        ]
        raw_resolution_records.extend([
            {
                "account_email": record.get("account_email", ""),
                "resolutions_csv": input_cfg.get("gmail_resolutions_csv"),
                "people_csv": record.get("people_csv"),
                "slug": record.get("slug") or record.get("account_email") or f"account-{index}",
                "source": "explicit",
            }
            for index, record in enumerate(people_records)
        ])
    raw_resolution_records.extend(record for record in artifacts.get("gmail_directory_resolution_records") or [] if isinstance(record, dict))
    raw_resolution_records.extend(record for record in artifacts.get("gmail_linkedin_resolutions_csvs") or [] if isinstance(record, dict))
    if raw_resolution_records:
        commit_gmail_resolutions_to_directory(input_cfg, artifacts, raw_resolution_records)
    resolution_records = combine_gmail_resolution_records(raw_resolution_records, artifact_dir_from_state(imp.state))
    if not resolution_records:
        imp._mark_step("gmail_apply_enrich", "skipped", reason="no gmail resolutions")
        return True
    resolution_records = ordered_records(resolution_records, unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")))
    checkpoint = build_directory_checkpoint(input_cfg, artifacts)
    artifacts["directory_csv"] = checkpoint["directory_csv"]
    artifacts["directory_checkpoint"] = checkpoint
    artifacts["gmail_apply_enrich_by_slug"] = {}
    by_slug = artifacts["gmail_apply_enrich_by_slug"]
    artifacts["gmail_resolved_people_csvs"] = []
    artifacts["gmail_final_people_csvs"] = []
    artifacts["gmail_combined_resolutions_csvs"] = resolution_records
    imp._begin_step("gmail_apply_enrich", f"Applying Gmail LinkedIn matches for {len(resolution_records)} account file(s).")
    results = []
    final_people_csvs = []
    for index, record in enumerate(resolution_records):
        slug = source_slug(record.get("account_email") or record.get("slug") or f"account-{index}")
        account_dir = Path(str(record.get("people_csv") or "")).parent
        resolved_dir = account_dir / "resolved"
        # In-process engine call (no subprocess): GmailDiscoverEngine.apply_resolutions
        # attaches the STORED resolutions and RETURNS the payload the CLI used to
        # emit. A ValueError surfaces the way the old subprocess CLI did (exit 2 ->
        # error payload -> failed step): mirror it into an error payload so the
        # non-completed branch below stays equivalent.
        try:
            payload = GmailDiscoverEngine().apply_resolutions(
                people_csv=record["people_csv"],
                resolutions_csv=record["resolutions_csv"],
                output_dir=resolved_dir,
            )
        except ValueError as exc:
            payload = {"status": "error", "error": str(exc)}
        if payload.get("status") != "completed":
            imp._mark_step("gmail_apply_enrich", "failed", error=payload)
            imp.state["status"] = "failed"
            emit({"status": "failed", "step_id": "gmail_apply_enrich", "error": payload})
            return False
        resolved_people = payload.get("people_csv") or record["people_csv"]
        artifacts.setdefault("gmail_resolved_people_csvs", []).append(resolved_people)
        artifacts["gmail_resolved_people_csv"] = resolved_people
        result = {"account_email": record.get("account_email", ""), "slug": slug, "apply": payload, "people_csv": resolved_people}
        final_people_csvs.append(resolved_people)
        artifacts["gmail_people_csv"] = resolved_people
        result["final_people_csv"] = resolved_people
        by_slug[slug] = result
        results.append(result)
    artifacts["gmail_account_final_people_csvs"] = final_people_csvs
    artifacts["gmail_final_people_csvs"] = final_people_csvs
    gmail_merge = materialize_gmail_merged_people_csv(final_people_csvs, DEFAULT_BASE_DIR / "gmail" / "people.gmail.csv")
    artifacts["gmail_merged_people"] = gmail_merge
    if gmail_merge.get("status") == "completed" and gmail_merge.get("people_csv"):
        artifacts["gmail_merged_people_csv"] = gmail_merge.get("people_csv")
        artifacts["gmail_final_people_csvs"] = [str(gmail_merge.get("people_csv"))]
        artifacts["gmail_people_csv"] = str(gmail_merge.get("people_csv"))
    imp._mark_step("gmail_apply_enrich", "completed", payload={"results": results, "gmail_merged_people": gmail_merge})
    emit_progress("Gmail LinkedIn matches applied and enrichment completed.")
    return True
