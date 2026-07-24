#!/usr/bin/env python3
"""Import discovered Gmail contacts (directory-only — the only mode).

Free and local: apply the shared identity directory to the discovered Gmail
queues, materialize `import/gmail/people.csv`, and write the still-unresolved
contacts to `import/gmail/candidates.csv` for the deep-context processing
layer, which owns ALL resolution and enrichment: stored legacy resolutions
migrate into overrides/review.csv via `bin/deep-context migrate-legacy` (the
central source of truth the fan-in and the review flow read); new lookups run
through deep-context's judged, budget-gated stages.

THE gmail import entry. Owns the `GmailImport` orchestrator (fixed import dir,
transient run state, the two-step chain, the matched-people / candidates split,
the directory quality gate, and the manifest) plus the CLI surface
(`run` / `--force`) and `GMAIL_IMPORT_CONTRACT` (re-exported by the package
__init__). The two steps are ordinary functions imported from the `steps/`
package — `run_gmail_directory` (directory apply + commit) and
`run_gmail_apply_and_enrich` (apply STORED resolutions + materialize) — that
take this orchestrator and mutate its transient `self.state` in place; the pure
directory/queue transforms live alongside them in `steps/directory.py`.
Exit 0 completed/skipped, 1 failed. No approval gate: nothing here spends.

Changelog:
  2026-07-23 (dead accounts.json registry): dropped the vestigial `--accounts`
    read. The `accounts.json` gmail channel was never populated, so
    `linked_gmail_accounts` always returned `[]`; removed the `read_accounts`/
    `linked_gmail_accounts` calls, the `emails` var, and the always-empty
    `gmail_account_emails`/`from_accounts` manifest-input fields, plus the
    `--accounts` CLI arg. Directory apply + stored-resolution attach are
    unchanged.
  2026-07-23 (steps split): `import_steps.py` and its file-loader
    (`imports.common.load_gmail_import_steps`) are gone. `GmailImport` moved
    here from `import_steps.py`, and the two step bodies were pieced out into
    `steps/directory.py` (`run_gmail_directory`) and `steps/enrich.py`
    (`run_gmail_apply_and_enrich`), which this module imports and calls. No more
    `importlib.util.spec_from_file_location` — a normal package import replaces
    the fossil loader. CLI flags, exit codes, fixed output paths, `ledger.json`
    unlink, and manifest payloads are unchanged.
  2026-07-23 (oop): the import flow (manifest no-op check, state construction,
    step dispatch, people/candidate materialization, quality gate, manifest) was
    folded into the `GmailImport` orchestrator. CLI flags, exit codes, fixed
    output paths, and manifest payloads are unchanged.
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

from packs.ingestion.primitives.common.jsonio import emit, now_iso  # noqa: E402
from packs.ingestion.primitives.common.paths import (  # noqa: E402
    DEFAULT_BASE_DIR,
    DEFAULT_DIRECTORY_CSV,
    DEFAULT_IMPORT_DIR,
    DEFAULT_PROFILE_CACHE_DIR,
    source_import_dir,
)
from packs.ingestion.primitives.imports.common import (  # noqa: E402
    copy_people_csv,
    csv_count,
    directory_source_account_quality,
    import_manifest_current,
    normalize_directory_source_accounts,
    write_manifest,
)
from packs.ingestion.primitives.imports.gmail.steps.directory import run_gmail_directory  # noqa: E402
from packs.ingestion.primitives.imports.gmail.steps.enrich import run_gmail_apply_and_enrich  # noqa: E402
from packs.ingestion.primitives.imports.gmail.util import (  # noqa: E402
    emit_progress,
    gmail_artifacts_from_discovery,
    write_gmail_candidates,
)

GMAIL_IMPORT_CONTRACT = "gmail-directory-only-v2"


class GmailImport:
    """Orchestrates the directory-only Gmail import.

    Owns the fixed import dir, transient run state, the two-step chain
    (directory apply -> stored-resolution apply + people materialization), the
    matched-people / candidates split, the directory source-account quality
    gate, and the import manifest. The step functions (`run_gmail_directory`,
    `run_gmail_apply_and_enrich`, imported from `steps/`) take this orchestrator
    and mutate `self.state` in place instead of threading a state dict around;
    `_mark_step` / `_begin_step` are the status-tracking helpers they call.

    A step returning False ends the run with status `failed` (steps record their
    own error in transient state); there is no approval gate — nothing here
    spends."""

    def __init__(self, *, args: argparse.Namespace, contract: str) -> None:
        self.args = args
        self.contract = contract
        self.import_dir = source_import_dir("gmail")
        self.state: dict[str, Any] = {}

    # --- transient run state --------------------------------------------------

    def _mark_step(self, step: str, status: str, **extra: Any) -> None:
        """Update one step's status/timestamps in transient state."""
        rec = self.state.setdefault("steps", {}).setdefault(step, {"id": step})
        if status == "running" and "started_at" not in rec:
            rec["started_at"] = now_iso()
        if status in {"completed", "failed", "blocked", "skipped"}:
            rec["finished_at"] = now_iso()
        rec["status"] = status
        rec.update({k: v for k, v in extra.items() if v is not None})

    def _begin_step(self, step: str, message: str) -> None:
        """Mark a step running and emit a progress line."""
        self._mark_step(step, "running")
        emit_progress(message)

    # --- orchestration --------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """The whole import: fingerprint no-op check -> build transient state -> the
        two step functions (directory match, then apply + people materialization)
        -> candidates + directory quality checks -> the import manifest."""
        args = self.args
        (self.import_dir / "ledger.json").unlink(missing_ok=True)
        expected_input = {
            "pipeline_contract": self.contract,
            "mode": "directory-only",
        }
        current = import_manifest_current("gmail", expected_input, import_dir=DEFAULT_IMPORT_DIR)
        if current and not getattr(args, "force", False):
            return current
        import_dir = self.import_dir
        self.state = {
            "primitive": "import_contacts_gmail",
            "source": "gmail",
            "status": "running",
            "artifact_dir": str(import_dir),
            "input": {
                "operator_id": args.operator_id,
                # Directory-only, always: this import applies the directory and any
                # STORED resolutions; resolution + enrichment live in deep-context
                # (migrate-legacy for the stored era, judged lookups for new people).
                "linkedin_directory_csv": str(DEFAULT_DIRECTORY_CSV),
                "profile_cache_dir": str(DEFAULT_PROFILE_CACHE_DIR),
            },
            "steps": {},
            "artifacts": gmail_artifacts_from_discovery(),
        }
        state = self.state
        if not state["artifacts"].get("gmail_linkedin_resolution_queue_csvs"):
            reason = "no Gmail discovery queue"
            status = "skipped"
            if state["artifacts"].get("gmail_linkedin_resolution_queue_csv") or state["artifacts"].get("gmail_invalid_discovery_records"):
                reason = "gmail_discovery_missing_per_account_people_csv"
            return write_manifest("gmail", {
                "status": status,
                "reason": reason,
                "artifact_dir": str(import_dir),
                "artifacts": state.get("artifacts", {}),
            }, import_dir=DEFAULT_IMPORT_DIR)
        for step in (run_gmail_directory, run_gmail_apply_and_enrich):
            if not step(self):
                return write_manifest("gmail", {
                    "status": "failed",
                    "artifact_dir": str(import_dir),
                    "steps": state.get("steps", {}),
                    "artifacts": state.get("artifacts", {}),
                }, import_dir=DEFAULT_IMPORT_DIR)
        state["status"] = "completed"
        people_csv = copy_people_csv("gmail", str(state.get("artifacts", {}).get("gmail_merged_people_csv") or state.get("artifacts", {}).get("gmail_people_csv") or ""), import_dir=DEFAULT_IMPORT_DIR)
        candidates = write_gmail_candidates(state.get("artifacts", {}), import_dir)
        directory_normalization = normalize_directory_source_accounts("gmail")
        directory_quality = directory_source_account_quality("gmail")
        if directory_quality["status"] != "ok":
            return write_manifest("gmail", {
                "status": "failed",
                "reason": "directory_source_account_quality_failed",
                "artifact_dir": str(import_dir),
                "outputs": {
                    "people_csv": people_csv,
                    "directory_csv": str(DEFAULT_DIRECTORY_CSV),
                },
                "directory_normalization": directory_normalization,
                "directory_quality": directory_quality,
                "steps": state.get("steps", {}),
                "artifacts": state.get("artifacts", {}),
            }, import_dir=DEFAULT_IMPORT_DIR)
        return write_manifest("gmail", {
            "status": "completed",
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
            "steps": state.get("steps", {}),
            "directory_normalization": directory_normalization,
            "directory_quality": directory_quality,
            "artifacts": state.get("artifacts", {}),
        }, import_dir=DEFAULT_IMPORT_DIR)


def run(args: argparse.Namespace) -> dict:
    """Build and run the `GmailImport` orchestrator for the given CLI args."""
    return GmailImport(args=args, contract=GMAIL_IMPORT_CONTRACT).run()


def build_parser() -> argparse.ArgumentParser:
    """CLI: one `run` command; `--force` bypasses the manifest no-op skip."""
    parser = argparse.ArgumentParser(description="Import discovered Gmail contacts (directory-only)")
    parser.add_argument("command", choices=["run"])
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
