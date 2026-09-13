#!/usr/bin/env python3
"""Import every discovered Gmail contact as a source candidate.

Selected account people.csv files -> merge metadata by email -> people.csv +
manifest.json. Identity matching and worth decisions belong to Deep Context.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit, read_json  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_IMPORT_DIR  # noqa: E402
from packs.ingestion.primitives.discover.common import read_csv_rows  # noqa: E402
from packs.ingestion.primitives.discover.gmail.discover import (  # noqa: E402
    GMAIL_ACCOUNT_PEOPLE_CSV,
    GMAIL_STAGE_MANIFEST_JSON,
)
from packs.ingestion.primitives.imports.common import import_manifest_current, write_manifest  # noqa: E402
from packs.ingestion.primitives.imports.directory import merge_jsonish_lists  # noqa: E402
from packs.ingestion.primitives.imports.merge_people import merge_group  # noqa: E402
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, PeopleRow, StageManifest  # noqa: E402
from packs.ingestion.schemas.candidates_schema import candidate_key_for  # noqa: E402
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS, normalize_people_row  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402

GMAIL_IMPORT_CONTRACT = "gmail-source-only-v1"


@dataclass(frozen=True)
class _Account:
    email: str
    people_csv: Path


def _read_accounts(manifest_json: Path) -> tuple[_Account, ...]:
    manifest = read_json(manifest_json, {})
    return tuple(sorted(
        (_Account(child["account_email"], Path(child["people_csv"])) for child in manifest.get("children", [])),
        key=lambda account: account.email,
    ))


def _people_from_accounts(accounts: tuple[_Account, ...]) -> list[dict[str, str]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for account in accounts:
        fields, rows = read_csv_rows(account.people_csv)
        if not {"primary_email", "interaction_counts"}.issubset(fields):
            raise ValueError(f"Gmail people schema missing primary_email or interaction_counts: {account.people_csv}")
        for raw in rows:
            row = normalize_people_row(raw)
            key = candidate_key_for(row["primary_email"])
            if not key:
                raise ValueError(f"Gmail source contact has no valid email: {account.people_csv}")
            row["primary_email"] = row["primary_email"].strip().lower()
            row["source_artifacts"] = merge_jsonish_lists(row["source_artifacts"], str(account.people_csv))
            grouped.setdefault(f"candidate:{key}", []).append(row)
    return [merge_group(key, members) for key, members in sorted(grouped.items())]


class GmailImportManifest(StageManifest):
    status: str
    reason: str | None = None
    input: dict[str, Any]
    outputs: dict[str, str]
    stats: dict[str, int]


class GmailImport(Node):
    """Write source candidates without reading or modifying identity decisions."""

    name = "gmail_import"
    inputs = (
        Artifact(path=GMAIL_ACCOUNT_PEOPLE_CSV, row_model=PeopleRow, required=False),
        Artifact(path=GMAIL_STAGE_MANIFEST_JSON, required=False),
    )
    outputs = (
        Artifact(path=str(DEFAULT_IMPORT_DIR / "gmail" / "people.csv"), row_model=PeopleRow, writes="full_rewrite", required=False),
    )
    payload = GmailImportManifest
    # The existing import writer owns fingerprinting and the no-op contract.
    manifest = ""

    def __init__(
        self, *, force: bool = False, manifest_json: Path = Path(GMAIL_STAGE_MANIFEST_JSON),
        import_dir: Path = DEFAULT_IMPORT_DIR,
    ) -> None:
        self.force = force
        self.manifest_json = manifest_json
        self.import_root = import_dir
        self.people_csv = import_dir / "gmail" / "people.csv"
        self.written: dict[str, Any] = {}

    def bindings(self) -> dict[str, str]:
        return {
            GMAIL_STAGE_MANIFEST_JSON: str(self.manifest_json),
            self.outputs[0].path: str(self.people_csv),
        }

    def execute(self) -> GmailImportManifest:
        expected_input = {
            "pipeline_contract": GMAIL_IMPORT_CONTRACT,
            "discovery_manifest": str(self.manifest_json),
        }
        current = import_manifest_current("gmail", expected_input, import_dir=self.import_root)
        if current and not self.force:
            self.written = current
            return GmailImportManifest.model_validate({
                key: current[key] for key in ("status", "input", "outputs", "stats")
            })
        accounts = _read_accounts(self.manifest_json)
        people = _people_from_accounts(accounts)
        if accounts:
            self.people_csv.parent.mkdir(parents=True, exist_ok=True)
            CsvIO.write_dict_rows(self.people_csv, PEOPLE_SCHEMA_COLUMNS, people)
        payload = GmailImportManifest(
            status="completed" if accounts else "skipped",
            reason=None if accounts else "no Gmail discovery accounts",
            input={
                **expected_input,
                "accounts": [{"account_email": account.email, "people_csv": str(account.people_csv)} for account in accounts],
            },
            outputs={"people_csv": str(self.people_csv)} if accounts else {},
            stats={"people": len(people), "candidates": len(people)},
        )
        self.written = write_manifest("gmail", payload.to_payload(), import_dir=self.import_root)
        return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import discovered Gmail contact metadata")
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--force", action="store_true", help="Rebuild even when source inputs are unchanged")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    importer = GmailImport(force=args.force)
    importer.run()
    emit(importer.written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
