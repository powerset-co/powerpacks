#!/usr/bin/env python3
"""Read-only per-source import status report.

Reports, for each fan-in source (gmail, linkedin, messages): whether discovery
ran, whether the import completed (manifest `status: completed` with an
existing `outputs.people_csv`), whether it is still current (fingerprints
match), and row counts — plus the merged people.csv summary. This is the
presence check the import skills use to suggest missing sources. It writes
nothing and always exits 0.

The DISCOVER half reads the stage manifest and nothing else — for gmail and
messages: `status` says whether discovery ran, and the declared output's row
count comes from the per-node IO stats the node recorded
(`fingerprints.output_artifacts[<declared path>].rows`, see
`pipeline/contract.py:Node.artifact_stats`). It used to open a CSV and count
it, and that was the only reason `discover/gmail/contacts.csv` and
`discover/messages/contacts.csv` existed — both are deleted now.

LinkedIn has no discover-stage NODE: its discover-stage artifact is the user's
downloaded `Connections.csv` export sitting in `discover/linkedin/`, and its
people arrive externally through the Modal pipeline into
`import/linkedin/people.csv`. The linkedin discover block therefore reports the
export (present + row count, preamble-aware) and nothing else.

The IMPORT half still counts its two files, deliberately: `import/linkedin/
people.csv` is declared `external=True` (the Modal indexing pipeline downloads it,
no node in this graph writes it) and `candidates.csv` has had no writer since #339
folded the candidate pool into `people.csv`. Neither has a node that could record
a row count, so opening them IS the only honest source.

Changelog:
  2026-07-26 (linkedin discover honesty): the linkedin discover block reports the
    `Connections.csv` export instead of pretending a discover node ran — it used
    to read the fossil discover-dir manifest (last written by a June pipeline;
    before that rework it counted the June `discover/linkedin/contacts.csv`,
    which nothing writes) and report `present: true, status: completed,
    contacts: 0` for a stage that never runs on the user's machine.
    `DISCOVERY_NODES` is gmail + messages only now.
  2026-07-26 (per-node IO stats): `discover_status` reads the manifest's declared
    output stats instead of opening a CSV; the two staged `contacts.csv` files it
    used to count are gone, and the declared path per source is imported from the
    producing node rather than rebuilt here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit, now_iso, read_json  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR, DEFAULT_IMPORT_DIR  # noqa: E402
from packs.ingestion.primitives.discover.gmail.discover import GmailDiscovery  # noqa: E402
from packs.ingestion.primitives.discover.messages.discover import MessagesDiscovery  # noqa: E402
from packs.ingestion.primitives.imports.common import (  # noqa: E402
    csv_count,
    import_manifest_current,
)
from packs.ingestion.primitives.imports.linkedin.network_import import linkedin_export_header  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402


FAN_IN_SOURCES = ["gmail", "linkedin", "messages"]
CANONICAL_MERGED_PEOPLE_CSV = Path(".powerpacks/network-import/merged/people.csv")

# The node that publishes each source's discovery manifest. Its ONE declared
# output is the artifact whose row count IS that source's contact count, and it is
# read from the declaration so this report and the producer name one path string:
# gmail's `linkedin_resolution_queue.csv` and the merged
# `.powerpacks/messages/contacts.csv`. LinkedIn is absent on purpose — it has no
# discover node (see `linkedin_discover_status`).
DISCOVERY_NODES = {
    "gmail": GmailDiscovery,
    "messages": MessagesDiscovery,
}


def linkedin_connections_count(connections_csv: Path) -> int:
    """Data rows of a LinkedIn `Connections.csv` export. The export opens with a
    notes preamble, so the header row is DETECTED (same rule the importer's
    parser uses), never assumed to be the first line."""
    with connections_csv.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            if linkedin_export_header(line):
                return sum(1 for record in CsvIO.reader(handle) if any(cell.strip() for cell in record))
    return 0


def linkedin_discover_status(base_dir: Path) -> dict[str, Any]:
    """LinkedIn's discover-stage artifact is the user's `Connections.csv` export;
    no node runs, so there is no manifest and no contacts count to report. The
    people themselves arrive externally (Modal) into `import/linkedin/people.csv`,
    which the IMPORT half of this report covers."""
    connections_csv = base_dir / "discover" / "linkedin" / "Connections.csv"
    present = connections_csv.is_file()
    return {
        "connections_csv": str(connections_csv),
        "present": present,
        "connections": linkedin_connections_count(connections_csv) if present else 0,
    }


def discover_status(source: str, base_dir: Path) -> dict[str, Any]:
    """This source's discovery state, from its stage manifest ONLY (no file reads
    beyond linkedin's export check)."""
    if source == "linkedin":
        return linkedin_discover_status(base_dir)
    manifest_path = base_dir / "discover" / source / "manifest.json"
    manifest = read_json(manifest_path, {}) or {}
    declared = DISCOVERY_NODES[source].outputs[0].path
    outputs = (manifest.get("fingerprints") or {}).get("output_artifacts") or {}
    stat = outputs.get(declared) if isinstance(outputs, dict) else {}
    stat = stat if isinstance(stat, dict) else {}
    return {
        "manifest": str(manifest_path),
        "present": manifest.get("status") == "completed",
        "status": str(manifest.get("status") or ""),
        "contacts_csv": declared if stat.get("exists") else "",
        "contacts": int(stat.get("rows") or 0),
        "updated_at": str(manifest.get("updated_at") or ""),
    }


def import_status(source: str, import_dir: Path) -> dict[str, Any]:
    manifest_path = import_dir / source / "manifest.json"
    manifest = read_json(manifest_path, {}) or {}
    outputs = manifest.get("outputs") if isinstance(manifest.get("outputs"), dict) else {}
    people_csv = str(outputs.get("people_csv") or "")
    candidates_csv = str(outputs.get("candidates_csv") or "")
    imported = (
        manifest.get("status") == "completed"
        and bool(people_csv)
        and Path(people_csv).exists()
    )
    current = bool(import_manifest_current(source, import_dir=import_dir)) if imported else False
    return {
        "manifest": str(manifest_path),
        "present": bool(manifest),
        "status": str(manifest.get("status") or ""),
        "imported": imported,
        "current": current,
        "people_csv": people_csv if imported else "",
        "people": csv_count(people_csv) if imported else 0,
        "candidates_csv": candidates_csv if candidates_csv and Path(candidates_csv).exists() else "",
        "candidates": csv_count(candidates_csv),
        "updated_at": str(manifest.get("updated_at") or ""),
    }


def merged_status() -> dict[str, Any]:
    return {
        "people_csv": str(CANONICAL_MERGED_PEOPLE_CSV),
        "exists": CANONICAL_MERGED_PEOPLE_CSV.exists(),
        "people": csv_count(str(CANONICAL_MERGED_PEOPLE_CSV)),
    }


def status_payload(sources: list[str]) -> dict[str, Any]:
    return {
        "primitive": "import_contacts_status",
        "status": "ok",
        "sources": {
            source: {
                "discover": discover_status(source, DEFAULT_BASE_DIR),
                "import": import_status(source, DEFAULT_IMPORT_DIR),
            }
            for source in sources
        },
        "merged": merged_status(),
        "updated_at": now_iso(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status"])
    parser.add_argument(
        "--source",
        choices=[*FAN_IN_SOURCES, "all"],
        default="all",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    sources = FAN_IN_SOURCES if args.source == "all" else [args.source]
    emit(status_payload(sources))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
