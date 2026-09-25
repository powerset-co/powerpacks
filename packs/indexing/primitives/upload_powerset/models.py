#!/usr/bin/env python3
"""Frozen value types for the Powerset upload, parsed once at the boundary.

Flow: share table row -> ShareDecisionRow from the deep-context store; people.csv row ->
LocalPerson (channel labels already mapped to cloud names); local_person_profiles
row -> PersonProfile (the persons upsert payload); operator_person_sources row ->
SourceRow; contact_tags row -> TagRow. Everything downstream of these
constructors takes typed values.

Changelog:
  2026-09-24: the share contract is the store's `share` table row.
  2026-09-24: created; namespace definitions have one owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packs.ingestion.schemas.people_schema import parse_interaction_counts, parse_source_channels

# Local people.csv channel label -> cloud operator_person_sources.source_channel.
# A local label missing from this table contributes no source row.
CLOUD_SOURCE_CHANNEL = {
    "gmail_msgvault": "gmail",
    "imessage": "imessage",
    "whatsapp": "whatsapp",
    "linkedin_csv": "linkedin",
}

DISCOVERY_METHOD = "powerpacks"
PRIVATE_TAG = "private"


@dataclass(frozen=True)
class Namespace:
    logical: str
    table: str
    doc_key: str | None
    write_schema: dict[str, dict[str, Any]]
    person_grain: bool


@dataclass(frozen=True)
class LocalPerson:
    """One merged/people.csv row, reduced to what the cloud needs."""

    person_id: str
    public_identifier: str
    channels: tuple[str, ...]
    interaction_counts: dict[str, int] = field(default_factory=dict)
    last_interaction: str = ""
    primary_email: str = ""
    primary_phone: str = ""

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> "LocalPerson":
        channels = [CLOUD_SOURCE_CHANNEL[label] for label in parse_source_channels(row.get("source_channels"))
                    if label in CLOUD_SOURCE_CHANNEL]
        return cls(
            person_id=str(row["id"]).strip(),
            public_identifier=str(row.get("public_identifier") or "").strip().lower(),
            channels=tuple(sorted(set(channels))),
            interaction_counts=parse_interaction_counts(row.get("interaction_counts")),
            last_interaction=str(row.get("last_interaction") or "").strip(),
            primary_email=str(row.get("primary_email") or "").strip(),
            primary_phone=str(row.get("primary_phone") or "").strip(),
        )

    def source_identifier(self, channel: str) -> str:
        """The cloud source_identifier for a channel: the address the channel reached."""
        return {
            "gmail": self.primary_email,
            "imessage": self.primary_phone,
            "whatsapp": self.primary_phone,
            "linkedin": self.public_identifier,
        }[channel]


@dataclass(frozen=True)
class SourceRow:
    """One operator_person_sources row for this operator (discovery_method=powerpacks)."""

    person_id: str
    source_channel: str
    source_identifier: str
    total_interactions: int = 0
    last_interaction_at: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        """The table's UNIQUE (operator_id, person_id, source_channel, source_identifier)
        minus the operator, which is fixed for one run."""
        return (self.person_id, self.source_channel, self.source_identifier)


@dataclass(frozen=True)
class TagRow:
    """One contact_tags row: group_key is the LinkedIn public_identifier."""

    person_id: str
    group_key: str
    tag: str = PRIVATE_TAG


@dataclass(frozen=True)
class PersonProfile:
    """The persons-upsert payload, read from local_person_profiles."""

    id: str
    public_identifier: str
    public_profile_url: str | None
    first_name: str | None
    last_name: str | None
    full_name: str | None
    headline: str | None
    summary: str | None
    profile_picture_url: str | None
    city: str | None
    state: str | None
    country: str | None
    location_raw: str | None
    hydrated_context: str | None
    x_twitter_handle: str | None
    x_twitter_followers: int | None
    linkedin_followers: int | None
    linkedin_connections: int | None
    ig_followers: int | None
    inferred_birth_year: int | None

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "PersonProfile":
        def text(name: str) -> str | None:
            value = row.get(name)
            return str(value) if value not in (None, "") else None

        def number(name: str) -> int | None:
            value = row.get(name)
            return int(value) if value not in (None, "") else None

        hydrated = row.get("hydrated_context")
        return cls(
            id=str(row["person_id"]),
            public_identifier=str(row["public_identifier"]).lower(),
            public_profile_url=text("public_profile_url") or text("linkedin_url"),
            first_name=text("first_name"),
            last_name=text("last_name"),
            full_name=text("full_name"),
            headline=text("headline"),
            summary=text("summary"),
            profile_picture_url=text("profile_picture_url"),
            city=text("city"),
            state=text("state"),
            country=text("country"),
            location_raw=text("location_raw"),
            hydrated_context=hydrated if isinstance(hydrated, str) and hydrated else None,
            x_twitter_handle=text("x_twitter_handle"),
            x_twitter_followers=number("x_twitter_followers"),
            linkedin_followers=number("linkedin_followers"),
            linkedin_connections=number("linkedin_connections"),
            ig_followers=number("ig_followers"),
            inferred_birth_year=number("inferred_birth_year"),
        )


@dataclass(frozen=True)
class CloudState:
    """Everything the plan needs to know about the cloud before it writes.

    `cloud_id_by_person` maps a local person id to the cloud `persons.id` for the
    same public_identifier — the cloud's unique key. They are equal for every
    person minted by the shared uuid5 recipe; where the cloud minted its own id,
    the cloud id is the one its foreign keys and documents carry.
    """

    cloud_id_by_person: dict[str, str]
    operator_sources: tuple[SourceRow, ...]
    operator_ids_by_person: dict[str, tuple[str, ...]]
    private_tag_keys: frozenset[str]
    present_entity_ids: dict[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class NamespacePlan:
    """One TurboPuffer namespace's work.

    ``upsert_ids`` are person ids for the person-grain namespaces (people,
    summaries, education) and entity ids for companies/schools;
    ``patch_person_ids`` is always person ids and is empty for companies/schools.
    """

    logical: str
    namespace: str
    upsert_ids: tuple[str, ...]
    patch_person_ids: tuple[str, ...]


@dataclass(frozen=True)
class UploadPlan:
    operator_id: str
    persons_upsert: tuple[str, ...]
    skipped_no_linkedin: tuple[str, ...]
    sources_insert: tuple[SourceRow, ...]
    sources_delete: tuple[SourceRow, ...]
    namespaces: tuple[NamespacePlan, ...]
    allowed_operator_ids: dict[str, tuple[str, ...]]
    tags_put: tuple[TagRow, ...]
    tags_delete: tuple[TagRow, ...]

    def counts(self) -> dict[str, Any]:
        return {
            "persons_upsert": len(self.persons_upsert),
            "skipped_no_linkedin": len(self.skipped_no_linkedin),
            "sources_insert": len(self.sources_insert),
            "sources_delete": len(self.sources_delete),
            "tags_put": len(self.tags_put),
            "tags_delete": len(self.tags_delete),
            "namespaces": {
                plan.logical: {"upsert": len(plan.upsert_ids), "patch_people": len(plan.patch_person_ids)}
                for plan in self.namespaces
            },
        }


@dataclass(frozen=True)
class UploadResult:
    persons_upserted: int = 0
    sources_inserted: int = 0
    sources_deleted: int = 0
    tags_put: int = 0
    tags_deleted: int = 0
    docs_upserted: dict[str, int] = field(default_factory=dict)
    docs_patched: dict[str, int] = field(default_factory=dict)
