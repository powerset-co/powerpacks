"""Block 01, the import load: the per-source import CSVs and owner.json into the v2 store.

Spec "01 Import": candidates, names, identifiers, sources, the connections lookup and owner.
No parent rows, no merge, no verdict. Rows join only on identical id; the Gmail and messages
CSVs never share one (email ids vs phone ids), so each CSV row is one candidate. The LinkedIn
export is the `connections` lookup, never a candidate. The one drop applied here is the shared
mailbox; every other keep rule ran in the per-source importer.

A rerun upserts each candidate and rewrites its names, identifiers and sources; connections
upsert by URL. Candidate rows are never deleted. A rebuild is rm of the store and a rerun.

From v1:
  common/contact_fields.py: normalize_email, normalize_phone, is_shared_mailbox
  packs/shared/csv_io.py: CsvIO.read_dict_rows
Copied: the shared-mailbox predicate (deep_context/ensure_parents/imported_people.py:165-175)
  minus its LinkedIn-channel clause; the owner match (deep_context/db/projectors.py:73-79).

Created: 2026-10-06
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from packs.ingestion.primitives.common.contact_fields import is_shared_mailbox, normalize_email, normalize_phone
from packs.ingestion.primitives.deep_context_v2.db.owner import load_owner
from packs.ingestion.primitives.deep_context_v2.db.schema import IdentifierKind, SourceChannel
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.shared.csv_io import CsvIO

IMPORT_DIR = Path("network-import") / "import"
CANDIDATE_CSVS = (IMPORT_DIR / "gmail" / "people.csv", IMPORT_DIR / "messages" / "people.csv")
CONNECTIONS_CSV = IMPORT_DIR / "linkedin" / "people.csv"
OWNER_JSON = Path("deep-context") / "owner.json"


def _values(row: dict[str, str], primary: str, listed: str) -> list[str]:
    """The row's primary value and its JSON-list column, as they appear in the CSV, once each."""
    values: list[str] = []
    for value in [row[primary]] + json.loads(row[listed] or "[]"):
        if value and value not in values:
            values.append(value)
    return values


class ImportLoad(Node):
    name = "import_load"
    reads = ()
    writes = ("candidates", "candidate_names", "candidate_identifiers", "candidate_sources", "connections", "owner")

    def required_files(self) -> tuple[Path, ...]:
        paths = []
        for path in CANDIDATE_CSVS + (CONNECTIONS_CSV, OWNER_JSON):
            paths.append(self.data_root / path)
        return tuple(paths)

    def execute(self) -> dict[str, int]:
        conn = self.conn
        owner = load_owner(conn, self.data_root / OWNER_JSON)  # emails and phones already normalized
        now = now_iso()
        dropped = 0

        for csv_path in CANDIDATE_CSVS:
            for row in CsvIO.read_dict_rows(self.data_root / csv_path):
                emails = _values(row, "primary_email", "all_emails")
                phones = _values(row, "primary_phone", "all_phones")
                if is_shared_mailbox(emails, phones):
                    dropped += 1
                    continue
                candidate_id = row["id"]
                identifiers = []  # (kind, normalized value, value as shown in the CSV)
                for email in emails:
                    identifiers.append((IdentifierKind.EMAIL, normalize_email(email), email))
                for phone in phones:
                    identifiers.append((IdentifierKind.PHONE, normalize_phone(phone), phone))
                is_owner = False
                for _, normalized, _ in identifiers:
                    if normalized in owner.emails or normalized in owner.phones:
                        is_owner = True
                full_name = row["full_name"].strip()
                first_last = (row["first_name"].strip() + " " + row["last_name"].strip()).strip()
                names = []
                for name in (full_name, first_last):
                    if name and name not in names:
                        names.append(name)
                sources = [SourceChannel(part.strip()) for part in row["source_channels"].split(",")]

                conn.execute(
                    "INSERT INTO candidates (candidate_id, display_name, is_owner, import_json, imported_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (candidate_id) DO UPDATE SET "
                    "display_name = excluded.display_name, is_owner = excluded.is_owner, "
                    "import_json = excluded.import_json, imported_at = excluded.imported_at",
                    (candidate_id, full_name, int(is_owner), json.dumps(row, ensure_ascii=False), now),
                )
                for table in ("candidate_names", "candidate_identifiers", "candidate_sources"):
                    conn.execute(f"DELETE FROM {table} WHERE candidate_id = ?", (candidate_id,))
                conn.executemany("INSERT INTO candidate_names (candidate_id, name) VALUES (?, ?)",
                                 [(candidate_id, name) for name in names])
                conn.executemany(
                    "INSERT INTO candidate_identifiers (candidate_id, kind, normalized_value, display_value) "
                    "VALUES (?, ?, ?, ?)",
                    [(candidate_id, kind.value, value, display) for kind, value, display in identifiers],
                )
                conn.executemany("INSERT INTO candidate_sources (candidate_id, source) VALUES (?, ?)",
                                 [(candidate_id, source.value) for source in sources])

        connection_rows = CsvIO.read_dict_rows(self.data_root / CONNECTIONS_CSV)
        by_url = {row["linkedin_url"]: row for row in connection_rows}
        if len(by_url) != len(connection_rows):
            raise ValueError(f"{len(connection_rows) - len(by_url)} LinkedIn rows share a URL with another row")
        conn.executemany(
            "INSERT INTO connections (linkedin_url, name, email, position, company, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (linkedin_url) DO UPDATE SET name = excluded.name, "
            "email = excluded.email, position = excluded.position, company = excluded.company, "
            "imported_at = excluded.imported_at",
            [(url, row["full_name"], row["primary_email"] or None, row["current_title"],
              row["current_company"], now) for url, row in by_url.items()],
        )

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
