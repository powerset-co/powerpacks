"""people.csv joined to its deep-context leaves — the share stage's one reader.

Facts, raw bundles and parent dossiers are keyed by PARENT id, not by the
people.csv id; `review_store.parent_ids_by_person` is the one map between them.
Only body-free fields of `raw/<parent_id>.json` are ever read (direction, at,
channel, group names); message text never reaches a request.

Flow: `ShareEvidence(...).load()` -> `list[PersonEvidence]`; `owner_state()` ->
the mailbox owner's background for the request.

Changelog:
  2026-09-24: keyed parentless worth by slug; evidence date = facts file date.
  2026-09-24: created (split out of share.py).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import read_json, read_jsonl
from packs.ingestion.primitives.deep_context.candidates import effective_network_worth
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    FACTS_DIR,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    OWNER_JSON,
    PARENTS_DIR,
    RAW_DIR,
    load_index,
    load_owner,
    parse_list,
)
from packs.ingestion.primitives.deep_context.review_store import (
    load_override_rows,
    parent_ids_by_person,
    parent_worth_key,
)
from packs.ingestion.primitives.share.models import NO_MESSAGES, MessageStats, PersonEvidence
from packs.ingestion.primitives.share.csv_cells import cell_text
from packs.ingestion.schemas.people_schema import parse_interaction_counts, parse_source_channels
from packs.shared.csv_io import CsvIO


class ShareEvidence:
    """people.csv joined to its deep-context leaves, parsed once at the boundary.

    Facts, raw bundles and parent dossiers are keyed by PARENT id, not by the
    people.csv id — `review_store.parent_ids_by_person` is the one map between them.
    """

    def __init__(
        self,
        *,
        people_csv: Path = DEFAULT_PEOPLE_CSV,
        index_json: Path = INDEX_JSON,
        facts_dir: Path = FACTS_DIR,
        raw_dir: Path = RAW_DIR,
        dossier_dir: Path = DOSSIER_DIR,
        parents_dir: Path = PARENTS_DIR,
        overrides_csv: Path = LINKEDIN_OVERRIDES_CSV,
        owner_json: Path = OWNER_JSON,
    ) -> None:
        self.people_csv = Path(people_csv)
        self.index_json = Path(index_json)
        self.facts_dir = Path(facts_dir)
        self.raw_dir = Path(raw_dir)
        self.dossier_dir = Path(dossier_dir)
        self.parents_dir = Path(parents_dir)
        self.overrides_csv = Path(overrides_csv)
        self.owner_json = Path(owner_json)

    def owner_state(self) -> dict[str, Any]:
        owner = load_owner(self.owner_json) or {}
        work = (owner.get("work") or [{}])[0]
        return {
            "name": owner.get("name"),
            "company": work.get("company"),
            "title": work.get("title"),
            "locations": owner.get("locations") or [],
            "education": [entry.get("school") for entry in owner.get("education") or []],
        }

    def load(self, *, limit: int = 0) -> list[PersonEvidence]:
        index = load_index(self.index_json)
        slugs = index.get("slugs") or {}
        slug_by_person = {
            str(record.get("person_id") or ""): slug for slug, record in slugs.items() if record.get("person_id")
        }
        parent_slug_by_child = {
            child: parent_slug
            for parent_slug, record in (index.get("parents") or {}).items()
            for child in record.get("children") or []
        }
        parent_ids = parent_ids_by_person(self.index_json)
        overrides = load_override_rows(self.overrides_csv)

        people: list[PersonEvidence] = []
        for row in CsvIO.read_dict_rows(self.people_csv):
            person_id = str(row.get("id") or "").strip()
            if not person_id:
                continue
            parent_id = parent_ids.get(person_id.lower(), "")
            public_identifier = cell_text(row.get("public_identifier"))
            public_identifier = public_identifier.lower() if public_identifier else None
            worth_key = parent_worth_key(parent_id) if parent_id else (public_identifier or person_id)
            dossier = self._dossier(slug_by_person.get(person_id, ""), parent_slug_by_child)
            facts = self._facts(parent_id)
            people.append(
                PersonEvidence(
                    person_id=person_id,
                    public_identifier=public_identifier,
                    full_name=str(row.get("full_name") or "").strip(),
                    headline=cell_text(row.get("headline")),
                    current_title=cell_text(row.get("current_title")),
                    current_company=cell_text(row.get("current_company")),
                    city=cell_text(row.get("city")),
                    state=cell_text(row.get("state")),
                    country=cell_text(row.get("country")),
                    source_channels=parse_source_channels(row.get("source_channels")),
                    interaction_counts=parse_interaction_counts(row.get("interaction_counts")),
                    last_interaction=cell_text(row.get("last_interaction")),
                    superseded_person_ids=tuple(parse_list(row.get("superseded_person_ids"))),
                    network_worth=effective_network_worth(worth_key, overrides, self.facts_dir)["decision"],
                    dossier=dossier,
                    evidence_date=self._evidence_date(parent_id, facts),
                    facts=facts,
                    shared_overlaps=_overlaps(facts),
                    messages=self._messages(parent_id),
                )
            )
            if limit and len(people) >= limit:
                break
        return people

    def _evidence_date(self, parent_id: str, facts: dict[str, Any] | None) -> str | None:
        """The date the facts file was synthesized — the dossiers are rendered from
        it (parent dossiers carry no date of their own). None without facts."""
        if facts is None:
            return None
        return date.fromtimestamp((self.facts_dir / f"{parent_id}.jsonl").stat().st_mtime).isoformat()

    def _facts(self, parent_id: str) -> dict[str, Any] | None:
        if not parent_id:
            return None
        records = read_jsonl(self.facts_dir / f"{parent_id}.jsonl")
        if not records:
            return None
        facts = records[-1].get("facts")
        return facts if isinstance(facts, dict) else None

    def _dossier(self, slug: str, parent_slug_by_child: dict[str, str]) -> str | None:
        if not slug:
            return None
        parent_slug = parent_slug_by_child.get(slug, "")
        candidates = ([self.parents_dir / f"{parent_slug}.md"] if parent_slug else []) + [
            self.dossier_dir / f"{slug}.md"
        ]
        for path in candidates:
            if path.exists():
                return path.read_text(encoding="utf-8")
        return None

    def _messages(self, parent_id: str) -> MessageStats:
        """Body-free message stats. Reads ONLY direction/at/channel and the group
        names — `subject` and `text` are never touched, here or anywhere after."""
        if not parent_id:
            return NO_MESSAGES
        bundle = read_json(self.raw_dir / f"{parent_id}.json", None)
        if bundle is None:
            return NO_MESSAGES
        timestamps: list[str] = []
        channels: list[str] = []
        from_me = from_them = 0
        for message in bundle.get("messages") or []:
            at = str(message.get("at") or "")
            if at:
                timestamps.append(at)
            channel = str(message.get("channel") or "")
            if channel and channel not in channels:
                channels.append(channel)
            direction = str(message.get("direction") or "")
            if direction == "from_me":
                from_me += 1
            elif direction == "from_them":
                from_them += 1
        return MessageStats(
            first_at=min(timestamps) if timestamps else None,
            last_at=max(timestamps) if timestamps else None,
            from_me=from_me,
            from_them=from_them,
            group_count=len(bundle.get("groups") or []),
            channels=tuple(channels),
        )


def _overlaps(facts: dict[str, Any] | None) -> frozenset[str]:
    """`facts.shared_context[].overlap` values (school | employer | location | era | other)."""
    return frozenset(
        str(entry.get("overlap") or "")
        for entry in (facts or {}).get("shared_context") or []
        if isinstance(entry, dict)
    )
