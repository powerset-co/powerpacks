"""Project the live imported-person roster into stable SQLite parent families.

Changelog:
- 2026-10-02: preserve original contacts from the fan-in's recorded source CSVs.
- 2026-09-25: the CLI creates the canonical store when it is missing; this is
  the first cold step, so nothing else has to create it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.common.legacy import scrub_august_deep_context_store, scrub_deep_context
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    DEFAULT_PEOPLE_CSV,
    emit,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import (
    project_imported_people,
    read_imported_people,
)
from packs.ingestion.primitives.deep_context.ensure_parents.source_people import (
    project_source_people,
    read_source_people,
    retain_source_identifiers,
)
from packs.ingestion.primitives.deep_context.manifests.ensure_parents_manifest import (
    EnsureParentsManifest,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node


class EnsureParents(Node):
    """Get-or-create stable parents for every row in the current fan-in export."""

    name = "deep_ensure_parents"
    inputs = (
        Artifact(path=str(DEFAULT_PEOPLE_CSV), external=True, required=False),
        Artifact(path=str(DEFAULT_PEOPLE_CSV.parent / "manifest.json"), external=True, required=False),
        Artifact(path=str(CANONICAL_DB), external=True),
    )
    outputs = ()
    payload = EnsureParentsManifest
    manifest = ""

    def __init__(self, *, db: Db, people_csv: Path = DEFAULT_PEOPLE_CSV) -> None:
        self.db = db
        self.people_csv = Path(people_csv)

    def bindings(self) -> dict[str, str]:
        return {
            str(DEFAULT_PEOPLE_CSV): str(self.people_csv),
            str(DEFAULT_PEOPLE_CSV.parent / "manifest.json"): str(self.people_csv.parent / "manifest.json"),
            str(CANONICAL_DB): str(self.db.db_path),
        }

    def execute(self) -> EnsureParentsManifest:
        imported = read_imported_people(self.people_csv)
        sources = read_source_people(self.people_csv)
        repair, removed, historical = scrub_deep_context(self.db)
        if removed:
            print(f'[deep-context] invalidated {removed} Harmonic profile artifacts', file=sys.stderr)
        if repair.repaired or repair.unresolved:
            print(f'[deep-context] repaired {len(repair.repaired)} merged parents; '
                  f'{len(repair.unresolved)} unresolved', file=sys.stderr)
        if historical.repaired or historical.unresolved:
            print(f'[deep-context] restored {len(historical.repaired)} historical merged parents; '
                  f'{len(historical.unresolved)} unresolved', file=sys.stderr)
        project_source_people(self.db, sources, imported)
        projected = project_imported_people(self.db, imported)
        retain_source_identifiers(self.db, sources)
        return EnsureParentsManifest(
            status="completed",
            people_projected=projected,
            updated_at=now_iso(),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project imported people into stable SQLite parent families.")
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scrub_august_deep_context_store(Path(args.db))
    payload = EnsureParents(
        db=Db(Path(args.db)),
        people_csv=Path(args.people_csv),
    ).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
