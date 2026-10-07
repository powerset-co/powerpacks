"""Block 09 Realize: one people row per parent the owner has talked to, written to people.csv.

A family is the candidates sharing a current parent; it is exported when any member has facts. Its
members' emails, phones, channels and message counts are unioned into one row. The row's URL is the
family's accepted LinkedIn from current_profile, and the profile columns are filled from the profile
cache. A synthetic key is not a URL: that family is exported with no URL, so the index skips it. No
decisions and no provider calls here; a URL without a cached profile is counted.

The search index is built from this people.csv by the indexing pipeline, which may spend; this block
prints its command.

Created: 2026-10-07
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context_v2.db import queries_realize
from packs.ingestion.primitives.deep_context_v2.db.queries_realize import Identifier, Member
from packs.ingestion.primitives.deep_context_v2.db.schema import SYNTHETIC_PROFILE_PREFIX, IdentifierKind, Worth
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import CACHE_RELATIVE_DIR
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path, read_usable_cached_profile
from packs.ingestion.primitives.enrich.profile_transforms import normalize_rapidapi
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS, extract_public_identifier
from packs.shared.csv_io import CsvIO

# The file the index builder reads and hashes to decide on a re-index; replaced whole each run.
PEOPLE_CSV_RELATIVE_PATH = Path("network-import") / "merged" / "people.csv"
# The people columns a LinkedIn profile fills.
PROFILE_COLUMNS: tuple[str, ...] = (
    "first_name", "last_name", "full_name", "headline", "summary", "city", "state", "country",
    "location_raw", "profile_picture_url", "work_experiences", "education", "current_title", "current_company",
)


def message_total(member: Member) -> int:
    """A candidate's messages across channels, from the importer's counts."""
    total: int = 0
    for count in member.interaction_counts.values():
        total += count
    return total


def by_most_messages(members: list[Member]) -> list[Member]:
    """The family's members, most messages first; ties by candidate id so the order is stable."""
    return sorted(members, key=lambda member: (-message_total(member), member.candidate_id))


def fill_profile(row: dict[str, str], record: dict[str, Any]) -> None:
    """Fill the row's profile columns from one cached LinkedIn profile."""
    profile: dict[str, Any] = normalize_rapidapi(record["raw_response"], row["public_identifier"], row["linkedin_url"])
    for column in PROFILE_COLUMNS:
        value: Any = profile[column]
        if isinstance(value, list):
            value = json.dumps(value, ensure_ascii=False)
        row[column] = value


class Realize(Node):
    name = "realize"
    reads = ("current_parent", "current_worth", "current_profile", "candidates", "candidate_identifiers",
             "candidate_sources", "facts")
    writes = ()

    def build(self) -> tuple[list[dict[str, str]], dict[str, int]]:
        """Every exported row, and the counts for the manifest."""
        # Group the talked-to candidates into families.
        families: dict[str, list[Member]] = {}
        for member in queries_realize.members_of_talked_to_parents(self.conn):
            families.setdefault(member.parent_id, []).append(member)
        identifiers: dict[str, list[Identifier]] = queries_realize.identifiers_by_candidate(self.conn)
        channels: dict[str, list[str]] = queries_realize.channels_by_candidate(self.conn)
        profile_keys: dict[str, str] = queries_realize.profile_keys(self.conn)
        worth: dict[str, Worth] = queries_realize.worth_by_parent(self.conn)
        cache_dir: Path = self.data_root / CACHE_RELATIVE_DIR

        rows: list[dict[str, str]] = []
        counts: dict[str, int] = {"rows": 0, "with_url": 0, "synthetic": 0, "no_profile_key": 0,
                                  "profiles_filled": 0, "profiles_missing": 0, "worth_no": 0, "worth_undecided": 0}
        for parent_id, family in families.items():
            row: dict[str, str] = {}
            for column in PEOPLE_SCHEMA_COLUMNS:
                row[column] = ""
            row["id"] = parent_id

            # Union the members' emails, phones, channels and counts. The member with the most
            # messages comes first, so its address and number are the primary ones.
            ordered: list[Member] = by_most_messages(family)
            emails: list[str] = []
            phones: list[str] = []
            sources: set[str] = set()
            interaction_counts: dict[str, int] = {}
            last_interaction: str = ""
            for member in ordered:
                for identifier in identifiers[member.candidate_id]:
                    if identifier.kind == IdentifierKind.EMAIL and identifier.display_value not in emails:
                        emails.append(identifier.display_value)
                    if identifier.kind == IdentifierKind.PHONE and identifier.display_value not in phones:
                        phones.append(identifier.display_value)
                sources.update(channels[member.candidate_id])
                for channel, count in member.interaction_counts.items():
                    interaction_counts[channel] = interaction_counts.get(channel, 0) + count
                last_interaction = max(last_interaction, member.last_interaction)
            if emails:
                row["primary_email"] = emails[0]
            if phones:
                row["primary_phone"] = phones[0]
            row["all_emails"] = json.dumps(emails, ensure_ascii=False)
            row["all_phones"] = json.dumps(phones, ensure_ascii=False)
            row["source_channels"] = ",".join(sorted(sources))
            row["interaction_counts"] = json.dumps(interaction_counts, ensure_ascii=False)
            row["last_interaction"] = last_interaction

            # The family's one accepted profile: a LinkedIn URL is exported and filled from the cache. A
            # synthetic key is not a URL: the row goes out without one (the index still takes a URL-less
            # row; the page's "synthetic rows are not indexed" is not what the indexer does, flagged).
            key: str = profile_keys.get(parent_id, "")
            if not key:
                counts["no_profile_key"] += 1
            elif key.startswith(SYNTHETIC_PROFILE_PREFIX):
                counts["synthetic"] += 1
            else:
                counts["with_url"] += 1
                row["linkedin_url"] = key
                row["public_identifier"] = extract_public_identifier(key)
                record: dict[str, Any] | None = read_usable_cached_profile(
                    profile_cache_path(cache_dir, row["public_identifier"]))
                if record is None:
                    counts["profiles_missing"] += 1
                else:
                    fill_profile(row, record)
                    counts["profiles_filled"] += 1

            # The family's one name: the profile's when it has one, else the display name of the
            # member with the most messages.
            if not row["full_name"]:
                row["full_name"] = ordered[0].display_name

            # Worth is not a people column: a worth-no family is exported like any other and counted.
            if parent_id not in worth:
                counts["worth_undecided"] += 1
            elif worth[parent_id] == Worth.NO:
                counts["worth_no"] += 1
            rows.append(row)
        counts["rows"] = len(rows)
        return rows, counts

    def people_csv(self) -> Path:
        """Where the export is written: merged/people.csv under the data root (the file the index builder reads)."""
        return self.data_root / PEOPLE_CSV_RELATIVE_PATH

    def execute(self) -> dict[str, int]:
        """One row per parent with facts, written to people.csv, replaced whole each run; the counts say what each row carried."""
        rows, counts = self.build()
        CsvIO.write_dict_rows(self.people_csv(), PEOPLE_SCHEMA_COLUMNS, rows)
        return counts


def index_command(people_csv: Path, data_root: Path) -> str:
    """The indexing pipeline's dry run over this people.csv: it prices the build before any spend."""
    return ("uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py "
            f"run --dry-run --input {people_csv} --output-dir {data_root / 'search-index'} "
            "--default-operator-id <operator-id>")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="09 Realize: one people row per parent, written to people.csv.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--dry-run", action="store_true", help="counts only; no file, no manifest")
    args = parser.parse_args(argv)
    node = Realize(open_store(store_path(args.data_root)), args.data_root)
    if args.dry_run:
        _rows, counts = node.build()
        print(json.dumps(counts, indent=2))
        return 0
    manifest = node.run()
    print(manifest.status, " ".join(f"{key}={value}" for key, value in manifest.counts.items()), manifest.error or "")
    print(f"people.csv: {node.people_csv()}")
    print(f"index: {index_command(node.people_csv(), args.data_root)}")
    return 0 if manifest.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
