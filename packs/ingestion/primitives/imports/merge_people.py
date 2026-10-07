#!/usr/bin/env python3
"""Combine source people by their existing IDs into people.csv and manifest.json.

Flow:
  1. read source people; expand current Gmail imports to recorded account rows
  2. retain each source ID; derive a contact key only when the ID is absent
  3. union metadata for repeated IDs and write merged/people.csv + manifest.json

Profile URLs, saved directory lookups and cached profile contents do not establish
contact identity. Deep Context owns profile association and person merging.

Changelog:
  2026-10-03: remove directory stamping, profile grouping and cache hydration.
  2026-07-24: created, replacing merge_network_sources.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit, now_iso, unique_strings  # noqa: E402
from packs.ingestion.primitives.common.paths import (  # noqa: E402
    DEFAULT_BASE_DIR,
    DEFAULT_IMPORT_DIR,
)
from packs.ingestion.primitives.pipeline.contract import (  # noqa: E402
    Artifact,
    Node,
    PeopleRow,
    StageManifest,
)
from packs.ingestion.primitives.imports.directory import (  # noqa: E402
    merge_jsonish_lists,
    union_alias_list,
)
from packs.ingestion.primitives.common.names import source_names_can_match  # noqa: E402
from packs.ingestion.primitives.imports.gmail.source_people import original_source_people  # noqa: E402
from packs.ingestion.schemas.candidates_schema import candidate_key_for  # noqa: E402
from packs.ingestion.schemas.people_schema import (  # noqa: E402
    LIST_VALUE_COLUMNS,
    PEOPLE_SCHEMA_COLUMNS,
    latest_interaction,
    merge_interaction_counts,
    parse_jsonish,
)
from packs.shared.csv_io import CsvIO  # noqa: E402
from pydantic import BaseModel  # noqa: E402

# Source order is precedence order: on a scalar-field tie the earlier source's
# value is kept, so the curated LinkedIn export beats mailbox-derived text.
MERGE_SOURCES = ("linkedin", "gmail", "messages")
DEFAULT_OUTPUT_DIR = DEFAULT_BASE_DIR / "merged"
CANDIDATE_KEY_PREFIX = "candidate:"
# Alias list column -> the primary column whose value belongs in that union.
PRIMARY_FOR_LIST_COLUMN = {"all_emails": "primary_email", "all_phones": "primary_phone"}
# Profile fields filled by Deep Context after an association is accepted.
PROFILE_COLUMNS = (
    "first_name", "last_name", "full_name", "headline", "summary", "city", "state", "country",
    "location_raw", "profile_picture_url", "work_experiences", "education",
    "current_title", "current_company",
)


def default_input_paths(import_dir: Path | None = None) -> list[Path]:
    """The three per-source `people.csv` paths, in precedence order."""
    root = import_dir or DEFAULT_IMPORT_DIR
    return [root / source / "people.csv" for source in MERGE_SOURCES]


def group_key(row: PeopleRow) -> str:
    """Use the source ID, deriving a contact key only when it is absent."""
    if row.id.strip():
        return row.id.strip()
    contact_key = candidate_key_for(row.primary_email, row.primary_phone)
    return f"{CANDIDATE_KEY_PREFIX}{contact_key}" if contact_key else ""


def merge_group(key: str, members: list[PeopleRow]) -> dict[str, str]:
    """Union metadata under the caller's chosen existing person ID."""
    merged = {column: "" for column in PEOPLE_SCHEMA_COLUMNS}
    for row in members:
        for column in PEOPLE_SCHEMA_COLUMNS:
            value = getattr(row, column)
            if column in LIST_VALUE_COLUMNS:
                primary = PRIMARY_FOR_LIST_COLUMN[column] if column in PRIMARY_FOR_LIST_COLUMN else ""
                merged[column] = union_alias_list(
                    merged[column], value,
                    merged[primary] if primary else "",
                    getattr(row, primary) if primary else "",
                )
            elif column == "source_channels":
                merged[column] = ",".join(unique_strings(
                    merged[column].split(",") + value.split(",")
                ))
            elif column == "source_artifacts":
                merged[column] = merge_jsonish_lists(merged[column], value)
            elif column == "interaction_counts":
                counts = merge_interaction_counts(merged[column], value)
                merged[column] = json.dumps(counts, ensure_ascii=False) if counts else ""
            elif column == "last_interaction":
                merged[column] = latest_interaction(merged[column], value)
            elif not merged[column]:
                merged[column] = value
    names = tuple(name for row in members
                  if (name := row.full_name or " ".join(filter(None, (row.first_name, row.last_name)))).strip())
    if names and not source_names_can_match(names):
        for column in ("full_name", "first_name", "last_name"):
            merged[column] = ""
    # Promote an aliased value when no source row carried the primary.
    for column, primary in PRIMARY_FOR_LIST_COLUMN.items():
        if not merged[primary]:
            aliases = unique_strings(parse_jsonish(merged[column], []))
            merged[primary] = aliases[0] if aliases else ""
    merged["id"] = key
    for row in members:
        if row.id and row.id != merged["id"]:
            merged["superseded_person_ids"] = union_alias_list(merged["superseded_person_ids"], "", row.id)
    return merged


def has_work_history(row: dict[str, str]) -> bool:
    return row["work_experiences"] not in ("", "[]")


def fill_profile_columns(row: dict[str, str], profile: dict) -> None:
    """Fill the row's empty profile columns from one normalized profile."""
    for column in PROFILE_COLUMNS if profile else ():
        value = profile[column]
        if isinstance(value, list):
            value = json.dumps(value, ensure_ascii=False) if value else ""
        if value and row[column] in ("", "[]"):
            row[column] = value


class MergePeopleInput(BaseModel):
    """The `input` block: what this run was pointed at."""
    people_csvs: list[str]


class MergePeopleArtifacts(BaseModel):
    """The `artifacts` block: the one output this stage owns."""
    people_csv: str


class MergePeopleStats(BaseModel):
    """Source and output counts for this fan-in."""
    input_rows: dict[str, int]
    input_rows_total: int
    rows: int
    linkedin_ids: int
    candidate_ids: int
    dropped_unkeyable: int
    groups_by_size: dict[str, int]


class MergePeopleManifest(StageManifest):
    """Source inputs, output path and counts for this stage."""
    stage: str = "merge_people"
    input: MergePeopleInput
    artifacts: MergePeopleArtifacts
    stats: MergePeopleStats
    started_at: str = ""
    # Dropped from the manifest when absent, like the old `if reason:` guard.
    reason: str | None = None


class PeopleMerge(Node):
    """Merges the per-source import people files into `merged/people.csv`.

    Owns its fixed output paths and the one manifest.
    Construct with explicit inputs/paths and call `run()` (the Node template:
    declared inputs -> `execute()` -> declared outputs -> manifest)."""

    name = "merge_people"
    # The gmail and messages people.csv files are their importers' declared
    # outputs; `import/linkedin/people.csv` is `external=True` because NOTHING in
    # `packs/ingestion` writes it: the LinkedIn import runs in the Modal sandbox
    # and `packs/indexing/modal/linkedin_modal_pipeline.py` downloads the enriched
    # file to that path (see `imports/linkedin/network_import.py`, which says so at
    # the top and declares `discover/linkedin/people.csv` as its own output).
    # `required=False` because this merge deliberately tolerates an absent source
    # (it merges whatever is present and reports `not_ready` only when NO source
    # file was readable).
    inputs = tuple(
        Artifact(
            path=str(DEFAULT_IMPORT_DIR / source / "people.csv"),
            row_model=PeopleRow,
            external=source == "linkedin",
            required=False,
        )
        for source in MERGE_SOURCES
    )
    outputs = (
        Artifact(
            path=str(DEFAULT_OUTPUT_DIR / "people.csv"),
            row_model=PeopleRow,
            writes="full_rewrite",
            # Read by indexing / deep-context / search, none of them converted.
        ),
    )
    payload = MergePeopleManifest
    manifest = str(DEFAULT_OUTPUT_DIR / "manifest.json")

    def __init__(
        self,
        *,
        inputs: list[Path] | None = None,
        output_dir: Path | None = None,
    ) -> None:
        # `source_csvs`, not `inputs`: `inputs` is now the declared Artifact tuple
        # (the contract); this is the path list THIS run was constructed with.
        self.source_csvs = [Path(path) for path in (inputs if inputs is not None else default_input_paths())]
        self.output_dir = Path(output_dir or DEFAULT_OUTPUT_DIR)
        self.people_csv = self.output_dir / "people.csv"
        self.manifest_json = self.output_dir / "manifest.json"

    def bindings(self) -> dict[str, str]:
        """Declared path -> this instance's path, so an explicit `--output-dir` /
        `--input` list still validates against the declaration. Keys come from the
        declaration itself, never from a second read of a default."""
        bound = {
            self.outputs[0].path: str(self.people_csv),
            self.manifest: str(self.manifest_json),
        }
        for declared, actual in zip(self.inputs, self.source_csvs):
            bound[declared.path] = str(actual)
        return bound

    def execute(self) -> MergePeopleManifest:
        """Merge every present input, then write people.csv (the Node template
        writes the manifest)."""
        started_at = now_iso()
        groups: dict[str, list[PeopleRow]] = {}
        input_rows: dict[str, int] = {}
        unkeyable = 0
        for path in self.source_csvs:
            if not path.exists():
                continue
            rows = original_source_people(path)
            if rows is None:
                rows = tuple(PeopleRow.model_validate(raw) for raw in CsvIO.read_dict_rows(path))
            input_rows[str(path)] = len(rows)
            for row in rows:
                key = group_key(row)
                if not key:
                    unkeyable += 1
                    continue
                groups.setdefault(key, []).append(row)
        if not input_rows:
            return self._payload(
                status="not_ready", reason="missing_import_people_csvs", started_at=started_at,
                input_rows=input_rows, rows=0, unkeyable=unkeyable, groups={},
            )
        merged = [merge_group(key, groups[key]) for key in sorted(groups)]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        CsvIO.write_dict_rows(self.people_csv, PEOPLE_SCHEMA_COLUMNS, merged)
        progress(f"merged {sum(input_rows.values())} source rows into {len(merged)} people")
        return self._payload(
            status="completed", reason="", started_at=started_at, input_rows=input_rows,
            rows=len(merged), unkeyable=unkeyable, groups=groups,
        )

    def _payload(
        self,
        *,
        status: str,
        reason: str,
        started_at: str,
        input_rows: dict[str, int],
        rows: int,
        unkeyable: int,
        groups: dict[str, list[PeopleRow]],
    ) -> MergePeopleManifest:
        """This stage's typed manifest payload (the Node template writes it)."""
        sizes: dict[str, int] = {}
        for members in groups.values():
            bucket = str(len(members))
            sizes[bucket] = sizes[bucket] + 1 if bucket in sizes else 1
        return MergePeopleManifest(
            status=status,
            input=MergePeopleInput(
                people_csvs=[str(path) for path in self.source_csvs],
            ),
            artifacts=MergePeopleArtifacts(people_csv=str(self.people_csv)),
            stats=MergePeopleStats(
                input_rows=input_rows,
                input_rows_total=sum(input_rows.values()),
                rows=rows,
                linkedin_ids=sum(1 for members in groups.values()
                                 if any("linkedin_csv" in row.source_channels.split(",") for row in members)),
                candidate_ids=sum(1 for key in groups if key.startswith(CANDIDATE_KEY_PREFIX)),
                dropped_unkeyable=unkeyable,
                groups_by_size=sizes,
            ),
            started_at=started_at,
            reason=reason or None,
        )


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
    args = parser.parse_args()
    payload = PeopleMerge(
        inputs=[Path(value) for value in args.input] or None,
        output_dir=Path(args.output_dir),
    ).run()
    emit(payload.to_payload())
    return 0 if payload.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
