"""Read selected original Gmail account observations without enrichment fields."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from packs.ingestion.primitives.common.jsonio import read_json
from packs.ingestion.primitives.discover.common import read_csv_rows
from packs.ingestion.primitives.discover.gmail.msgvault.util import normalize_name
from packs.ingestion.primitives.imports.common import ImportManifest
from packs.ingestion.primitives.imports.directory import merge_jsonish_lists
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.schemas.candidates_schema import candidate_key_for

GMAIL_IMPORT_CONTRACT = "gmail-source-only-v3"
GMAIL_SOURCE_COLUMNS = (
    "first_name", "last_name", "full_name", "primary_email", "source_artifacts",
    "interaction_counts", "last_interaction",
)


@dataclass(frozen=True)
class GmailAccount:
    account_email: str
    people_csv: Path


@dataclass(frozen=True)
class _NameConflict:
    email: str
    observed_names: tuple[str, ...]


def _original_names(account: GmailAccount) -> dict[str, tuple[str, ...]]:
    manifest_path = account.people_csv.with_name("manifest.json")
    manifest = read_json(manifest_path, {})
    if manifest.get("source") != "msgvault":
        return {}
    if (manifest["status"] != "completed" or manifest["account_email"] != account.account_email
            or Path(manifest["artifacts"]["people_csv"]).resolve() != account.people_csv.resolve()):
        raise ValueError(f"Gmail source manifest does not match selected account: {manifest_path}")
    conflicts = TypeAdapter(tuple[_NameConflict, ...]).validate_python(manifest.get("name_conflicts", ()))
    return {conflict.email.strip().lower(): conflict.observed_names for conflict in conflicts}


def source_people_from_accounts(accounts: tuple[GmailAccount, ...]) -> tuple[PeopleRow, ...]:
    """Read original account observations with only source-owned fields."""
    people = []
    for account in accounts:
        if not account.people_csv.is_file():
            raise FileNotFoundError(f"Gmail source people CSV missing: {account.people_csv}")
        fields, rows = read_csv_rows(account.people_csv)
        if not {"primary_email", "interaction_counts"}.issubset(fields):
            raise ValueError(f"Gmail people schema missing primary_email or interaction_counts: {account.people_csv}")
        names = _original_names(account)
        for raw in rows:
            row = PeopleRow.model_validate({column: raw[column] for column in GMAIL_SOURCE_COLUMNS if column in fields})
            key = candidate_key_for(row.primary_email)
            if not key:
                raise ValueError(f"Gmail source contact has no valid email: {account.people_csv}")
            row.primary_email = row.primary_email.strip().lower()
            row.all_emails = json.dumps([row.primary_email])
            row.source_channels = "gmail_msgvault"
            row.source_artifacts = merge_jsonish_lists(row.source_artifacts, str(account.people_csv))
            row.id = f"candidate:{key}"
            if row.primary_email in names:
                row.source_artifacts = merge_jsonish_lists(row.source_artifacts, str(account.people_csv.with_name("manifest.json")))
                people.extend(row.model_copy(update={"full_name": normalize_name(name, row.primary_email)})
                              for name in names[row.primary_email])
            else:
                people.append(row)
    return tuple(people)


def original_source_accounts(people_csv: Path) -> tuple[GmailAccount, ...] | None:
    """Read the selected accounts for the exact current Gmail import output."""
    gmail = ImportManifest.read("gmail", import_dir=people_csv.parent.parent)
    if (gmail.status != "completed" or gmail.input.get("pipeline_contract") != GMAIL_IMPORT_CONTRACT
            or Path(gmail.outputs["people_csv"]).resolve() != people_csv.resolve()):
        return None
    return TypeAdapter(tuple[GmailAccount, ...]).validate_python(gmail.input["accounts"])


def original_source_people(people_csv: Path) -> tuple[PeopleRow, ...] | None:
    """Expand the exact current Gmail import output to its selected account rows."""
    accounts = original_source_accounts(people_csv)
    return None if accounts is None else source_people_from_accounts(accounts)
