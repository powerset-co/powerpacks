"""Back up an existing store, restore contact facts and apply scoped feedback.

Reads operator feedback before mutation. Preserves existing human decisions and
original contact histories; applies feedback before imported LinkedIn matching.
Reruns require another unused backup path and reuse the existing paid artifacts.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import shutil
import sqlite3

from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.common.legacy import restore_contact_facts
from packs.ingestion.primitives.common.manifests import write_stage_manifest
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.merge_candidates.linkedin_name_matches import apply_linkedin_name_matches
from packs.ingestion.primitives.deep_context.migration.feedback import apply_feedback, read_feedback, write_feedback_csv
from packs.ingestion.primitives.deep_context.migration.human_decisions import CarryStatus, DecisionResult, HumanSnapshot
from packs.ingestion.primitives.deep_context.shared.common import emit
from packs.ingestion.primitives.deep_context.synthesis.normalization import normalize_parent_cache
from packs.ingestion.primitives.pipeline.contract import StageManifest


class RecoverManifest(StageManifest):
    source: str = "recover"
    state_root: str
    backup_root: str
    operator_id: str
    feedback_snapshot: str
    feedback_csv: str
    feedback_rows: int
    applied: int
    held: int
    unmatched: int
    decisions: tuple[DecisionResult, ...]
    contact_facts_restored: int
    parents_merged: int
    next_command: str
    updated_at: str


class Recover:
    def __init__(self, *, state_root: Path, backup_root: Path, operator_id: str,
                 feedback_json: Path | None = None):
        self.state = Path(state_root).resolve()
        self.backup = Path(backup_root).resolve()
        self.operator_id = operator_id
        self.feedback_json = Path(feedback_json).resolve() if feedback_json is not None else None
        self.deep_context = self.state / "deep-context"
        self.db_path = self.deep_context / "deep-context.sqlite"
        self.facts = self.deep_context / "facts"
        self.raw = self.deep_context / "raw"
        self.manifest = self.deep_context / "recover/manifest.json"
        self.feedback_snapshot = self.deep_context / "recover/feedback.json"
        self.feedback_csv = self.deep_context / "recover/feedback.csv"

    def _backup(self) -> HumanSnapshot:
        with sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True) as original:
            original.execute("BEGIN")
            snapshot = HumanSnapshot.read(original, self.state / "network-import/overrides/review.csv")

            def ignore_database(path: str, names: list[str]) -> tuple[str, ...]:
                return tuple(name for name in names if name in {
                    "deep-context.sqlite", "deep-context.sqlite-wal",
                    "deep-context.sqlite-shm", "deep-context.sqlite-journal",
                }) if Path(path) == self.deep_context else ()

            shutil.copytree(self.state, self.backup, symlinks=True, ignore=ignore_database)
            with sqlite3.connect(self.backup / "deep-context/deep-context.sqlite") as target:
                original.backup(target)
        return snapshot

    def run(self) -> RecoverManifest:
        if self.state.is_relative_to(self.backup) or self.backup.is_relative_to(self.state):
            raise StoreError("state and backup must be disjoint")
        if self.backup.exists():
            raise StoreError("backup path already exists; choose an unused destination, including on reruns")
        if not self.db_path.is_file():
            raise StoreError("existing canonical database is missing")
        if self.deep_context.is_symlink():
            raise StoreError("deep-context must be a local directory so its backup is independent")
        feedback = read_feedback(self.operator_id, feedback_json=self.feedback_json)
        snapshot = self._backup()
        db = Db(self.db_path)
        write_json(self.feedback_snapshot, list(feedback.raw))
        write_feedback_csv(self.feedback_csv, feedback)
        restored = restore_contact_facts(db, self.facts)
        decisions = apply_feedback(db, feedback, snapshot)
        merged = apply_linkedin_name_matches(db)
        # Always normalize: a prior interrupted invocation may have committed joins.
        restored += normalize_parent_cache(db, raw_dir=self.raw, facts_dir=self.facts)
        result = RecoverManifest(
            status="completed", state_root=str(self.state), backup_root=str(self.backup),
            operator_id=feedback.operator_id, feedback_snapshot=str(self.feedback_snapshot),
            feedback_csv=str(self.feedback_csv), feedback_rows=len(feedback.rows),
            applied=sum(row.status == CarryStatus.APPLIED for row in decisions),
            held=sum(row.status == CarryStatus.HELD for row in decisions),
            unmatched=sum(row.status == CarryStatus.UNMATCHED for row in decisions),
            decisions=decisions, contact_facts_restored=restored, parents_merged=merged,
            next_command=(f"bin/deep-context parents --db {shlex.quote(str(self.db_path))} "
                          f"--parents-dir {shlex.quote(str(self.deep_context / 'parents'))}"),
            updated_at=now_iso(),
        )
        write_stage_manifest(self.manifest, result)
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recover existing local facts and scoped operator feedback without paid calls")
    parser.add_argument("--state-root", required=True, type=Path, help="Existing .powerpacks directory")
    parser.add_argument("--backup-root", required=True, type=Path, help="Unused destination for the complete state backup; use a new path on reruns")
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--feedback-json", type=Path, help="Saved operator-scoped GET /v2/feedback response row list; otherwise fetched read-only")
    args = parser.parse_args(argv)
    try:
        result = Recover(**vars(args)).run()
    except (StoreError, OSError, ValueError, sqlite3.Error) as exc:
        emit({"status": "error", "error": str(exc)})
        return 1
    emit(result.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
