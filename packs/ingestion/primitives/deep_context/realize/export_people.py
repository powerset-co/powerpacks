"""Apply exact reviewed profile associations and export the SQLite parent roster.

Rows already sharing a judged parent union under an existing person id. A profile
selection never merges parents, and an unreviewed contact lookup is omitted.
Original people, facts and decisions stay in SQLite; no provider is called.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import emit, now_iso, parse_json_object, write_json
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR
from packs.ingestion.primitives.deep_context.db import identity_queries, queries
from packs.ingestion.primitives.deep_context.db.identity_policy import (
    AFFIRMATIVE_MACHINE_ACTIONS,
    AFFIRMATIVE_MACHINE_APPROVALS,
)
from packs.ingestion.primitives.deep_context.db.models import HUMAN_DECISION_SOURCES, ReviewAction, RowKind, SourceChannel
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.enrich.profiles.projection import profile_payloads
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.name_policy import profile_name_verdict, profile_names
from packs.ingestion.primitives.deep_context.db.readiness import CANONICAL_DB
from packs.ingestion.primitives.deep_context.shared.dossier_policy import name_match_review_parents
from packs.ingestion.primitives.enrich.profile_transforms import normalize_rapidapi
from packs.ingestion.primitives.imports.merge_people import (
    fill_profile_columns,
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
        links = {row.row_key: row for row in identity_queries.links(self.db)}
        held_parents = name_match_review_parents(self.db)
        people_by_candidate: dict[str, list[str]] = {}
        for membership in identity_queries.memberships(self.db):
            people_by_candidate.setdefault(membership.row_key, []).append(membership.person_id)
        accepted: dict[str, str] = {}
        rejected: set[tuple[str, str]] = set()
        for review in sorted(identity_queries.review_rows(self.db, include_worth=False),
                             key=lambda row: links[row.key].decision_source in HUMAN_DECISION_SOURCES):
            link = links[review.key]
            parent_id, kind = link.parent_id, link.kind
            if review.approved not in AFFIRMATIVE_MACHINE_APPROVALS:
                continue
            if _is_accepted(review.action, kind):
                url = review.new_linkedin_url if review.action == ReviewAction.RETARGET.value else review.linkedin_url
                if (not link.decision_action
                        and parse_json_object(link.judgment_payload_json).get("relationship_decision", {}).get("fingerprint")
                            != (link.judgment_fingerprint or "")
                        and profile_name_verdict(
                            self.db, parent_id, profile_names(self.db, parent_id, review.key, url or ""),
                        ) is not None):
                    continue
                slug = (review.new_public_identifier or review.public_identifier).lower()
                accepted.update((person_id, slug) for person_id in people_by_candidate.get(review.key, ()))
            elif review.action in REJECTING_ACTIONS:
                slug = review.public_identifier.lower()
                for person_id in people_by_candidate.get(review.key, ()):
                    rejected.add((person_id, slug))
                    if link.decision_source in HUMAN_DECISION_SOURCES and accepted.get(person_id) == slug:
                        accepted.pop(person_id)

        realized: list[PeopleRow] = []
        groups: dict[str, list[PeopleRow]] = {}
        for row in roster:
            source_name = row.full_name
            parent_id = parent_of[row.id]
            direct_linkedin = SourceChannel.LINKEDIN.value in row.source_channels.split(",")
            if direct_linkedin and row.id not in accepted:
                candidate = next((link for link in links.values() if link.parent_id == parent_id
                                  and link.public_identifier == row.public_identifier), None)
                veto = profile_name_verdict(self.db, parent_id, profile_names(
                    self.db, parent_id, candidate.row_key if candidate else "", row.linkedin_url,
                ))
                direct_linkedin = veto is None or veto.value != "wrong_person"
            slug = accepted.get(row.id, row.public_identifier if direct_linkedin else "")
            if row.id not in accepted and (row.id, slug) in rejected:
                slug = ""
            if slug != row.public_identifier:
                row = _relinked(row, slug)
            row = row.model_copy(update={"full_name": source_name})
            realized.append(row)
            if parent_id not in held_parents:
                groups.setdefault(parent_id, []).append(row)

        profiles = {
            result.normalized_profile.public_identifier: result
            for result in profile_payloads(self.db).values()
            if result.normalized_profile.present
        }
        merged = {key: merge_group(groups[key][0].id, groups[key]) for key in sorted(groups)}
        needed = [row for parent_id, row in merged.items()
                  if any(accepted.get(source.id) == row["public_identifier"] for source in groups[parent_id])
                  and not has_work_history(row)]
        for row in needed:
            result = profiles.get(row["public_identifier"])
            # One profile that cannot be read leaves that person's row as it is.
            try:
                raw = result.raw_payload() if result else None
                fill_profile_columns(row, normalize_rapidapi(raw, row["public_identifier"], row["linkedin_url"]))
            except Exception as exc:
                print(f"[realize] {row['id']}: profile not used: {type(exc).__name__}: {exc}"[:300],
                      file=sys.stderr, flush=True)

        final = tuple(PeopleRow.model_validate(row) for row in merged.values())
        self.db.replace_imported_people(tuple(realized))

        buffer = io.StringIO()
        writer = CsvIO.dict_writer(buffer, fieldnames=PEOPLE_SCHEMA_COLUMNS)
        writer.writeheader()
        writer.writerows(row.to_row() for row in final)
        content = buffer.getvalue().encode("utf-8")
        if not self.people_csv.exists() or self.people_csv.read_bytes() != content:
            self.people_csv.parent.mkdir(parents=True, exist_ok=True)
            self.people_csv.write_bytes(content)
        payload: dict[str, object] = {
            "primitive": "deep_context_export_people",
            "status": "completed",
            "people_csv": str(self.people_csv),
            "rows": len(final),
            "parents_held": len(held_parents),
            "people_added": 0,
            "parents_merged": 0,
            "accepted_identities": len(accepted),
            "rejected_identities": len(rejected),
            "dropped_unkeyable": 0,
            "profiles_filled": sum(1 for row in needed if has_work_history(row)),
            "profiles_missing": sum(1 for row in needed if not has_work_history(row)),
            "updated_at": now_iso(),
        }
        write_json(self.manifest_json, payload)
        return payload


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
