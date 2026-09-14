"""Project canonical worth and settled identities onto the derived people CSV."""

from __future__ import annotations

import argparse
import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.db.context_queries import dossier_evidence_rows
from packs.ingestion.primitives.deep_context.db.identity_queries import links, review_rows
from packs.ingestion.primitives.deep_context.db.identity_views import pending_parent_ids
from packs.ingestion.primitives.deep_context.db.models import SourceChannel
from packs.ingestion.primitives.deep_context.db.queries import identifiers, people, sources
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.db.worth_views import worth_rows
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import read_imported_people, read_imported_rows
from packs.ingestion.primitives.deep_context.shared.common import CANONICAL_DB, DEFAULT_PEOPLE_CSV, emit
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.synthesis.facts import merge_disjoint_fact_records
from packs.ingestion.primitives.deep_context.synthesis.models import FactRecord, SynthesizedFacts
from packs.ingestion.primitives.deep_context.synthesis.rendering import render_fact_sections
from packs.ingestion.primitives.imports.common import write_manifest
from packs.ingestion.primitives.imports.directory import merge_jsonish_lists
from packs.ingestion.schemas.people_schema import (
    PEOPLE_SCHEMA_COLUMNS, latest_interaction, merge_interaction_counts,
    normalize_linkedin_url, normalize_people_row, stable_person_id_from_key,
)
from packs.shared.csv_io import CsvIO


class RealizePeople:
    """Apply SQLite decisions after raw fan-in, preserving the input as a backup."""

    def __init__(self, *, db: Db, people_csv: Path = DEFAULT_PEOPLE_CSV, dry_run: bool = False):
        self.db = db
        self.people_csv = people_csv
        self.dry_run = dry_run

    def run(self) -> dict[str, object]:
        members = {row.person_id: row.parent_id for row in people(self.db)
                   if not row.is_owner and not row.is_ghost}
        owners: dict[tuple[str, str], set[str]] = defaultdict(set)
        for identifier in identifiers(self.db):
            if identifier.person_id in members:
                owners[identifier.kind, identifier.normalized_value].add(members[identifier.person_id])
        channels: dict[str, set[str]] = defaultdict(set)
        for source in sources(self.db):
            if source.person_id in members:
                channels[members[source.person_id]].add(source.source)
        link_rows = {row.row_key: row for row in links(self.db)}
        worth = {row.parent_id: row for row in worth_rows(self.db)}
        approved, detached, excluded = set(), set(), set()
        profile_owners: dict[str, set[str]] = defaultdict(set)
        for decision in review_rows(self.db, include_worth=False):
            link = link_rows[decision.key]
            if decision.approved not in {"auto", "yes", "no"}:
                continue
            if decision.action == "exclude":
                parent_worth = worth.get(link.parent_id)
                if decision.approved == "auto" and parent_worth and parent_worth.human and parent_worth.human.decision == "yes":
                    detached.add(link.parent_id)
                else:
                    excluded.add(link.parent_id)
            elif decision.action == "detach" or link.kind == "synthetic":
                detached.add(link.parent_id)
            elif decision.action in {"verify", "retarget"} and decision.approved in {"auto", "yes"}:
                approved.add(link.parent_id)
                url = normalize_linkedin_url(decision.new_linkedin_url or decision.linkedin_url or "")
                if url:
                    profile_owners[url].add(link.parent_id)
        # Match workflow rejection: machine worth cannot overrule a human keep
        # or an imported LinkedIn identity; explicit human worth No can.
        kept = {row.parent_id for row in link_rows.values()
                if row.decision_approved == "yes" and row.decision_action in {"verify", "retarget"}}
        kept.update(parent_id for parent_id, values in channels.items() if SourceChannel.LINKEDIN in values)
        excluded.update(row.parent_id for row in worth.values()
                        if row.effective == "no" and (row.human is not None or row.parent_id not in kept))
        pending = pending_parent_ids(self.db)
        dossiers = {parent_id for parent_id in detached - approved - excluded - pending
                    if parent_id in kept or (parent_id in worth and worth[parent_id].effective == "yes")}
        changed = excluded | dossiers
        imported = {row.person_id: row for row in read_imported_people(self.people_csv)}
        metadata: dict[str, dict[str, str]] = defaultdict(dict)
        output = []
        ambiguous = []
        unassigned_profiles = 0
        unmapped = 0
        removed = 0
        for raw in read_imported_rows(self.people_csv):
            row = normalize_people_row(raw)
            person = imported.get(row["id"].lower())
            if person is None:
                output.append(row)
                continue
            direct = {members[value] for value in (person.person_id, *person.superseded_person_ids)
                      if value in members}
            direct.update(profile_owners[row["linkedin_url"]])
            if len(direct) == 1 and direct <= dossiers:
                contact_metadata = metadata[next(iter(direct))]
                counts = merge_interaction_counts(contact_metadata.get("interaction_counts"), row["interaction_counts"])
                contact_metadata.update(
                    interaction_counts=json.dumps(counts, ensure_ascii=False) if counts else "",
                    last_interaction=latest_interaction(contact_metadata.get("last_interaction"), row["last_interaction"]),
                    source_artifacts=merge_jsonish_lists(contact_metadata.get("source_artifacts", ""), row["source_artifacts"]),
                )
            contact = set().union(*(owners[kind, value] for kind, values in
                (("email", person.emails), ("phone", person.phones)) for value in values))
            assigned = direct or contact
            if not assigned:
                unmapped += 1
            if len(assigned) > 1:
                ambiguous.append(person.person_id)
            # An imported profile with only contact overlap can be another person.
            # Neither a proposed slug nor an ambiguous identifier proves ownership.
            unassigned_profile = not direct and SourceChannel.LINKEDIN in person.source_channels
            if unassigned_profile:
                unassigned_profiles += 1
            if assigned and assigned <= changed and not unassigned_profile:
                removed += 1
                continue
            for kind, values, primary, plural in (
                ("email", person.emails, "primary_email", "all_emails"),
                ("phone", person.phones, "primary_phone", "all_phones"),
            ):
                retained = [value for value in values if not (
                    len(owners[kind, value]) == 1 and owners[kind, value] <= changed
                    and not owners[kind, value] & direct
                )]
                if len(retained) != len(values):
                    row[primary] = retained[0] if retained else ""
                    row[plural] = json.dumps(retained)
            output.append(row)

        for parent_id in sorted(dossiers):
            dossier = dossier_evidence_rows(self.db, (parent_id,))
            evidence = DossierEvidence.from_rows((parent_id,), dossier)
            elected = tuple(row for row in dossier.facts if row.person_id is None) or dossier.facts
            records = tuple(record for row in elected if (
                record := FactRecord.from_payload({"facts": parse_json_object(row.facts_json)})
            ) is not None)
            merged = merge_disjoint_fact_records(records) or SynthesizedFacts()
            output.append(normalize_people_row({
                **metadata[parent_id],
                "id": stable_person_id_from_key(f"parent:{parent_id}"),
                "full_name": evidence.name,
                "summary": "\n\n".join(filter(None, (merged.relationship_to_owner, render_fact_sections(merged)))),
                "primary_email": next(iter(evidence.emails), ""),
                "all_emails": json.dumps(evidence.emails),
                "primary_phone": next(iter(evidence.phones), ""),
                "all_phones": json.dumps(evidence.phones),
                "source_channels": json.dumps(sorted(channels[parent_id])),
                "superseded_person_ids": json.dumps(sorted(
                    person_id for person_id, owner in members.items() if owner == parent_id
                )),
            }))
        payload = {
            "status": "dry_run" if self.dry_run else "completed",
            "people_csv": str(self.people_csv), "people": len(output),
            "excluded_parents": len(excluded), "dossier_parents": len(dossiers),
            "removed_rows": removed, "ambiguous_rows": len(ambiguous),
            "ambiguous_person_ids": ambiguous,
            "unmapped_rows": unmapped,
            "unassigned_profile_rows": unassigned_profiles,
        }
        if not self.dry_run:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            shutil.copy2(self.people_csv, self.people_csv.with_name(f"{self.people_csv.name}.{stamp}.bkup"))
            CsvIO.write_dict_rows(self.people_csv, PEOPLE_SCHEMA_COLUMNS, output)
            payload["artifacts"] = {"people_csv": str(self.people_csv)}
            return write_manifest("realize", payload, import_dir=self.db.db_path.parent)
        return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=CANONICAL_DB)
    parser.add_argument("--people-csv", type=Path, default=DEFAULT_PEOPLE_CSV)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    emit(RealizePeople(db=open_existing_db(args.db), people_csv=args.people_csv, dry_run=args.dry_run).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
