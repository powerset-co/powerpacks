"""Realize reviewed identities in canonical SQLite, then export people.csv.

Flow: the imported roster (`queries.imported_people`) -> each person takes its
parent's accepted LinkedIn (verify, or a retarget to a real LinkedIn — a
synthetic profile itself is never accepted); a person whose own imported
LinkedIn was detached or excluded loses it; a row that loses or changes its
LinkedIn keeps only its contact columns -> rows group and union exactly as the
import fan-in does (`merge_people.group_key` / `merge_group`) -> an accepted
LinkedIn fills its empty profile columns from the profile projected into SQLite
for that exact slug -> the parent families a final row spans (its members' and,
when its id is already a person, that person's) merge with `Db.merge_parents`
into the one `ParentAssignment.elect` picks -> every final id not yet a person
is added under that parent -> the final rows replace the imported roster -> the
same rows are written to `merged/people.csv` + `manifest.json`.

Earlier people, facts and decisions stay; a final row names the ids it absorbed
in `superseded_person_ids`. A person with no LinkedIn, email or phone left has
no identity and leaves the roster. No CSV is read and no provider is called; a
profile not yet in SQLite is counted in `profiles_missing`. An empty roster
(a store no import has reached yet) exports nothing and says to run
`ensure-parents`. Each step is idempotent, so a re-run finishes an interrupted one.

Changelog:
  2026-09-28: created. Replaces persist_review_identities (directory.csv) and
    apply_retargets (retarget-people.csv) plus the fan-in/hydrate/fan-in
    round trip `realize` used to run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import emit, now_iso, write_json
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR
from packs.ingestion.primitives.deep_context.db import identity_queries, queries
from packs.ingestion.primitives.deep_context.db.identity_policy import (
    AFFIRMATIVE_MACHINE_ACTIONS,
    AFFIRMATIVE_MACHINE_APPROVALS,
)
from packs.ingestion.primitives.deep_context.db.models import PersonRow, ReviewAction, RowKind
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.enrich.profiles.projection import profile_payloads
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import load_assignment
from packs.ingestion.primitives.deep_context.db.readiness import CANONICAL_DB
from packs.ingestion.primitives.deep_context.shared.common import slugify
from packs.ingestion.primitives.enrich.profile_transforms import normalize_rapidapi
from packs.ingestion.primitives.imports.merge_people import (
    fill_profile_columns,
    group_key,
    has_work_history,
    merge_group,
)
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.schemas.people_schema import CONTACT_CARRY_COLUMNS, PEOPLE_SCHEMA_COLUMNS
from packs.shared.csv_io import CsvIO

DEFAULT_OUTPUT_DIR = DEFAULT_BASE_DIR / "merged"
REJECTING_ACTIONS = frozenset({ReviewAction.DETACH.value, ReviewAction.EXCLUDE.value})
# What a row keeps when its LinkedIn changes: the person's own contact trail.
KEPT_COLUMNS = frozenset({"id", "superseded_person_ids", "source_artifacts", *CONTACT_CARRY_COLUMNS})


def _linkedin_url(slug: str) -> str:
    return f"https://www.linkedin.com/in/{slug}"


def _relinked(row: PeopleRow, slug: str) -> PeopleRow:
    """The row under a different LinkedIn (or none): contact columns only."""
    columns = KEPT_COLUMNS if row.public_identifier else PEOPLE_SCHEMA_COLUMNS
    kept = {column: getattr(row, column) for column in columns}
    kept.update(public_identifier=slug, linkedin_url=_linkedin_url(slug) if slug else "")
    return PeopleRow.model_validate(kept)


def _is_accepted(action: str | None, kind: str) -> bool:
    """Verify or retarget; a synthetic card counts only when retargeted to a real LinkedIn."""
    if action not in AFFIRMATIVE_MACHINE_ACTIONS:
        return False
    return kind != RowKind.SYNTHETIC.value or action == ReviewAction.RETARGET.value


class ExportPeople:
    """SQLite roster and decisions in; final roster in SQLite and people.csv out."""

    def __init__(self, *, db: Db, out_dir: Path = DEFAULT_OUTPUT_DIR) -> None:
        self.db = db
        self.people_csv = Path(out_dir) / "people.csv"
        self.manifest_json = Path(out_dir) / "manifest.json"

    def run(self) -> dict[str, object]:
        roster = queries.imported_people(self.db)
        if not roster:
            return {
                "primitive": "deep_context_export_people",
                "status": "blocked",
                "reason": "the SQLite roster is empty; run bin/deep-context ensure-parents first",
            }
        parent_of = {row.person_id: row.parent_id for row in queries.people(self.db)}
        kinds = {row.row_key: (row.parent_id, row.kind) for row in identity_queries.links(self.db)}
        accepted: dict[str, str] = {}
        rejected: set[tuple[str, str]] = set()
        for review in identity_queries.review_rows(self.db, include_worth=False):
            parent_id, kind = kinds[review.key]
            if review.approved not in AFFIRMATIVE_MACHINE_APPROVALS:
                continue
            if _is_accepted(review.action, kind):
                accepted[parent_id] = (review.new_public_identifier or review.public_identifier).lower()
            elif review.action in REJECTING_ACTIONS:
                rejected.add((parent_id, review.public_identifier.lower()))

        groups: dict[str, list[PeopleRow]] = {}
        families: dict[str, set[str]] = {}
        unkeyable = 0
        for row in roster:
            parent_id = parent_of[row.id]
            slug = accepted.get(parent_id, row.public_identifier)
            if parent_id not in accepted and (parent_id, slug) in rejected:
                slug = ""
            if slug != row.public_identifier:
                row = _relinked(row, slug)
            key = group_key(row)
            if not key:
                unkeyable += 1
                continue
            groups.setdefault(key, []).append(row)
            families.setdefault(key, set()).add(parent_id)

        profiles = {
            result.normalized_profile.public_identifier: result
            for result in profile_payloads(self.db).values()
            if result.normalized_profile.present
        }
        merged = {key: merge_group(key, groups[key]) for key in sorted(groups)}
        accepted_slugs = set(accepted.values())
        needed = [row for row in merged.values() if row["public_identifier"] in accepted_slugs and not has_work_history(row)]
        for row in needed:
            result = profiles.get(row["public_identifier"])
            # One profile that cannot be read leaves that person's row as it is.
            try:
                raw = result.raw_payload() if result else None
                fill_profile_columns(row, normalize_rapidapi(raw, row["public_identifier"], row["linkedin_url"]))
            except Exception as exc:
                print(f"[realize] {row['id']}: profile not used: {type(exc).__name__}: {exc}"[:300],
                      file=sys.stderr, flush=True)

        parents_before = len(set(parent_of.values()))
        group_parent = self._merge_families(merged, families, parent_of)
        parent_slugs = {row.parent_id: row.display_slug for row in queries.parents(self.db)}
        new_people = tuple(
            PersonRow(
                row["id"], group_parent[key], slugify(row["full_name"], row["id"]),
                parent_slugs[group_parent[key]], row["full_name"], updated_at=now_iso(),
            )
            for key, row in merged.items()
            if row["id"] not in parent_of
        )
        final = tuple(PeopleRow.model_validate(row) for row in merged.values())
        self.db.project_rows(new_people)
        self.db.replace_imported_people(final)

        CsvIO.write_dict_rows(self.people_csv, PEOPLE_SCHEMA_COLUMNS, [row.to_row() for row in final])
        payload: dict[str, object] = {
            "primitive": "deep_context_export_people",
            "status": "completed",
            "people_csv": str(self.people_csv),
            "rows": len(final),
            "people_added": len(new_people),
            "parents_merged": parents_before - len({row.parent_id for row in queries.people(self.db)}),
            "accepted_identities": len(accepted),
            "rejected_identities": len(rejected),
            "dropped_unkeyable": unkeyable,
            "profiles_filled": sum(1 for row in needed if has_work_history(row)),
            "profiles_missing": sum(1 for row in needed if not has_work_history(row)),
            "updated_at": now_iso(),
        }
        write_json(self.manifest_json, payload)
        return payload

    def _merge_families(
        self,
        merged: dict[str, dict[str, str]],
        families: dict[str, set[str]],
        parent_of: dict[str, str],
    ) -> dict[str, str]:
        """One parent per final row: every family it spans merges into the elected one."""
        assignment = load_assignment(self.db)
        survivor_of: dict[str, str] = {}

        def current(parent_id: str) -> str:
            while parent_id in survivor_of:
                parent_id = survivor_of[parent_id]
            return parent_id

        group_parent: dict[str, str] = {}
        for key, row in merged.items():
            spanned = families[key] | ({parent_of[row["id"]]} if row["id"] in parent_of else set())
            touched = sorted({current(parent_id) for parent_id in spanned})
            survivor = assignment.elect(touched)
            for absorbed in touched:
                if absorbed != survivor:
                    self.db.merge_parents(survivor, absorbed)
                    survivor_of[absorbed] = survivor
            group_parent[key] = survivor
        return {key: current(parent_id) for key, parent_id in group_parent.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args(argv)
    payload = ExportPeople(db=open_existing_db(args.db), out_dir=Path(args.out_dir)).run()
    emit(payload)
    return 0 if payload["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
