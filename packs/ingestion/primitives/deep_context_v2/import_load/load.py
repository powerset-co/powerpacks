"""Block 01 Import load: the per-source import CSVs and owner.json into the v2 store.

Gmail rows become email candidates and messages rows become phone candidates, each with its
names, its one identifier and its channels. The two files never share an id. LinkedIn rows
become the `connections` lookup, never candidates. Shared mailboxes (role addresses such as
office@ or billing@) are dropped here; every other keep rule ran in the per-source importer.
owner.json fills the `owner` row and flags the operator's own candidates.

Nothing is deleted. A candidate row upserts on its id; names, identifiers and sources insert on
their primary keys and are ignored when present. A rebuild is rm of the store and a rerun.

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
from packs.ingestion.primitives.deep_context_v2.db.owner import OwnerProfile, load_owner
from packs.ingestion.primitives.deep_context_v2.db.schema import IdentifierKind, SourceChannel
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.shared.csv_io import CsvIO

IMPORT_DIR = Path("network-import") / "import"
GMAIL_CSV = IMPORT_DIR / "gmail" / "people.csv"        # one email per row, ids candidate:email:<address>
MESSAGES_CSV = IMPORT_DIR / "messages" / "people.csv"  # one phone per row, ids candidate:phone:+<digits>
CONNECTIONS_CSV = IMPORT_DIR / "linkedin" / "people.csv"
OWNER_JSON = Path("deep-context") / "owner.json"
BATCH = 500


@dataclass(frozen=True)
class Candidate:
    """One import row, ready to write: the candidate row, its names, its identifier, its channels."""

    candidate_id: str
    display_name: str
    is_owner: bool
    import_json: str
    names: tuple[str, ...]
    kind: IdentifierKind
    normalized: str
    display: str
    sources: tuple[SourceChannel, ...]


def _candidate(row: dict[str, str], kind: IdentifierKind, normalized: str, display: str, owner: OwnerProfile) -> Candidate:
    full_name = row["full_name"].strip()
    first_last = (row["first_name"].strip() + " " + row["last_name"].strip()).strip()
    names = []
    for name in (full_name, first_last):
        if name and name not in names:
            names.append(name)
    sources = []
    for part in row["source_channels"].split(","):
        sources.append(SourceChannel(part.strip()))
    return Candidate(
        candidate_id=row["id"],
        display_name=full_name,
        is_owner=normalized in owner.emails or normalized in owner.phones,
        import_json=json.dumps(row, ensure_ascii=False),
        names=tuple(names),
        kind=kind,
        normalized=normalized,
        display=display,
        sources=tuple(sources),
    )


def gmail_candidates(path: Path, owner: OwnerProfile) -> tuple[list[Candidate], int]:
    """Email candidates from the Gmail import; returns them and the count of shared mailboxes dropped."""
    candidates = []
    dropped = 0
    for row in CsvIO.read_dict_rows(path):
        email = row["primary_email"]
        if is_role_address(email):
            dropped += 1
            continue
        candidates.append(_candidate(row, IdentifierKind.EMAIL, normalize_email(email), email, owner))
    return candidates, dropped


def phone_candidates(path: Path, owner: OwnerProfile) -> list[Candidate]:
    """Phone candidates from the iMessage and WhatsApp import."""
    candidates = []
    for row in CsvIO.read_dict_rows(path):
        phone = row["primary_phone"]
        candidates.append(_candidate(row, IdentifierKind.PHONE, normalize_phone(phone), phone, owner))
    return candidates


def write_candidates(conn: sqlite3.Connection, candidates: list[Candidate], now: str) -> None:
    """Upsert the candidate rows and insert their names, identifier and sources, BATCH at a time."""
    for start in range(0, len(candidates), BATCH):
        candidate_rows = []
        name_rows = []
        identifier_rows = []
        source_rows = []
        for c in candidates[start:start + BATCH]:
            candidate_rows.append((c.candidate_id, c.display_name, int(c.is_owner), c.import_json, now))
            for name in c.names:
                name_rows.append((c.candidate_id, name))
            identifier_rows.append((c.candidate_id, c.kind.value, c.normalized, c.display))
            for source in c.sources:
                source_rows.append((c.candidate_id, source.value))
        conn.executemany(
            "INSERT INTO candidates (candidate_id, display_name, is_owner, import_json, imported_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (candidate_id) DO UPDATE SET "
            "display_name = excluded.display_name, is_owner = excluded.is_owner, "
            "import_json = excluded.import_json, imported_at = excluded.imported_at",
            candidate_rows,
        )
        conn.executemany("INSERT OR IGNORE INTO candidate_names (candidate_id, name) VALUES (?, ?)", name_rows)
        conn.executemany(
            "INSERT OR IGNORE INTO candidate_identifiers (candidate_id, kind, normalized_value, display_value) "
            "VALUES (?, ?, ?, ?)",
            identifier_rows,
        )
        conn.executemany("INSERT OR IGNORE INTO candidate_sources (candidate_id, source) VALUES (?, ?)", source_rows)


def write_connections(conn: sqlite3.Connection, path: Path, now: str) -> None:
    """The LinkedIn export as a lookup keyed by URL: name, email when the export has one, position, company."""
    rows = []
    for row in CsvIO.read_dict_rows(path):
        rows.append((row["linkedin_url"], row["full_name"], row["primary_email"] or None,
                     row["current_title"], row["current_company"], now))
    for start in range(0, len(rows), BATCH):
        conn.executemany(
            "INSERT INTO connections (linkedin_url, name, email, position, company, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (linkedin_url) DO UPDATE SET name = excluded.name, "
            "email = excluded.email, position = excluded.position, company = excluded.company, "
            "imported_at = excluded.imported_at",
            rows[start:start + BATCH],
        )


class ImportLoad(Node):
    name = "import_load"
    reads = ()
    writes = ("candidates", "candidate_names", "candidate_identifiers", "candidate_sources", "connections", "owner")

    def required_files(self) -> tuple[Path, ...]:
        return (
            self.data_root / GMAIL_CSV,
            self.data_root / MESSAGES_CSV,
            self.data_root / CONNECTIONS_CSV,
            self.data_root / OWNER_JSON,
        )

    def execute(self) -> dict[str, int]:
        conn = self.conn
        now = now_iso()
        owner = load_owner(conn, self.data_root / OWNER_JSON)
        emails, dropped = gmail_candidates(self.data_root / GMAIL_CSV, owner)
        phones = phone_candidates(self.data_root / MESSAGES_CSV, owner)
        write_candidates(conn, emails + phones, now)
        write_connections(conn, self.data_root / CONNECTIONS_CSV, now)

        def count(sql: str, *params: str) -> int:
            return conn.execute(sql, params).fetchone()[0]

        return {
            "candidates": count("SELECT COUNT(*) FROM candidates"),
            "names": count("SELECT COUNT(*) FROM candidate_names"),
            "connections": count("SELECT COUNT(*) FROM connections"),
            "shared_mailboxes_dropped": dropped,
            "owner_flagged": count("SELECT COUNT(*) FROM candidates WHERE is_owner = 1"),
            "source_gmail_msgvault": count("SELECT COUNT(*) FROM candidate_sources WHERE source = ?", "gmail_msgvault"),
            "source_imessage": count("SELECT COUNT(*) FROM candidate_sources WHERE source = ?", "imessage"),
            "source_whatsapp": count("SELECT COUNT(*) FROM candidate_sources WHERE source = ?", "whatsapp"),
            "identifiers_email": count("SELECT COUNT(*) FROM candidate_identifiers WHERE kind = ?", "email"),
            "identifiers_phone": count("SELECT COUNT(*) FROM candidate_identifiers WHERE kind = ?", "phone"),
        }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Load the import CSVs and owner.json into the v2 store.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    args = parser.parse_args(argv)
    conn = open_store(store_path(args.data_root))
    manifest = ImportLoad(conn, args.data_root).run()
    print(manifest.status, " ".join(f"{key}={value}" for key, value in manifest.counts.items()), manifest.error or "")
    return 0 if manifest.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
