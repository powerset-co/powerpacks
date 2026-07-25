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

Declared contract (`GmailImport`, node `gmail_import`):

  reads   discovery's per-account `linkedin_resolution_queue.csv` + `people.csv`
          (the `{account_slug}` templates gmail discovery declares — imported from
          there so the graph matches on ONE path string), discovery's
          `manifest.json` (the account selection), and `directory.csv`
  writes   `import/gmail/people.csv`, and the `gmail_msgvault` ROW SLICE of the
          shared `directory.csv` (`Artifact.owns_rows_where`)

`directory.csv` is BOTH: this node reads other sources' rows to resolve its own
queue (a real data dependency) and upserts its own rows back. Not declared,
because they never cross a node boundary: the per-account split CSVs, the
combined resolution CSVs, and `.powerpacks/network-import/gmail/people.gmail.csv`
— all written and read inside this one node.

Changelog:
  2026-07-25 (declared contract): `GmailImport` is a `pipeline/contract.py`
    `Node`. The flow moved from `run()` to `execute()` (`run()` is the inherited
    template) and the manifest payload is the typed `GmailImportManifest`, still
    written by the import-stage `imports/common.py:write_manifest` — see
    `GmailImport.manifest`. Two key-guessing reads are gone: the people file to
    copy is `self.gmail_people_csv` (one attribute the enrich step sets) instead
    of `state["artifacts"]["gmail_merged_people_csv"] or ...["gmail_people_csv"]
    or ""`, and the two artifact keys that differ by one letter are read through
    `util.gmail_account_queue_records` / `util.gmail_stage_queue_csv`.
    `import/gmail/people.csv` and the directory slice are byte-identical.
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
from packs.ingestion.primitives.discover.gmail.discover import (  # noqa: E402
    GMAIL_ACCOUNT_PEOPLE_CSV,
    GMAIL_ACCOUNT_QUEUE_CSV,
)
from packs.ingestion.primitives.discover.discovery_config import output_path  # noqa: E402
from packs.ingestion.primitives.discover.gmail.models import GmailContactRow  # noqa: E402
from packs.ingestion.primitives.imports.directory import (  # noqa: E402
    GMAIL_DIRECTORY_ROWS,
    DirectoryRow,
)
from packs.ingestion.primitives.imports.gmail.steps.directory import run_gmail_directory  # noqa: E402
from packs.ingestion.primitives.imports.gmail.steps.enrich import run_gmail_apply_and_enrich  # noqa: E402
from packs.ingestion.primitives.common.proc import emit_progress  # noqa: E402
from packs.ingestion.primitives.imports.gmail.util import (  # noqa: E402
    GMAIL_IMPORT_PREFIX,
    gmail_account_queue_records,
    gmail_artifacts_from_discovery,
    gmail_candidate_people,
    gmail_stage_queue_csv,
)
from packs.ingestion.primitives.pipeline.contract import (  # noqa: E402
    Artifact,
    Node,
    PeopleRow,
    StageManifest,
)
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS, normalize_people_row  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402

GMAIL_IMPORT_CONTRACT = "gmail-directory-only-v2"
# Where gmail discovery published the account selection this import reads. Its
# producer declares it as a `manifest`, not an `Artifact`, so the graph checker
# cannot attribute it and reports it as a phantom input — see the PR notes.
GMAIL_DISCOVERY_MANIFEST = DEFAULT_BASE_DIR / "discover" / "gmail" / "manifest.json"


class GmailImportManifest(StageManifest):
    """This stage's typed manifest payload — the pydantic successor to the four
    per-path dicts `run()` used to build inline. Field order IS the completed
    manifest's key order, and the optional fields are dropped when None exactly
    as the old per-path dicts omitted them."""

    # No `stage` field: the import-stage writer already stamps `source`.
    status: str = ""
    reason: str | None = None
    artifact_dir: str = ""
    input: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    stats: dict[str, Any] | None = None
    candidates: dict[str, Any] | None = None
    steps: dict[str, Any] | None = None
    directory_normalization: dict[str, Any] | None = None
    directory_quality: dict[str, Any] | None = None
    artifacts: dict[str, Any] | None = None


class GmailImport(Node):
    """Orchestrates the directory-only Gmail import.

    Owns the fixed import dir, transient run state, the two-step chain
    (directory apply -> stored-resolution apply + people materialization), the
    matched-people / candidates split, the directory source-account quality
    gate, and the import manifest. The step functions (`run_gmail_directory`,
    `run_gmail_apply_and_enrich`, imported from `steps/`) take this orchestrator
    and mutate `self.state` in place instead of threading a state dict around;
    `_mark_step` / `_begin_step` are the status-tracking helpers they call, and
    `self.gmail_people_csv` is the one path the enrich step hands back.

    A step returning False ends the run with status `failed` (steps record their
    own error in transient state); there is no approval gate — nothing here
    spends. `execute()` is that flow; `run()` is the inherited Node template."""

    name = "gmail_import"
    inputs = (
        # The per-account templates gmail discovery DECLARES (imported from it, so
        # both nodes name one path string and the graph draws the real edge).
        # required=False: the templates are unbound here — this import learns its
        # accounts from the discovery manifest below, not from a fixed slug.
        Artifact(path=GMAIL_ACCOUNT_QUEUE_CSV, row_model=GmailContactRow, required=False),
        Artifact(path=GMAIL_ACCOUNT_PEOPLE_CSV, row_model=PeopleRow, required=False),
        # The STAGE-level merged queue (singular key). Read only on the fallback
        # path — when no per-account record survived discovery's schema check,
        # `gmail_queue_records` synthesizes one record over this file and splits it
        # against the directory. Same `output_path` call gmail discovery declares
        # it with, so the graph matches on one string.
        Artifact(
            path=str(output_path("gmail", "linkedin_resolution_queue_csv")),
            row_model=GmailContactRow,
            required=False,
        ),
        # The account selection. required=False because its absence is a handled,
        # reported state: a `skipped` manifest with reason "no Gmail discovery
        # queue" (locked by test_gmail_import_removes_legacy_ledger_and_writes_manifest).
        Artifact(path=str(GMAIL_DISCOVERY_MANIFEST), required=False),
        # A real data dependency, not just the write half: load_directory_lookup
        # reads EVERY source's rows to resolve this queue.
        Artifact(path=str(DEFAULT_DIRECTORY_CSV), row_model=DirectoryRow, required=False),
    )
    outputs = (
        # required=False: copy_people_csv returns "" when no people file was
        # materialized, and the run still completes with `people_csv: ""`.
        Artifact(
            path=str(DEFAULT_IMPORT_DIR / "gmail" / "people.csv"),
            row_model=PeopleRow,
            writes="full_rewrite",
            required=False,
        ),
        # The gmail ROW SLICE of the shared aggregate. `upsert`, not full_rewrite:
        # commit_directory_rows merges by source_key and keeps every row it did
        # not produce. See imports/directory.py for the slice predicates.
        Artifact(
            path=str(DEFAULT_DIRECTORY_CSV),
            row_model=DirectoryRow,
            writes="upsert",
            owns_rows_where=GMAIL_DIRECTORY_ROWS,
        ),
    )
    payload = GmailImportManifest
    # "" is deliberate — same reason as messages: this stage's manifest goes
    # through the IMPORT-stage writer (`imports/common.py:write_manifest`), whose
    # fingerprint chain `import_manifest_current` reads for the no-op gate and
    # which `common/manifests.py` documents as divergent from `write_stage_manifest`
    # on purpose. `execute()` writes it and parks the result on `self.written`.
    manifest = ""

    def __init__(self, *, args: argparse.Namespace, contract: str) -> None:
        self.args = args
        self.contract = contract
        self.import_dir = source_import_dir("gmail")
        self.directory_csv = DEFAULT_DIRECTORY_CSV
        self.people_csv = DEFAULT_IMPORT_DIR / "gmail" / "people.csv"
        self.state: dict[str, Any] = {}
        # The people file the enrich step actually materialized — the merged
        # Gmail artifact, or (when the merge produced no rows) the last account's
        # resolved file. ONE name replaces the old two-key `or` chain.
        self.gmail_people_csv: Path | None = None
        # The manifest dict `write_manifest` produced (it may return the unchanged
        # existing one) — what the CLI emits.
        self.written: dict[str, Any] = {}

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
        emit_progress(message, GMAIL_IMPORT_PREFIX)

    # --- orchestration --------------------------------------------------------

    def bindings(self) -> dict[str, str]:
        """Declared path -> this instance's path. `source_import_dir`,
        `DEFAULT_IMPORT_DIR`, and `DEFAULT_DIRECTORY_CSV` are module defaults the
        tests patch, so the binding is what makes a temp-dir run validate against
        the declaration. The two per-account TEMPLATES stay unbound: this node
        learns its accounts from the discovery manifest, so there is no single
        slug to bind them to (which is why they are `required=False`)."""
        directory_declared = self.inputs[-1].path
        people_declared, directory_output_declared = (item.path for item in self.outputs)
        return {
            directory_declared: str(self.directory_csv),
            people_declared: str(self.people_csv),
            directory_output_declared: str(self.directory_csv),
        }

    def _manifest(self, payload: GmailImportManifest) -> GmailImportManifest:
        """Write this stage's single import manifest, parking the writer's result
        (which may be the unchanged existing manifest) on `self.written`."""
        self.written = write_manifest("gmail", payload.to_payload(), import_dir=DEFAULT_IMPORT_DIR)
        return payload

    def execute(self) -> GmailImportManifest:
        """The whole import: fingerprint no-op check -> build transient state -> the
        two step functions (directory match, then apply + people materialization)
        -> one people.csv + directory quality checks -> the import manifest."""
        args = self.args
        (self.import_dir / "ledger.json").unlink(missing_ok=True)
        expected_input = {
            "pipeline_contract": self.contract,
            "mode": "directory-only",
        }
        current = import_manifest_current("gmail", expected_input, import_dir=DEFAULT_IMPORT_DIR)
        if current and not getattr(args, "force", False):
            # A no-op writes nothing, so there is no manifest body to type — the
            # previous run's manifest IS the answer. Mirror only its status.
            self.written = current
            return GmailImportManifest(status=str(current.get("status") or ""))
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
        # The two artifact keys here differ by ONE LETTER: the per-account records
        # (plural) are what this import iterates; the stage-level queue (singular)
        # existing without them is precisely the "discovery wrote a queue but no
        # usable per-account people.csv" case the second reason names.
        if not gmail_account_queue_records(state["artifacts"]):
            reason = "no Gmail discovery queue"
            status = "skipped"
            if gmail_stage_queue_csv(state["artifacts"]) or state["artifacts"].get("gmail_invalid_discovery_records"):
                reason = "gmail_discovery_missing_per_account_people_csv"
            return self._manifest(GmailImportManifest(
                status=status,
                reason=reason,
                artifact_dir=str(import_dir),
                artifacts=state.get("artifacts", {}),
            ))
        for step in (run_gmail_directory, run_gmail_apply_and_enrich):
            if not step(self):
                return self._manifest(GmailImportManifest(
                    status="failed",
                    artifact_dir=str(import_dir),
                    steps=state.get("steps", {}),
                    artifacts=state.get("artifacts", {}),
                ))
        state["status"] = "completed"
        # The people file to import is the ONE the enrich step materialized, not
        # a guess across two state keys.
        people_csv = copy_people_csv("gmail", str(self.gmail_people_csv or ""), import_dir=DEFAULT_IMPORT_DIR)
        candidates = gmail_candidate_people(state.get("artifacts", {}))
        if people_csv:
            resolved_rows = CsvIO.read_dict_rows(Path(people_csv))
            all_rows = [normalize_people_row(row) for row in resolved_rows] + candidates["people"]
            CsvIO.write_dict_rows(Path(people_csv), PEOPLE_SCHEMA_COLUMNS, all_rows)
        # Pre-#339 leftover: the candidate pool is folded into people.csv now, so
        # this file has no writer. The unlink clears it from existing installs.
        (import_dir / "candidates.csv").unlink(missing_ok=True)
        directory_normalization = normalize_directory_source_accounts("gmail")
        directory_quality = directory_source_account_quality("gmail")
        if directory_quality["status"] != "ok":
            return self._manifest(GmailImportManifest(
                status="failed",
                reason="directory_source_account_quality_failed",
                artifact_dir=str(import_dir),
                outputs={
                    "people_csv": people_csv,
                    "directory_csv": str(self.directory_csv),
                },
                directory_normalization=directory_normalization,
                directory_quality=directory_quality,
                steps=state.get("steps", {}),
                artifacts=state.get("artifacts", {}),
            ))
        return self._manifest(GmailImportManifest(
            status="completed",
            artifact_dir=str(import_dir),
            input={
                **expected_input,
                "discovery_manifest": str(GMAIL_DISCOVERY_MANIFEST),
                "contacts_csv": str(DEFAULT_BASE_DIR / "discover" / "gmail" / "contacts.csv"),
                "linkedin_resolution_queue_csv": str(DEFAULT_BASE_DIR / "discover" / "gmail" / "linkedin_resolution_queue.csv"),
            },
            outputs={
                "people_csv": people_csv,
                "directory_csv": str(self.directory_csv),
            },
            stats={
                "people": csv_count(people_csv),
                "candidates": candidates["candidates"],
            },
            candidates=candidates,
            steps=state.get("steps", {}),
            directory_normalization=directory_normalization,
            directory_quality=directory_quality,
            artifacts=state.get("artifacts", {}),
        ))


def run(args: argparse.Namespace) -> dict:
    """Build and run the `GmailImport` orchestrator for the given CLI args.

    Returns the manifest `write_manifest` produced, not the Node template's typed
    body: that manifest (with its `source`, `fingerprints`, `noop`) is the payload
    the skills and the no-op gate read."""
    imp = GmailImport(args=args, contract=GMAIL_IMPORT_CONTRACT)
    imp.run()
    return imp.written


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
