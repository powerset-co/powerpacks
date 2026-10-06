"""Read original source observations before repeated IDs are combined.

Changelog:
- 2026-10-06: a contact the store already holds keeps its person id. The source
  reader used to re-key every Gmail contact to `candidate:email:<address>`, which
  the projection took for a new person: a second parent with the address and no
  facts, while the first kept the facts and lost the address. `repair_split_contacts`
  folds those second parents back.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from pydantic import TypeAdapter

from packs.ingestion.primitives.common.contact_fields import emails_from_row, normalize_email, normalize_phone, phones_from_row
from packs.ingestion.primitives.discover.gmail.extract_gmail import people_rows_from_msgvault
from packs.ingestion.primitives.discover.gmail.msgvault.util import MsgvaultContactRow
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.models import (
    IdentifierKind,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonSourceRow,
    PersonSourcesProjection,
    SourceChannel,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import (
    ImportedPerson,
    _imported_people,
    _is_shared_mailbox,
    read_imported_people,
    stored_imported_people,
)
from packs.ingestion.primitives.imports.merge_people import MergePeopleInput, merge_group
from packs.ingestion.primitives.imports.gmail.source_people import original_source_people
from packs.ingestion.schemas.candidates_schema import candidate_key_for
from packs.ingestion.schemas.people_schema import CONTACT_CARRY_COLUMNS, parse_jsonish
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.shared.csv_io import CsvIO


def read_source_people(people_csv: Path, db: Db) -> tuple[ImportedPerson, ...]:
    """Read recorded source contacts, or the retained source roster after export."""
    manifest_path = people_csv.parent / "manifest.json"
    if not manifest_path.is_file():
        return ()
    payload = json.loads(manifest_path.read_text())
    if payload.get("primitive") == "deep_context_export_people":
        return stored_imported_people(db)
    if payload.get("stage") != "merge_people":
        return ()
    inputs = MergePeopleInput.model_validate(payload["input"])
    input_rows = TypeAdapter(dict[str, int]).validate_python(payload["stats"]["input_rows"])
    rows = []
    restored = 0
    unnamed = 0
    gmail_contacts: dict[Path, dict[str, MsgvaultContactRow]] = {}
    restored_contacts: dict[str, dict[Path, MsgvaultContactRow]] = {}
    # Rows this reader keys by a Gmail address, the only rows that can re-key a stored contact.
    gmail_ids: set[str] = set()
    for source in inputs.people_csvs:
        if source not in input_rows:
            continue
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"fan-in source people CSV missing: {path}")
        originals = original_source_people(path)
        if originals is not None:
            rows.extend(originals)
            gmail_ids.update(row.id for row in originals)
            continue
        source_root = next((parent.parent for parent in path.parents if parent.name == ".powerpacks"), path.parent)
        for raw in CsvIO.read_dict_rows(path):
            row = PeopleRow.model_validate(raw)
            if row.source_channels == SourceChannel.GMAIL.value and not row.id.startswith("candidate:"):
                key = candidate_key_for(row.primary_email)
                if not key:
                    print("[deep-context] Gmail source contact has no primary email; original ownership unresolved",
                          file=sys.stderr)
                    continue
                contact = {column: getattr(row, column) for column in (
                    *CONTACT_CARRY_COLUMNS, "first_name", "last_name", "full_name", "source_artifacts",
                    "public_identifier", "linkedin_url",
                )}
                names = set()
                for value in parse_jsonish(row.source_artifacts, []):
                    artifact = Path(value)
                    if artifact.name != "gmail_contacts_aggregated.csv":
                        continue
                    if not artifact.is_absolute():
                        artifact = source_root / artifact
                    if artifact not in gmail_contacts:
                        originals = tuple(MsgvaultContactRow.from_row(item)
                                          for item in CsvIO.read_dict_rows(artifact)) if artifact.is_file() else ()
                        gmail_contacts[artifact] = {item.email.strip().lower(): item for item in originals}
                    original = gmail_contacts[artifact].get(row.primary_email.strip().lower())
                    if original and original.display_name.strip():
                        names.add(original.display_name.strip())
                    for email in emails_from_row(row.to_row()):
                        if email == key.removeprefix("email:"):
                            continue
                        if original := gmail_contacts[artifact].get(email):
                            restored_contacts.setdefault(email, {})[artifact] = original
                name = next(iter(names)) if len(names) == 1 else ""
                unnamed += not bool(name)
                contact.update(id=f"candidate:{key}", full_name=name, first_name="", last_name="", primary_email=key.removeprefix("email:"),
                               all_emails=json.dumps([key.removeprefix("email:")]), primary_phone="", all_phones="")
                row = PeopleRow.model_validate(contact)
                restored += 1
                gmail_ids.add(row.id)
            rows.append(row)
    existing_ids = {row.id for row in rows}
    recovered = 0
    for email, originals in restored_contacts.items():
        candidate_id = f"candidate:{candidate_key_for(email)}"
        if candidate_id in existing_ids:
            continue
        source_rows = tuple(PeopleRow.model_validate(item) for item in people_rows_from_msgvault(
            tuple(originals.values()), [str(path) for path in originals],
        ))
        contact = merge_group(candidate_id, list(source_rows))
        names = {original.display_name.strip() for original in originals.values() if original.display_name.strip()}
        contact.update(full_name=next(iter(names)) if len(names) == 1 else "",
                       first_name="", last_name="", superseded_person_ids="")
        unnamed += len(names) != 1
        rows.append(PeopleRow.model_validate(contact))
        gmail_ids.add(candidate_id)
        recovered += 1
    if recovered:
        print(f"[deep-context] restored {recovered} additional original Gmail source contacts", file=sys.stderr)
    if restored:
        print(f"[deep-context] restored {restored} Gmail source contact keys; legacy lookup aliases omitted", file=sys.stderr)
    if unnamed:
        print(f"[deep-context] {unnamed} historical Gmail contact names unresolved; lookup names omitted", file=sys.stderr)
    rows = _keep_existing_contacts(rows, people_csv, db, gmail_ids)
    return tuple(person for person in _imported_people(tuple(rows)) if not _is_shared_mailbox(person))


def _keep_existing_contacts(
    rows: list[PeopleRow], people_csv: Path, db: Db | None, gmail_ids: set[str],
) -> list[PeopleRow]:
    """A contact the store already holds stays that person when the re-import would take it apart.

    Two shapes take a stored contact apart: a Gmail-keyed row carries one of its
    addresses under another id (the `candidate:email:<address>` re-key), or the
    rows under its own id carry none of its addresses (a LinkedIn row for a
    contact whose addresses came from messages). The fan-in row then stands in
    for its own rows and for the Gmail-keyed rows of its addresses. Rows from
    other sources are never touched: a new contact that shares a phone with a
    stored one is still a new contact.
    """
    if db is None:
        return rows
    existing = {row.person_id for row in queries.people(db)}
    keyed = [(row, _contact_keys(emails_from_row(row.to_row()), phones_from_row(row.to_row()))) for row in rows]
    own_keys: dict[str, set[tuple[str, str]]] = {}
    for row, keys in keyed:
        own_keys.setdefault(row.id, set()).update(keys)
    kept: list[ImportedPerson] = []
    kept_emails: set[tuple[str, str]] = set()
    for person in read_imported_people(people_csv):
        if person.person_id not in existing:
            continue
        mine = _contact_keys(person.emails, person.phones)
        emails = {key for key in mine if key[0] == IdentifierKind.EMAIL.value}
        rekeyed = any(row.id != person.person_id and row.id in gmail_ids and keys & emails for row, keys in keyed)
        stripped = bool(mine) and person.person_id in own_keys and not own_keys[person.person_id]
        if rekeyed or stripped:
            kept.append(person)
            kept_emails |= emails
    if not kept:
        return rows
    kept_ids = {person.person_id for person in kept}
    remaining = [row for row, keys in keyed
                 if row.id not in kept_ids and not (row.id in gmail_ids and keys & kept_emails)]
    print(f"[deep-context] kept {len(kept)} existing contacts under their current person ids", file=sys.stderr)
    return remaining + [person.index_row for person in kept]


def _contact_keys(emails: Iterable[str], phones: Iterable[str]) -> set[tuple[str, str]]:
    keys = {(IdentifierKind.EMAIL.value, normalize_email(email)) for email in emails}
    keys.update((IdentifierKind.PHONE.value, normalize_phone(phone)) for phone in phones)
    return {key for key in keys if key[1]}


def repair_split_contacts(db: Db, people: tuple[ImportedPerson, ...]) -> tuple[int, int]:
    """Fold back the second parent an earlier re-key made for a contact the store already held.

    The re-key gave the contact's own address a second person, `candidate:email:<address>`,
    under a second parent; only that exact person is the duplicate. A shared phone or a
    different person at the same address is not. A second parent holding nothing paid or
    decided is deleted (its rows cascade); one holding evidence or a decision is merged
    into the contact's parent, keeping both children. Returns (deleted, merged).
    """
    stored = {row.person_id: row for row in queries.people(db)}
    deleted = merged = 0
    for person in people:
        prior = stored.get(person.person_id)
        if prior is None:
            continue
        others = {f"candidate:{IdentifierKind.EMAIL.value}:{key}"
                  for key in (normalize_email(email) for email in person.emails) if key}
        for other in sorted(others):
            row = stored.get(other)
            if row is None or other == person.person_id or row.parent_id == prior.parent_id:
                continue
            family = [member for member in stored.values() if member.parent_id == row.parent_id]
            if queries.parent_has_paid_work(db, row.parent_id):
                db.merge_parents(prior.parent_id, row.parent_id)
                for member in family:
                    stored[member.person_id] = replace(member, parent_id=prior.parent_id)
                merged += 1
            else:
                db.delete_parent(row.parent_id)
                for member in family:
                    del stored[member.person_id]
                deleted += 1
    return deleted, merged


def retain_source_identifiers(db: Db, people: tuple[ImportedPerson, ...], imported: tuple[ImportedPerson, ...]) -> None:
    """Project exact source ownership; copied aggregate identifiers are not aliases."""
    if not people:
        return
    source_ids = {person.person_id for person in people}
    current_people = {row.person_id: row for row in queries.people(db)}
    db.project_rows(tuple(replace(current_people[person.person_id], display_name=person.display_name)
                          for person in people))
    db.project_rows(tuple(
        PersonIdentifiersProjection(person.person_id, tuple(
            PersonIdentifierRow(person.person_id, kind, value, value)
            for kind, values in ((IdentifierKind.EMAIL.value, person.emails),
                                 (IdentifierKind.PHONE.value, person.phones)) for value in values
        )) for person in people
    ))
    db.project_rows(tuple(PersonSourcesProjection(person.person_id, tuple(
        PersonSourceRow(person.person_id, channel) for channel in person.source_channels
    )) for person in people))
    current = {row.id: row for row in queries.imported_people(db)}
    current.update((person.person_id, person.index_row) for person in people)
    aggregate_ids = set()
    for row in imported:
        aliases = row.superseded_person_ids
        aggregate_ids.update(value for value in (row.person_id, *aliases)
                             if value not in source_ids and not value.startswith("candidate:"))
    db.replace_imported_people(tuple(row for key, row in current.items() if key not in aggregate_ids))
    db.project_rows(tuple(PersonIdentifiersProjection(person_id, ())
                          for person_id in aggregate_ids.intersection(current_people)))
