"""Project the live imported-person roster into stable SQLite parent families."""

from __future__ import annotations

import argparse
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    DEFAULT_PEOPLE_CSV,
    emit,
)
from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.imported_people import (
    project_imported_people,
    read_imported_people,
)
from packs.ingestion.primitives.pipeline.contract import (
    Artifact,
    Node,
    StageManifest,
)


class EnsureParentsManifest(StageManifest):
    source: str = "ensure_parents"
    people_projected: int = 0
    updated_at: IsoTimestamp | None = None


class EnsureParents(Node):
    """Get-or-create stable parents for every row in the current fan-in export."""

    name = "deep_ensure_parents"
    inputs = (
        Artifact(path=str(DEFAULT_PEOPLE_CSV), external=True),
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
            str(CANONICAL_DB): str(self.db.db_path),
        }

    def execute(self) -> EnsureParentsManifest:
        imported = read_imported_people(self.people_csv)
        projected = project_imported_people(self.db, imported)
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
    payload = EnsureParents(
        db=open_existing_db(args.db),
        people_csv=Path(args.people_csv),
    ).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
