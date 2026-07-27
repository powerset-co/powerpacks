#!/usr/bin/env python3
"""Merge the per-source import people files into the one canonical people.csv.

This is the whole fan-in. It combines the per-source `people.csv` artifacts,
resolves LinkedIn identity from the shared cross-source `directory.csv`, groups
rows that name the same human, and writes ONE output. It applies no human
decisions, admits nobody, and drops nobody.

Flow:
  1. read `import/<source>/people.csv` for linkedin, gmail, messages (in that
     order — earlier sources win a scalar-field tie)
  2. read `directory.csv` when it exists; build email -> slug and phone -> slug
     lookups from its confident `found` rows
  3. for a row with no slug: look it up by email, then by phone, and stamp
     `public_identifier` + `linkedin_url`
  4. key = `linkedin:<slug>` when a slug is known, else
     `candidate:<candidate_key_for(primary_email, primary_phone)>`
  5. group by key, union the fields (first non-empty wins per scalar column;
     alias lists / channels / artifacts set-union; interaction counts take the
     channel-wise max; last_interaction takes the latest)
  6. id = uuid5(PERSON_ID_NAMESPACE, "linkedin:<slug>") for a linkedin key,
     else the `candidate:<candidate_key>` key verbatim
  7. write `merged/people.csv` + `manifest.json`

A person either has a `public_identifier` or does not. That is the only
distinction the merge makes, and it is a column — not a second file, not an
admission decision.

Changelog:
  2026-07-24: created, replacing `merge_network_sources.py`. The merged CSV
    contract changed: the merge no longer applies `overrides/*.csv` decisions,
    no longer re-reads its own `merged/people.csv` output, and no longer emits
    the reader-less bookkeeping columns (`merge_key`, `merge_confidence`,
    `merge_sources`, `merged_row_count`, `needs_review`, `linkedin_verified*`)
    or the reader-less side artifacts (`network_contacts.csv`,
    `network_companies.csv`, `network_contact_sources.csv`,
    `possible_duplicates_review.csv`, `people_harmonic_all.merged.csv`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.contact_fields import (  # noqa: E402
    emails_from_row,
    normalize_phone,
    phones_from_row,
)
from packs.ingestion.primitives.common.jsonio import emit, now_iso, unique_strings  # noqa: E402
from packs.ingestion.primitives.common.manifests import write_stage_manifest  # noqa: E402
from packs.ingestion.primitives.common.paths import (  # noqa: E402
    DEFAULT_BASE_DIR,
    DEFAULT_DIRECTORY_CSV,
    DEFAULT_IMPORT_DIR,
)
from packs.ingestion.primitives.imports.directory import (  # noqa: E402
    merge_jsonish_lists,
    parse_confidence,
    union_alias_list,
)
from packs.ingestion.schemas.candidates_schema import candidate_key_for  # noqa: E402
from packs.ingestion.schemas.people_schema import (  # noqa: E402
    LIST_VALUE_COLUMNS,
    PEOPLE_SCHEMA_COLUMNS,
    extract_public_identifier,
    generate_person_id,
    latest_interaction,
    merge_interaction_counts,
    normalize_people_row,
    parse_jsonish,
)
from packs.shared.csv_io import CsvIO  # noqa: E402

# Source order is precedence order: on a scalar-field tie the earlier source's
# value is kept, so the curated LinkedIn export beats mailbox-derived text.
MERGE_SOURCES = ("linkedin", "gmail", "messages")
DEFAULT_OUTPUT_DIR = DEFAULT_BASE_DIR / "merged"
# Below this directory confidence a resolution is a guess, not an identity. Same
# bar the importers apply, so the merge stamps only identities they trusted.
MIN_DIRECTORY_CONFIDENCE = 0.75
LINKEDIN_KEY_PREFIX = "linkedin:"
CANDIDATE_KEY_PREFIX = "candidate:"
# Alias list column -> the primary column whose value belongs in that union.
PRIMARY_FOR_LIST_COLUMN = {"all_emails": "primary_email", "all_phones": "primary_phone"}


def default_input_paths(import_dir: Path | None = None) -> list[Path]:
    """The three per-source `people.csv` paths, in precedence order."""
    root = import_dir or DEFAULT_IMPORT_DIR
    return [root / source / "people.csv" for source in MERGE_SOURCES]


def directory_slug_lookups(directory_csv: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(email -> slug, phone -> slug) from `directory.csv`'s confident matches.

    A row contributes only when it is `found`, carries a public identifier, and
    clears MIN_DIRECTORY_CONFIDENCE. First row wins per identifier."""
    emails: dict[str, str] = {}
    phones: dict[str, str] = {}
    if not directory_csv.exists():
        return emails, phones
    for row in CsvIO.read_dict_rows(directory_csv):
        slug = extract_public_identifier(str(row.get("linkedin_url") or "")) or str(
            row.get("public_identifier") or ""
        ).strip().lower()
        if not slug or str(row.get("status") or "").strip().lower() != "found":
            continue
        if parse_confidence(row.get("confidence"), 0.0) < MIN_DIRECTORY_CONFIDENCE:
            continue
        email = str(row.get("email") or "").strip().lower()
        if email:
            emails.setdefault(email, slug)
        phone = normalize_phone(row.get("phone") or "")
        if phone:
            phones.setdefault(phone, slug)
    return emails, phones


def deep_context_slug_lookups(directory_csv: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Final review mappings from the shared directory.

    Unlike ordinary directory rows, an approved Deep Context decision is allowed
    to replace a source row's already-attached slug: that is the purpose of a
    reviewed retarget.  Other directory sources retain the historical
    fill-only behavior below.
    """
    emails: dict[str, str] = {}
    phones: dict[str, str] = {}
    if not directory_csv.exists():
        return emails, phones
    for row in CsvIO.read_dict_rows(directory_csv):
        if str(row.get("source") or "").strip().lower() != "deep_context_review":
            continue
        slug = extract_public_identifier(str(row.get("linkedin_url") or "")) or str(
            row.get("public_identifier") or ""
        ).strip().lower()
        if not slug or str(row.get("status") or "").strip().lower() != "found":
            continue
        if parse_confidence(row.get("confidence"), 0.0) < MIN_DIRECTORY_CONFIDENCE:
            continue
        email = str(row.get("email") or "").strip().lower()
        if email:
            emails[email] = slug
        phone = normalize_phone(row.get("phone") or "")
        if phone:
            phones[phone] = slug
    return emails, phones


def directory_slug_for(row: dict[str, str], emails: dict[str, str], phones: dict[str, str]) -> str:
    """The directory's slug for a row's identifiers — every email first, then phones."""
    for email in emails_from_row(row):
        if email in emails:
            return emails[email]
    for phone in phones_from_row(row):
        if phone in phones:
            return phones[phone]
    return ""


def group_key(row: dict[str, str]) -> str:
    """The identity this row belongs to: its LinkedIn slug, else its contact key.

    Empty only when the row has neither a slug nor an email/phone, which makes it
    unkeyable — there is no identity to merge it onto or to mint an id from."""
    slug = str(row.get("public_identifier") or "").strip().lower()
    if slug:
        return f"{LINKEDIN_KEY_PREFIX}{slug}"
    contact_key = candidate_key_for(row.get("primary_email", ""), row.get("primary_phone", ""))
    return f"{CANDIDATE_KEY_PREFIX}{contact_key}" if contact_key else ""


def person_id_for(key: str) -> str:
    """The durable person id for a group key.

    A LinkedIn key mints Aleph's canonical uuid5; a contact key IS the id, so the
    artifacts already written under `candidate:<key>` keep addressing the same
    human for as long as no slug arrives."""
    if key.startswith(LINKEDIN_KEY_PREFIX):
        return generate_person_id(key[len(LINKEDIN_KEY_PREFIX):])
    return key


def merge_group(key: str, members: list[dict[str, str]]) -> dict[str, str]:
    """Union the rows that named one human into a single people row."""
    merged = {column: "" for column in PEOPLE_SCHEMA_COLUMNS}
    for row in members:
        for column in PEOPLE_SCHEMA_COLUMNS:
            if column in LIST_VALUE_COLUMNS:
                primary = PRIMARY_FOR_LIST_COLUMN.get(column, "")
                merged[column] = union_alias_list(
                    merged[column], row[column],
                    merged.get(primary, "") if primary else "",
                    row.get(primary, "") if primary else "",
                )
            elif column == "source_channels":
                merged[column] = ",".join(unique_strings(
                    merged[column].split(",") + row[column].split(",")
                ))
            elif column == "source_artifacts":
                merged[column] = merge_jsonish_lists(merged[column], row[column])
            elif column == "interaction_counts":
                counts = merge_interaction_counts(merged[column], row[column])
                merged[column] = json.dumps(counts, ensure_ascii=False) if counts else ""
            elif column == "last_interaction":
                merged[column] = latest_interaction(merged[column], row[column])
            elif not merged[column]:
                merged[column] = row[column]
    # Promote an aliased value when no source row carried the primary.
    for column, primary in PRIMARY_FOR_LIST_COLUMN.items():
        if not merged[primary]:
            aliases = unique_strings(parse_jsonish(merged[column], []))
            merged[primary] = aliases[0] if aliases else ""
    merged["id"] = person_id_for(key)
    return merged


class PeopleMerge:
    """Merges the per-source import people files into `merged/people.csv`.

    Owns its fixed output paths, the directory lookup, and the one manifest.
    Construct with explicit inputs/paths and call `run()`."""

    def __init__(
        self,
        *,
        inputs: list[Path] | None = None,
        output_dir: Path | None = None,
        directory_csv: Path | None = None,
    ) -> None:
        self.inputs = [Path(path) for path in (inputs if inputs is not None else default_input_paths())]
        self.output_dir = Path(output_dir or DEFAULT_OUTPUT_DIR)
        self.people_csv = self.output_dir / "people.csv"
        self.manifest_json = self.output_dir / "manifest.json"
        self.directory_csv = Path(directory_csv or DEFAULT_DIRECTORY_CSV)

    def run(self) -> dict[str, Any]:
        """Merge every present input, then write people.csv + manifest.json."""
        started_at = now_iso()
        email_slugs, phone_slugs = directory_slug_lookups(self.directory_csv)
        review_emails, review_phones = deep_context_slug_lookups(self.directory_csv)
        groups: dict[str, list[dict[str, str]]] = {}
        input_rows: dict[str, int] = {}
        stamped = 0
        unkeyable = 0
        for path in self.inputs:
            if not path.exists():
                continue
            rows = [normalize_people_row(raw) for raw in CsvIO.read_dict_rows(path)]
            input_rows[str(path)] = len(rows)
            for row in rows:
                reviewed_slug = directory_slug_for(row, review_emails, review_phones)
                if reviewed_slug and row["public_identifier"] != reviewed_slug:
                    row["public_identifier"] = reviewed_slug
                    row["linkedin_url"] = f"https://www.linkedin.com/in/{reviewed_slug}"
                    stamped += 1
                elif not row["public_identifier"]:
                    slug = directory_slug_for(row, email_slugs, phone_slugs)
                    if slug:
                        row["public_identifier"] = slug
                        row["linkedin_url"] = f"https://www.linkedin.com/in/{slug}"
                        stamped += 1
                key = group_key(row)
                if not key:
                    unkeyable += 1
                    continue
                groups.setdefault(key, []).append(row)
        if not input_rows:
            return self._manifest(
                status="not_ready", reason="missing_import_people_csvs", started_at=started_at,
                input_rows=input_rows, rows=0, stamped=stamped, unkeyable=unkeyable, groups={},
            )
        merged = [merge_group(key, groups[key]) for key in sorted(groups)]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        CsvIO.write_dict_rows(self.people_csv, PEOPLE_SCHEMA_COLUMNS, merged)
        progress(f"merged {sum(input_rows.values())} source rows into {len(merged)} people")
        return self._manifest(
            status="completed", reason="", started_at=started_at, input_rows=input_rows,
            rows=len(merged), stamped=stamped, unkeyable=unkeyable, groups=groups,
        )

    def _manifest(
        self,
        *,
        status: str,
        reason: str,
        started_at: str,
        input_rows: dict[str, int],
        rows: int,
        stamped: int,
        unkeyable: int,
        groups: dict[str, list[dict[str, str]]],
    ) -> dict[str, Any]:
        """Write this stage's single manifest and return its payload."""
        sizes: dict[str, int] = {}
        for members in groups.values():
            bucket = str(len(members))
            sizes[bucket] = sizes.get(bucket, 0) + 1
        payload: dict[str, Any] = {
            "stage": "merge_people",
            "status": status,
            "input": {
                "people_csvs": [str(path) for path in self.inputs],
                "directory_csv": str(self.directory_csv),
            },
            "artifacts": {"people_csv": str(self.people_csv)},
            "stats": {
                "input_rows": input_rows,
                "input_rows_total": sum(input_rows.values()),
                "rows": rows,
                "linkedin_ids": sum(1 for key in groups if key.startswith(LINKEDIN_KEY_PREFIX)),
                "candidate_ids": sum(1 for key in groups if key.startswith(CANDIDATE_KEY_PREFIX)),
                "directory_stamped": stamped,
                "dropped_unkeyable": unkeyable,
                "groups_by_size": sizes,
            },
            "started_at": started_at,
        }
        if reason:
            payload["reason"] = reason
        return write_stage_manifest(self.manifest_json, payload)


def progress(message: str) -> None:
    """One terse stderr line with this primitive's stable prefix."""
    print(f"[merge-people] {message}", file=sys.stderr, flush=True)


def main() -> int:
    """Exit 0 when the merge completed, 1 when there was nothing to merge."""
    parser = argparse.ArgumentParser(description="Merge per-source import people files")
    parser.add_argument("command", choices=["run"])
    parser.add_argument(
        "--input", action="append", default=[],
        help="A per-source people.csv to merge; repeatable. Defaults to the three import people.csv files.",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--directory-csv", default=str(DEFAULT_DIRECTORY_CSV))
    args = parser.parse_args()
    payload = PeopleMerge(
        inputs=[Path(value) for value in args.input] or None,
        output_dir=Path(args.output_dir),
        directory_csv=Path(args.directory_csv),
    ).run()
    emit(payload)
    return 0 if payload.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
