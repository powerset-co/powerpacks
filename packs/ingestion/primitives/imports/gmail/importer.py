#!/usr/bin/env python3
"""Import every discovered Gmail contact as a source candidate.

Selected account people.csv files -> source names, email and interactions ->
merge by email -> people.csv + manifest.json. Identity matching and worth
decisions belong to Deep Context.

Changelog:
  2026-09-23 (typed rows): account contacts are read as `PeopleRow` and handed to
    `merge_group` typed; the only raw `.get` left is the discovery children
    manifest parse that owns that external input.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit, read_json  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_IMPORT_DIR  # noqa: E402
from packs.ingestion.primitives.discover.gmail.discover import (  # noqa: E402
    GMAIL_ACCOUNT_PEOPLE_CSV,
    GMAIL_STAGE_MANIFEST_JSON,
)
from packs.ingestion.primitives.imports.common import import_manifest_current, write_manifest  # noqa: E402
from packs.ingestion.primitives.imports.merge_people import merge_group  # noqa: E402
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, PeopleRow, StageManifest  # noqa: E402
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402

from packs.ingestion.primitives.imports.gmail.source_people import (  # noqa: E402
    GMAIL_IMPORT_CONTRACT, GmailAccount, source_people_from_accounts,
)


def _read_accounts(manifest_json: Path) -> tuple[GmailAccount, ...]:
    manifest = read_json(manifest_json, {})
    return tuple(sorted(
        (GmailAccount(child["account_email"], Path(child["people_csv"])) for child in manifest.get("children", [])),
        key=lambda account: account.account_email,
    ))


def _people_from_accounts(accounts: tuple[GmailAccount, ...]) -> list[dict[str, str]]:
    grouped: dict[str, list[PeopleRow]] = {}
    for row in source_people_from_accounts(accounts):
        grouped.setdefault(row.id, []).append(row)
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
            self.written = current.to_payload()
            return GmailImportManifest.model_validate({
                "status": current.status,
                "input": current.input,
                "outputs": current.outputs,
                "stats": current.stats,
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
                "accounts": [{"account_email": account.account_email, "people_csv": str(account.people_csv)} for account in accounts],
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
