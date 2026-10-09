"""Block 01 Import load: the per-source import CSVs and owner.json into the v2 store.

Gmail rows become email candidates and messages rows become phone candidates, each with its
names, its one identifier and its channels; the id is minted from the identifier (candidate:email:<address>,
candidate:phone:+<digits>). A Gmail candidate's name is the one most often written
for its address in the mail archive's headers (gmail_names.py); the importer's own name column is
blank whenever two header spellings ever disagreed, which lost the name of people with hundreds of
messages. The two files never share an id. LinkedIn rows
become the `connections` lookup, never candidates. Shared mailboxes (role addresses such as
office@ or billing@) are dropped here; every other keep rule ran in the per-source importer.
owner.json fills the `owner` row and flags the operator's own candidates.

Nothing is deleted. A candidate row (with its one written name) upserts on its id; identifiers and
sources insert on their primary keys and are ignored when present. A rebuild is the store moved to
.bkup and a rerun.

Created: 2026-10-06
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from packs.ingestion.primitives.common.contact_fields import is_role_address, normalize_email, normalize_phone
from packs.ingestion.primitives.common.paths import DEFAULT_MSGVAULT_DB
from packs.ingestion.primitives.deep_context_v2.db import queries
from packs.ingestion.primitives.deep_context_v2.db.import_row import ImportRow
from packs.ingestion.primitives.deep_context_v2.db.queries import ConnectionRow
from packs.ingestion.primitives.deep_context_v2.db.owner import OwnerProfile, load_owner
from packs.ingestion.primitives.deep_context_v2.db.schema import IdentifierKind, SourceChannel
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.import_load.gmail_names import header_names
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.shared.csv_io import CsvIO

# The importers' outputs, under <data_root>/network-import/import/.
IMPORT_DIR = Path("network-import") / "import"
GMAIL_CSV = IMPORT_DIR / "gmail" / "people.csv"        # one email per row, ids candidate:email:<address>
MESSAGES_CSV = IMPORT_DIR / "messages" / "people.csv"  # one phone per row, ids candidate:phone:+<digits>
CONNECTIONS_CSV = IMPORT_DIR / "linkedin" / "people.csv"
OWNER_JSON = Path("deep-context") / "owner.json"


@dataclass(frozen=True)
class Candidate:
    """One import row, ready to write: the candidate row, its names, its identifier, its channels."""

    candidate_id: str
    display_name: str   # the one written name: the header name for a Gmail candidate, the CSV name for a phone
    is_owner: bool
    import_json: str
    kind: IdentifierKind
    normalized: str
    display: str
    sources: tuple[SourceChannel, ...]


@dataclass(frozen=True)
class GmailImport:
    candidates: list[Candidate]
    shared_mailboxes_dropped: int


def _candidate(cells: dict[str, str], row: ImportRow, kind: IdentifierKind, normalized: str, display: str,
               owner: OwnerProfile, header_name: str = "") -> Candidate:
    """What both passes share: the name, channels, the owner flag, and the cells kept whole as evidence."""
    # One written name. A Gmail candidate's is the one its headers wrote most often; a phone
    # candidate's is the CSV name. The CSV's first/last columns are not used: the importer splits
    # "Last, First" names wrongly and a second, broken name would block every pair.
    full_name: str = header_name or row.full_name.strip()
    return Candidate(
        # Minted here from the identifier, never taken from the CSV: an older importer wrote other ids.
        candidate_id="candidate:" + kind.value + ":" + normalized,
        display_name=full_name,
        # The operator's own addresses and numbers; those candidates are never collected or synthesized.
        is_owner=normalized in owner.emails or normalized in owner.phones,
        import_json=json.dumps(cells, ensure_ascii=False),
        kind=kind,
        normalized=normalized,
        display=display,
        sources=row.source_channels,
    )


def gmail_candidates(path: Path, owner: OwnerProfile, msgvault_db: Path) -> GmailImport:
    """Email candidates from the Gmail import, named from the archive's headers, and how many shared
    mailboxes were dropped."""
    names: dict[str, str] = header_names(msgvault_db)
    candidates: list[Candidate] = []
    dropped: int = 0
    for cells in CsvIO.read_dict_rows(path):
        row = ImportRow.model_validate(cells)
        email: str = row.primary_email
        if is_role_address(email):  # office@, billing@, support@: a mailbox, not a person
            dropped += 1
            continue
        normalized: str = normalize_email(email)
        candidates.append(_candidate(cells, row, IdentifierKind.EMAIL, normalized, email, owner,
                                     names.get(normalized, "")))
    return GmailImport(candidates, dropped)


def phone_candidates(path: Path, owner: OwnerProfile) -> list[Candidate]:
    """Phone candidates from the iMessage and WhatsApp import. Nothing is dropped here."""
    candidates: list[Candidate] = []
    for cells in CsvIO.read_dict_rows(path):
        row = ImportRow.model_validate(cells)
        phone: str = row.primary_phone
        candidates.append(_candidate(cells, row, IdentifierKind.PHONE, normalize_phone(phone), phone, owner))
    return candidates


def write_candidates(conn: sqlite3.Connection, candidates: list[Candidate], now: str) -> None:
    """Three tables from one list: the candidate rows, then their identifiers and sources."""
    candidate_rows: list[queries.CandidateRow] = []
    identifier_rows: list[queries.IdentifierRow] = []
    source_rows: list[queries.SourceRow] = []
    for c in candidates:
        candidate_rows.append((c.candidate_id, c.display_name, int(c.is_owner), c.import_json, now))
        identifier_rows.append((c.candidate_id, c.kind.value, c.normalized, c.display))
        for source in c.sources:
            source_rows.append((c.candidate_id, source.value))
    queries.upsert_candidates(conn, candidate_rows)
    queries.insert_candidate_identifiers(conn, identifier_rows)
    queries.insert_candidate_sources(conn, source_rows)


def write_connections(conn: sqlite3.Connection, path: Path, now: str) -> int:
    """The LinkedIn export as a lookup keyed by URL: name, email when the export has one, position,
    company. Returns how many were written."""
    rows: list[ConnectionRow] = []
    for row in CsvIO.read_dict_rows(path):
        rows.append((row["linkedin_url"], row["full_name"], row["primary_email"] or None,
                     row["current_title"], row["current_company"], now))
    queries.upsert_connections(conn, rows)
    return len(rows)


class ImportLoad(Node):
    name = "import_load"
    reads = ()
    writes = ("candidates", "candidate_identifiers", "candidate_sources", "connections", "owner")

    def required_files(self) -> tuple[Path, ...]:
        # Only the owner is required. Each channel's importer writes its file only when that
        # channel was linked, so a missing CSV means "this user has no such channel".
        return (self.data_root / OWNER_JSON,)

    def __init__(self, conn: sqlite3.Connection, data_root: Path, *, msgvault_db: Path = DEFAULT_MSGVAULT_DB) -> None:
        super().__init__(conn, data_root)
        self.msgvault_db = msgvault_db

    def execute(self) -> dict[str, int]:
        conn: sqlite3.Connection = self.conn
        now: str = now_iso()
        # Owner first: the candidate passes need its addresses and numbers for the owner flag.
        owner: OwnerProfile = load_owner(conn, self.data_root / OWNER_JSON)
        # Two passes, one per linked channel file, one identifier kind each.
        gmail = GmailImport([], 0)
        if (self.data_root / GMAIL_CSV).exists():
            gmail = gmail_candidates(self.data_root / GMAIL_CSV, owner, self.msgvault_db)
        phones: list[Candidate] = []
        if (self.data_root / MESSAGES_CSV).exists():
            phones = phone_candidates(self.data_root / MESSAGES_CSV, owner)
        candidates: list[Candidate] = gmail.candidates + phones
        write_candidates(conn, candidates, now)
        # The LinkedIn export is a lookup for later stages, not a source of candidates.
        connections: int = 0
        if (self.data_root / CONNECTIONS_CSV).exists():
            connections = write_connections(conn, self.data_root / CONNECTIONS_CSV, now)

        # The manifest counts come from what was just written, not from querying it back.
        counts: dict[str, int] = {
            "candidates": len(candidates),
            "identifiers_email": len(gmail.candidates),
            "identifiers_phone": len(phones),
            "shared_mailboxes_dropped": gmail.shared_mailboxes_dropped,
            "connections": connections,
            "owner_flagged": 0,
            "source_gmail_msgvault": 0,
            "source_imessage": 0,
            "source_whatsapp": 0,
        }
        for c in candidates:
            counts["owner_flagged"] += int(c.is_owner)
            for source in c.sources:
                counts["source_" + source.value] += 1
        return counts


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Load the import CSVs and owner.json into the v2 store.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--msgvault-db", type=Path, default=DEFAULT_MSGVAULT_DB, help="the Gmail archive the names are read from")
    args = parser.parse_args(argv)
    conn = open_store(store_path(args.data_root))
    manifest = ImportLoad(conn, args.data_root, msgvault_db=args.msgvault_db).run()
    print(manifest.status, manifest.counts, manifest.error or "")
    if manifest.status == "completed":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
