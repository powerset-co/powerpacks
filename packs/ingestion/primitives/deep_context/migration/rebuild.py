"""Back up original state and prepare a fresh source graph with scoped human decisions.

Reads original state and newly regenerated source imports. Writes only the explicit
backup and isolated state; never seeds facts, research or machine decisions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sqlite3

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.common.manifests import write_stage_manifest
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import ImportedPerson, project_imported_people
from packs.ingestion.primitives.deep_context.ensure_parents.source_people import read_source_people
from packs.ingestion.primitives.deep_context.migration.human_decisions import (
    CarryStatus, DecisionResult, HumanSnapshot, carry_human_decisions,
)
from packs.ingestion.primitives.deep_context.shared.build_owner import BuildOwner, _owner_from_payload
from packs.ingestion.primitives.deep_context.shared.common import emit
from packs.ingestion.primitives.imports.merge_people import MergePeopleInput, MergePeopleStats
from packs.ingestion.primitives.imports.gmail.source_people import original_source_accounts, source_people_from_accounts
from packs.ingestion.primitives.pipeline.contract import PeopleRow, StageManifest
from packs.shared.csv_io import CsvIO


class RebuildManifest(StageManifest):
    source: str = "rebuild"
    original_state_root: str
    backup_root: str
    state_root: str
    people_csv: str
    owner_profile: str
    contacts: int
    applied: int
    held: int
    unmatched: int
    excluded_generated: int
    excluded_machine: int
    decisions: tuple[DecisionResult, ...]
    updated_at: str


class Rebuild:
    def __init__(self, *, original_state_root: Path, backup_root: Path, state_root: Path,
                 people_csv: Path, owner_profile: Path):
        self.original = Path(original_state_root).resolve()
        self.backup = Path(backup_root).resolve()
        self.state = Path(state_root).resolve()
        self.people_csv = Path(people_csv).resolve()
        self.owner_profile = Path(owner_profile).resolve()
        self.deep_context = self.state / "deep-context"
        self.db_path = self.deep_context / "deep-context.sqlite"
        self.original_db = self.original / "deep-context/deep-context.sqlite"
        self.facts = self.deep_context / "facts"
        self.raw = self.deep_context / "raw"
        self.research = self.deep_context / "reconcile/deep-research"
        self.manifest = self.deep_context / "rebuild/manifest.json"

    def _validate(self) -> tuple[ImportedPerson, ...]:
        roots = (self.original, self.backup, self.state)
        if any(first.is_relative_to(second) for first in roots for second in roots if first != second) or len(set(roots)) != 3:
            raise StoreError("original, backup and new state must be disjoint")
        if not self.original_db.is_file():
            raise StoreError("original canonical database is missing")
        if self.backup.exists():
            raise StoreError("backup path already exists; choose an unused destination")
        for path in (self.deep_context, self.state / "network-import/directory.csv",
                     self.state / "network-import/overrides", self.state / "network-import/profile_cache_v2"):
            if path.exists() and (not path.is_dir() or any(path.iterdir())):
                raise StoreError(f"new state contains existing derived state: {path}")
        owner = json.loads(self.owner_profile.read_text())
        if not isinstance(owner, dict) or not owner.get("name"):
            raise StoreError("explicit owner profile must contain the reviewed owner's name")
        try:
            _owner_from_payload(owner)
        except (TypeError, ValueError, AttributeError) as exc:
            raise StoreError("explicit owner profile has invalid fields") from exc
        if not self.people_csv.is_file():
            raise StoreError("fresh fan-in people CSV is missing")
        manifest_path = self.people_csv.with_name("manifest.json")
        if not manifest_path.is_file():
            raise StoreError("fresh fan-in source manifest is missing")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("stage") != "merge_people" or manifest.get("primitive") == "deep_context_export_people":
            raise StoreError("fresh fan-in must reference regenerated source imports")
        inputs = MergePeopleInput.model_validate(manifest.get("input"))
        counts = MergePeopleStats.model_validate(manifest.get("stats")).input_rows
        if not inputs.people_csvs or set(inputs.people_csvs) != set(counts):
            raise StoreError("fresh fan-in source accounting is incomplete")
        for value in inputs.people_csvs:
            path = Path(value).resolve()
            if path.is_relative_to(self.original) or path.is_relative_to(self.backup):
                raise StoreError("fresh source imports must not come from original state or backup")
            if not path.is_file():
                raise StoreError(f"fresh source import is missing: {path}")
            accounts = original_source_accounts(path)
            if accounts is None:
                rows = tuple(PeopleRow.model_validate(row) for row in CsvIO.read_dict_rows(path))
            else:
                if any(account.people_csv.resolve().is_relative_to(root)
                       for account in accounts for root in (self.original, self.backup)):
                    raise StoreError("fresh Gmail account inputs must not come from original state or backup")
                rows = source_people_from_accounts(accounts)
            if len(rows) != counts[value]:
                raise StoreError(f"fresh source row count changed: {path}")
            if any(row.source_channels != "linkedin_csv" and
                   (not row.id.startswith("candidate:") or row.public_identifier or row.linkedin_url)
                   for row in rows):
                raise StoreError(f"source import contains old profile associations: {path}")
        # The export fallback is excluded above, so this reader does not use a DB.
        sources = read_source_people(self.people_csv, None)
        if not sources:
            raise StoreError("fresh sources contain no eligible contacts")
        return sources

    def run(self) -> RebuildManifest:
        sources = self._validate()
        with sqlite3.connect(f"{self.original_db.as_uri()}?mode=ro", uri=True) as original:
            original.execute("BEGIN")
            snapshot = HumanSnapshot.read(original, self.original / "network-import/overrides/review.csv")
            def ignore_database(path: str, names: list[str]) -> tuple[str, ...]:
                return tuple(name for name in names if name in {
                    "deep-context.sqlite", "deep-context.sqlite-wal", "deep-context.sqlite-shm", "deep-context.sqlite-journal",
                }) if Path(path) == self.original_db.parent else ()

            shutil.copytree(self.original, self.backup, symlinks=True, ignore=ignore_database)
            backup_db = self.backup / "deep-context/deep-context.sqlite"
            backup_db.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(backup_db) as target:
                original.backup(target)
        for path in (self.facts, self.raw, self.research):
            path.mkdir(parents=True, exist_ok=True)
        db = Db(self.db_path)
        contacts = project_imported_people(db, sources)
        owner_out = self.deep_context / "owner.json"
        shutil.copy2(self.owner_profile, owner_out)
        owner = BuildOwner(db=db, out=owner_out).execute()
        if owner.status != "exists":
            raise StoreError(owner.error or "explicit owner profile could not be projected")
        decisions, generated = carry_human_decisions(db, snapshot)
        result = RebuildManifest(
            status="completed", original_state_root=str(self.original), backup_root=str(self.backup),
            state_root=str(self.state), people_csv=str(self.people_csv), owner_profile=str(self.owner_profile),
            contacts=contacts, applied=sum(row.status == CarryStatus.APPLIED for row in decisions),
            held=sum(row.status == CarryStatus.HELD for row in decisions),
            unmatched=sum(row.status == CarryStatus.UNMATCHED for row in decisions),
            excluded_generated=generated, excluded_machine=snapshot.excluded_machine,
            decisions=decisions, updated_at=now_iso(),
        )
        write_stage_manifest(self.manifest, result)
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare an isolated fresh graph with precisely scoped human decisions")
    parser.add_argument("--original-state-root", required=True, type=Path)
    parser.add_argument("--backup-root", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--people-csv", required=True, type=Path)
    parser.add_argument("--owner-profile", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = Rebuild(**vars(args)).run()
    except (StoreError, OSError, ValueError, sqlite3.Error) as exc:
        emit({"status": "error", "error": str(exc)})
        return 1
    emit(result.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
