#!/usr/bin/env python3
"""Import source Messages contacts into canonical candidate people rows.

Flow: current-output check -> parse contacts -> write people.csv -> manifest.
Identity, worth, and person merging belong to Deep Context.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# Support invocation by file path.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.schemas.message_contacts import MessageContact  # noqa: E402
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS  # noqa: E402
from packs.ingestion.primitives.common.jsonio import emit  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_IMPORT_DIR  # noqa: E402
from packs.ingestion.primitives.discover.common import read_csv_rows, write_csv_rows  # noqa: E402
from packs.ingestion.primitives.discover.messages.models import MessageContactRow  # noqa: E402
from packs.ingestion.primitives.pipeline.contract import (  # noqa: E402
    Artifact,
    Node,
    PeopleRow,
    StageManifest,
)
from packs.ingestion.primitives.imports.common import (  # noqa: E402
    import_manifest_current,
    write_manifest,
)
from packs.ingestion.primitives.imports.messages.util import contact_to_person  # noqa: E402

MESSAGES_IMPORT_CONTRACT = "messages-source-only-v7"
WORKING_CONTACTS_CSV = Path(".powerpacks/messages/contacts.csv")


class MessagesImportManifest(StageManifest):
    """Source import outputs and counts."""

    status: str = ""
    reason: str | None = None
    message: str | None = None
    input: dict[str, Any] = {}
    outputs: dict[str, Any] = {}
    stats: dict[str, int] = {}


class MessagesImport(Node):
    """Retain every keyable source contact without resolving its identity."""

    source = "messages"
    name = "messages_import"
    inputs = (
        Artifact(path=str(WORKING_CONTACTS_CSV), row_model=MessageContactRow, required=False),
    )
    outputs = (
        Artifact(path=str(DEFAULT_IMPORT_DIR / source / "people.csv"), row_model=PeopleRow, writes="full_rewrite"),
    )
    payload = MessagesImportManifest
    # The import writer owns fingerprinting; Node must not overwrite its manifest.
    manifest = ""

    def __init__(
        self,
        *,
        contacts_csv: Path = WORKING_CONTACTS_CSV,
        import_dir: Path = DEFAULT_IMPORT_DIR,
    ) -> None:
        self.import_dir = import_dir / self.source
        self.people_csv = self.import_dir / "people.csv"
        self.contacts_csv = contacts_csv
        self.written: dict[str, Any] = {}
        self.manifest_input = {
            "pipeline_contract": MESSAGES_IMPORT_CONTRACT,
            "contacts_csv": str(self.contacts_csv),
        }

    def bindings(self) -> dict[str, str]:
        return {
            self.inputs[0].path: str(self.contacts_csv),
            self.outputs[0].path: str(self.people_csv),
        }

    def _manifest(self, payload: MessagesImportManifest) -> MessagesImportManifest:
        self.written = write_manifest(
            self.source, payload.to_payload(), import_dir=self.import_dir.parent,
        )
        return payload

    def execute(self) -> MessagesImportManifest:
        current = import_manifest_current(
            self.source, self.manifest_input, import_dir=self.import_dir.parent,
        )
        if current:
            self.written = current
            return MessagesImportManifest(status=current["status"])
        if not self.contacts_csv.exists():
            return self._manifest(MessagesImportManifest(
                status="failed",
                reason="messages_contacts_missing",
                message=(
                    f"Discover Messages contacts before import: {self.contacts_csv}. "
                    "Run: uv run --project . python packs/ingestion/primitives/"
                    "discover/messages/discover.py discover"
                ),
                input=self.manifest_input,
                stats={"people": 0, "candidates": 0},
            ))

        contacts = [
            MessageContact.from_csv_row(row)
            for row in read_csv_rows(self.contacts_csv)[1]
        ]
        people = []
        for contact in contacts:
            person = contact_to_person(contact, self.contacts_csv)
            if person is not None:
                people.append(person)

        self.import_dir.mkdir(parents=True, exist_ok=True)
        write_csv_rows(self.people_csv, PEOPLE_SCHEMA_COLUMNS, people)
        return self._manifest(MessagesImportManifest(
            status="completed",
            input=self.manifest_input,
            outputs={"people_csv": str(self.people_csv)},
            stats={"people": len(people), "candidates": len(people)},
        ))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import source Messages contacts")
    parser.add_argument("command", choices=["run"])
    return parser


def main() -> int:
    build_parser().parse_args()
    importer = MessagesImport()
    importer.run()
    emit(importer.written)
    return 1 if importer.written["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
