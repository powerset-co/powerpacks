"""Back up an existing store, restore contact facts and apply scoped feedback.

Version 4 restores facts and applies feedback before imported LinkedIn matching.
Version 5 repairs empty Gmail shells and preserves original parents and history.
Runs pending data migrations in order; completed versions are read-only no-ops.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sqlite3

from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.common.legacy import repair_gmail_contact_keys, restore_contact_facts, scrub_deep_context
from packs.ingestion.primitives.common.manifests import write_stage_manifest
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.ensure_parents.source_people import read_source_people
from packs.ingestion.primitives.deep_context.merge_candidates.linkedin_name_matches import apply_linkedin_name_matches
from packs.ingestion.primitives.deep_context.migration.feedback import apply_feedback, read_feedback, write_feedback_csv
from packs.ingestion.primitives.deep_context.migration.human_decisions import CarryStatus, DecisionResult, HumanSnapshot
from packs.ingestion.primitives.deep_context.shared.common import emit
from packs.ingestion.primitives.deep_context.synthesis.normalization import normalize_parent_cache
from packs.ingestion.primitives.pipeline.contract import StageManifest


HEAL_MIGRATION_VERSION = 5


class HealManifest(StageManifest):
    source: str = "heal"
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
    updated_at: str


class Heal:
    def __init__(self, *, state_root: Path, backup_root: Path, operator_id: str,
                 feedback_json: Path | None = None):
        self.state = Path(state_root).resolve()
        self.backup = Path(backup_root).resolve()
        self.operator_id = operator_id
        self.feedback_json = Path(feedback_json).resolve() if feedback_json is not None else None
        self.deep_context = self.state / "deep-context"
        self.db_path = self.deep_context / "deep-context.sqlite"
        self.people_csv = self.state / "network-import/merged/people.csv"
        self.facts = self.deep_context / "facts"
        self.raw = self.deep_context / "raw"
        self.manifest = self.deep_context / "heal/manifest.json"
        self.feedback_snapshot = self.deep_context / "heal/feedback.json"
        self.feedback_csv = self.deep_context / "heal/feedback.csv"

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

    def run(self) -> HealManifest:
        if not self.db_path.is_file():
            raise StoreError("existing canonical database is missing")
        with sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True) as original:
            version = original.execute("SELECT value FROM meta WHERE key='data_migration_version'").fetchone()
        applied_version = int(version[0]) if version else 0
        if applied_version >= HEAL_MIGRATION_VERSION:
            return HealManifest(
                status="skipped", state_root=str(self.state), backup_root=str(self.backup),
                operator_id=self.operator_id, feedback_snapshot=str(self.feedback_snapshot),
                feedback_csv=str(self.feedback_csv), feedback_rows=0,
                applied=0, held=0, unmatched=0, decisions=(),
                contact_facts_restored=0, parents_merged=0, updated_at=now_iso(),
            )
        source_manifest = self.people_csv.with_name("manifest.json")
        if not source_manifest.is_file() or json.loads(source_manifest.read_text()).get("stage") != "merge_people":
            raise StoreError("run fan-in before heal so Gmail key repair can read the original source IDs")
        if self.state.is_relative_to(self.backup) or self.backup.is_relative_to(self.state):
            raise StoreError("state and backup must be disjoint")
        if self.backup.exists():
            raise StoreError("backup path already exists; choose an unused destination for the pending migration")
        for directory in (self.deep_context, self.facts, self.raw, self.manifest.parent,
                          self.facts / "seed", self.facts / "parents", self.raw / "parents"):
            if directory.is_symlink():
                raise StoreError(f"recovery output directory must not be symlinked: {directory}")
        for directory in (self.manifest.parent, self.facts / "seed",
                          self.facts / "parents", self.raw / "parents"):
            if any(path.is_symlink() for path in directory.glob("*")):
                raise StoreError(f"recovery output files must not be symlinked: {directory}")
        feedback = read_feedback(self.operator_id, feedback_json=self.feedback_json) if applied_version < 4 else None
        snapshot = self._backup()
        db = Db(self.db_path)
        restored = merged = 0
        decisions = ()
        if feedback is not None:
            scrub_deep_context(db)
            restored = restore_contact_facts(db, self.facts)
            write_json(self.feedback_snapshot, list(feedback.raw))
            write_feedback_csv(self.feedback_csv, feedback)
            decisions = apply_feedback(db, feedback, snapshot)
            merged = apply_linkedin_name_matches(db)
            restored += normalize_parent_cache(db, raw_dir=self.raw, facts_dir=self.facts)
            with db.transaction() as conn:
                conn.execute("INSERT INTO meta(key,value) VALUES ('data_migration_version','4') "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        sources, rekeys = read_source_people(self.people_csv, db)
        repair_gmail_contact_keys(db, rekeys, retained_ids=frozenset(person.person_id for person in sources))
        with db.transaction() as conn:
            conn.execute("INSERT INTO meta(key,value) VALUES ('data_migration_version',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (str(HEAL_MIGRATION_VERSION),))
        result = HealManifest(
            status="completed", state_root=str(self.state), backup_root=str(self.backup),
            operator_id=self.operator_id, feedback_snapshot=str(self.feedback_snapshot),
            feedback_csv=str(self.feedback_csv), feedback_rows=len(feedback.rows) if feedback is not None else 0,
            applied=sum(row.status == CarryStatus.APPLIED for row in decisions),
            held=sum(row.status == CarryStatus.HELD for row in decisions),
            unmatched=sum(row.status == CarryStatus.UNMATCHED for row in decisions),
            decisions=decisions, contact_facts_restored=restored, parents_merged=merged,
            updated_at=now_iso(),
        )
        write_stage_manifest(self.manifest, result)
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Heal existing local facts and scoped operator feedback without paid calls")
    parser.add_argument("--state-root", required=True, type=Path, help="Existing .powerpacks directory")
    parser.add_argument("--backup-root", required=True, type=Path, help="Unused destination for a pending migration backup; completed migrations skip backup")
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--feedback-json", type=Path, help="Saved operator-scoped GET /v2/feedback response row list; otherwise fetched read-only")
    args = parser.parse_args(argv)
    try:
        result = Heal(**vars(args)).run()
    except (StoreError, OSError, ValueError, sqlite3.Error) as exc:
        emit({"status": "error", "error": str(exc)})
        return 1
    emit(result.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
