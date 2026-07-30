#!/usr/bin/env python3
"""Import discovered Gmail contacts (directory-only — the only mode).

Free and local: apply the shared identity directory to the discovered Gmail
queues and materialize `import/gmail/people.csv` (matched people + the
research-candidates pool folded in). Resolution + enrichment live in
deep-context.

THE gmail import entry. Owns the `GmailImport` orchestrator and the CLI surface
(`run` / `--force`) plus `GMAIL_IMPORT_CONTRACT` (re-exported by the package
__init__). The flow is a straight composition of returned values:

  legacy scrub -> manifest no-op check -> parse discovery (`GmailDiscovery`)
  -> directory step (returns `QueueSplit`s) -> apply step (returns
  `ApplyOutcome`) -> people.csv + candidates fold-in -> quality gate
  -> the import manifest, rendered from those returns.

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
  2026-07-28 (typed steps; style pass, no logic change): the transient
    `self.state` blob is gone. Steps take typed inputs and RETURN typed results
    (`QueueSplit`, `ApplyOutcome`); the manifest is rendered from those returns
    at the end. Legacy unlinks moved to `common/legacy.py:scrub_gmail_import`
    (the one module allowed to know legacy shapes). The manifest `artifacts`
    block now carries only keys with a reader; the write-only bookkeeping
    duplicates died with the blob. Declared outputs are byte-identical.
  2026-07-26 (declaration owns the path): the discovery paths this import reads
    are gmail discovery's declared constants instead of local rebuilds of the
    same strings.
  2026-07-25 (declared contract): `GmailImport` is a `pipeline/contract.py`
    `Node`; the manifest payload is the typed `GmailImportManifest`, written by
    the import-stage `imports/common.py:write_manifest`.
  2026-07-23 (steps split / oop / audit): `import_steps.py` and its file-loader
    died; the flow folded into the `GmailImport` orchestrator with step bodies
    in `steps/`.
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
from packs.ingestion.primitives.common.legacy import scrub_gmail_import  # noqa: E402
from packs.ingestion.primitives.common.paths import (  # noqa: E402
    DEFAULT_DIRECTORY_CSV,
    DEFAULT_IMPORT_DIR,
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
    GMAIL_STAGE_MANIFEST_JSON,
    GMAIL_STAGE_QUEUE_CSV,
)
from packs.ingestion.primitives.discover.gmail.models import GmailContactRow  # noqa: E402
from packs.ingestion.primitives.imports.directory import (  # noqa: E402
    GMAIL_DIRECTORY_ROWS,
    DirectoryRow,
)
from packs.ingestion.primitives.imports.gmail.steps.directory import (  # noqa: E402
    QueueSplit,
    run_gmail_directory,
)
from packs.ingestion.primitives.imports.gmail.steps.apply import (  # noqa: E402
    ApplyOutcome,
    run_gmail_apply,
)
from packs.ingestion.primitives.common.proc import emit_progress  # noqa: E402
from packs.ingestion.primitives.imports.gmail.util import (  # noqa: E402
    GMAIL_IMPORT_PREFIX,
    GmailDiscovery,
    candidate_people,
    discovery_from_manifest,
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
# Where gmail discovery published the account selection this import reads — its
# DECLARED manifest path, imported from the producer. The graph checker attributes
# a node's declared `manifest` to that node, so this is a real edge, not a phantom.
GMAIL_DISCOVERY_MANIFEST = Path(GMAIL_STAGE_MANIFEST_JSON)


class GmailImportManifest(StageManifest):
    """This stage's typed manifest payload. Field order IS the completed
    manifest's key order, and the optional fields are dropped when None."""

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

    Owns the fixed import dir, the two-step chain (directory apply ->
    stored-resolution apply + people materialization), the matched-people /
    candidates fold-in, the directory source-account quality gate, and the
    import manifest. Steps take typed inputs and return typed results;
    `self.steps` is the per-step status record rendered into the manifest, and
    `self.gmail_people_csv` is the one path the apply step hands back.

    `execute()` is the flow; `run()` is the inherited Node template."""

    name = "gmail_import"
    inputs = (
        # The per-account templates gmail discovery DECLARES (imported from it, so
        # both nodes name one path string and the graph draws the real edge).
        # required=False: the templates are unbound here — this import learns its
        # accounts from the discovery manifest below, not from a fixed slug.
        Artifact(path=GMAIL_ACCOUNT_QUEUE_CSV, row_model=GmailContactRow, required=False),
        Artifact(path=GMAIL_ACCOUNT_PEOPLE_CSV, row_model=PeopleRow, required=False),
        # The STAGE-level merged queue (singular). Its existence without any
        # usable per-account record is precisely the skip reason
        # `gmail_discovery_missing_per_account_people_csv` names. The same
        # declared constant gmail discovery names, so the graph matches on one
        # string.
        Artifact(path=GMAIL_STAGE_QUEUE_CSV, row_model=GmailContactRow, required=False),
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
        # Per-step status records rendered into the manifest's `steps` block.
        self.steps: dict[str, dict[str, Any]] = {}
        # The people file the apply step actually materialized — the merged
        # Gmail artifact, or (when the merge produced no rows) the last
        # account's resolved file.
        self.gmail_people_csv: Path | None = None
        # The manifest dict `write_manifest` produced (it may return the unchanged
        # existing one) — what the CLI emits.
        self.written: dict[str, Any] = {}

    # --- step bookkeeping -----------------------------------------------------

    def _mark_step(self, step: str, status: str, **extra: Any) -> None:
        """Update one step's status/timestamps in the manifest's steps block."""
        rec = self.steps.setdefault(step, {"id": step})
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

    def _rendered_artifacts(
        self,
        discovery: GmailDiscovery,
        splits: list[QueueSplit] | None = None,
        outcome: ApplyOutcome | None = None,
    ) -> dict[str, Any]:
        """The manifest's `artifacts` block, rendered from the typed returns.
        Only keys with a reader — the old write-only bookkeeping duplicates
        died with the state blob."""
        rendered: dict[str, Any] = {"directory_csv": str(self.directory_csv)}
        if discovery.stage_queue_csv:
            rendered["gmail_linkedin_resolution_queue_csv"] = discovery.stage_queue_csv
        if discovery.accounts:
            rendered["gmail_accounts"] = [
                {"account_email": a.email, "slug": a.slug, "queue_csv": a.queue_csv, "people_csv": a.people_csv}
                for a in discovery.accounts
            ]
        if discovery.invalid:
            rendered["gmail_invalid_discovery_records"] = [
                {"account_email": i.email, "people_csv": i.people_csv, "queue_csv": i.queue_csv, "reason": i.reason}
                for i in discovery.invalid
            ]
        if splits:
            rendered["gmail_directory_by_slug"] = {s.account.slug: s.to_result() for s in splits}
        if outcome:
            if outcome.results:
                # Fingerprint parity with the pre-refactor artifacts blob: the
                # per-slug apply payloads carry WRITTEN file paths (applied_csv,
                # resolved people.csv) that the no-op gate must keep watching —
                # dropping them made a deleted applied CSV look "up to date".
                rendered["gmail_apply_enrich_by_slug"] = {
                    result["slug"]: result for result in outcome.results
                }
            if outcome.combined:
                rendered["gmail_combined_resolutions_csvs"] = [
                    {"account_email": r.account_email, "slug": r.slug, "people_csv": r.people_csv,
                     "resolutions_csv": r.resolutions_csv,
                     "resolution_sources": list(r.resolution_sources), "resolved": r.resolved}
                    for r in outcome.combined
                ]
            if outcome.merge:
                rendered["gmail_merged_people"] = outcome.merge
        if self.gmail_people_csv:
            rendered["gmail_people_csv"] = str(self.gmail_people_csv)
        return rendered

    def execute(self) -> GmailImportManifest:
        """The whole import: legacy scrub -> fingerprint no-op check -> parse
        discovery -> the two steps -> people.csv + candidates fold-in ->
        directory quality gate -> the import manifest."""
        args = self.args
        scrub_gmail_import(self.import_dir)
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
        discovery = discovery_from_manifest()
        if not discovery.accounts:
            # No account survived with both a queue and a schema-valid
            # people.csv. A stage queue or invalid children existing means
            # discovery ran but produced no usable per-account people.csv.
            reason = "no Gmail discovery queue"
            if discovery.stage_queue_csv or discovery.invalid:
                reason = "gmail_discovery_missing_per_account_people_csv"
            return self._manifest(GmailImportManifest(
                status="skipped",
                reason=reason,
                artifact_dir=str(self.import_dir),
                artifacts=self._rendered_artifacts(discovery),
            ))
        splits = run_gmail_directory(self, list(discovery.accounts))
        outcome = run_gmail_apply(self, splits, discovery.people_accounts)
        if not outcome.ok:
            return self._manifest(GmailImportManifest(
                status="failed",
                artifact_dir=str(self.import_dir),
                steps=self.steps,
                artifacts=self._rendered_artifacts(discovery, splits, outcome),
            ))
        people_csv = copy_people_csv("gmail", str(self.gmail_people_csv or ""), import_dir=DEFAULT_IMPORT_DIR)
        candidates = candidate_people(
            [s.unresolved_csv for s in splits if s.unresolved > 0],
            [s.cached_negative_csv for s in splits if s.cached_negative > 0],
        )
        if people_csv:
            resolved_rows = CsvIO.read_dict_rows(Path(people_csv))
            all_rows = [normalize_people_row(row) for row in resolved_rows] + candidates["people"]
            CsvIO.write_dict_rows(Path(people_csv), PEOPLE_SCHEMA_COLUMNS, all_rows)
        directory_normalization = normalize_directory_source_accounts("gmail")
        directory_quality = directory_source_account_quality("gmail")
        if directory_quality["status"] != "ok":
            return self._manifest(GmailImportManifest(
                status="failed",
                reason="directory_source_account_quality_failed",
                artifact_dir=str(self.import_dir),
                outputs={
                    "people_csv": people_csv,
                    "directory_csv": str(self.directory_csv),
                },
                directory_normalization=directory_normalization,
                directory_quality=directory_quality,
                steps=self.steps,
                artifacts=self._rendered_artifacts(discovery, splits, outcome),
            ))
        return self._manifest(GmailImportManifest(
            status="completed",
            artifact_dir=str(self.import_dir),
            input={
                **expected_input,
                "discovery_manifest": str(GMAIL_DISCOVERY_MANIFEST),
                "linkedin_resolution_queue_csv": GMAIL_STAGE_QUEUE_CSV,
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
            steps=self.steps,
            directory_normalization=directory_normalization,
            directory_quality=directory_quality,
            artifacts=self._rendered_artifacts(discovery, splits, outcome),
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
