#!/usr/bin/env python3
"""Carry legacy Deep Context decisions and paid results onto cold parents.

An install that predates the SQLite store keeps its old artifacts under its
`.powerpacks` tree. `ensure-parents` mints fresh parents from the current
`merged/people.csv`; this stage then carries over, keyed by identifier (email,
phone, LinkedIn public identifier) onto those parents, exactly four things, in
this order:

  1. merges: legacy same-person families (index.json multi-child parents and
     accepted merge verdicts) whose members land on distinct cold parents
  2. facts: each legacy facts record, written as the cold parent's facts file
  3. human decisions from review.csv: worth marks and identity clicks
  4. Parallel research results, keyed by the cold parent's slug so enrichment
     reuses them instead of re-billing

Machine review rows, dossiers, the profile cache, synthetic rows and avatars
are not carried. A record whose identifiers hit no cold parent, or two, is
counted and left alone. A seeded store records `meta.seeded_at` and refuses
a second run.

Changelog:
- 2026-09-25: created; check routes legacy installs here instead of the
  whole-graph migrate-sqlite import (owner decision A).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.contact_fields import (
    emails_from_row,
    normalize_email,
    normalize_phone,
    phones_from_row,
)
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.common.legacy import (
    LEGACY_PARALLEL_HANDLE_RESULT,
    MESSAGE_LINKEDIN_PREFIX,
)
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.models import (
    HUMAN_DECISION_SOURCES,
    HUMAN_REVIEW_ACTIONS,
    PARENT_WORTH_PREFIX,
    ApprovedState,
    ArtifactKind,
    ArtifactProjection,
    ArtifactRow,
    HumanWorth,
    IdentifierKind,
    LinkRow,
    ProjectionStatus,
    ResearchRow,
    ResearchStatus,
    ReviewAction,
    ReviewSource,
    WriterSource,
    row_kind_for_key,
)
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError, open_existing_db
from packs.ingestion.primitives.deep_context.enrich.parallel_research.projection import (
    native_research_payload,
)
from packs.ingestion.primitives.deep_context.manifests.seed_manifest import SeedManifest
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    DEEP_RESEARCH_DIR,
    DEFAULT_PEOPLE_CSV,
    FACTS_DIR,
    emit,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node
from packs.ingestion.schemas.people_schema import extract_public_identifier, row_public_identifier
from packs.shared.csv_io import CsvIO

SEEDED_AT_KEY = "seeded_at"
# Written by the whole-graph import this stage replaces; a store carrying it
# already holds its legacy decisions.
LEGACY_IMPORTED_AT_KEY = "legacy_imported_at"

DEFAULT_LEGACY_ROOT = Path(".powerpacks")
LEGACY_DEEP_CONTEXT = Path("deep-context")
LEGACY_INDEX_JSON = LEGACY_DEEP_CONTEXT / "index.json"
LEGACY_FACTS_DIR = LEGACY_DEEP_CONTEXT / "facts"
LEGACY_RAW_DIR = LEGACY_DEEP_CONTEXT / "raw"
LEGACY_MERGE_VERDICTS_CSV = LEGACY_DEEP_CONTEXT / "merge-verdicts.csv"
LEGACY_MERGE_CANDIDATES_CSV = LEGACY_DEEP_CONTEXT / "merge-candidates.csv"
LEGACY_RESEARCH_DIR = LEGACY_DEEP_CONTEXT / "reconcile/deep-research"
LEGACY_PEOPLE_CSV = Path("network-import/merged/people.csv")
LEGACY_REVIEW_CSV = Path("network-import/overrides/review.csv")
LEGACY_SYNTHETIC_CSV = Path("network-import/overrides/synthetic-people.csv")
# The provider envelope enrichment writes today, and the retired normalized
# shape older installs hold instead.
NATIVE_RESULT_FILE = "00_parallel_result.json"
NORMALIZED_RESULT_FILE = "01_research_parallel.json"

CANDIDATE_EMAIL_PREFIX = "candidate:email:"
CANDIDATE_PHONE_PREFIX = "candidate:phone:"
LINKEDIN = "linkedin"
EMAIL = IdentifierKind.EMAIL.value
PHONE = IdentifierKind.PHONE.value
KINDS = (LINKEDIN, EMAIL, PHONE)
HUMAN_WORTH_VALUES = frozenset(item.value for item in HumanWorth)
HUMAN_APPROVALS = frozenset({ApprovedState.YES.value, ApprovedState.NO.value})

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_PARENT_ID_RE = re.compile(r"parent-[0-9a-f]{12}\Z")


class SeedRefused(StoreError):
    """The store is not a cold, unseeded one."""


def legacy_decisions_present(state_root: Path) -> bool:
    """Whether a `.powerpacks` tree holds pre-SQLite Deep Context decisions.

    Only the retired review export and the legacy dossier catalog count: raw
    bundles and facts files are written by current stages too.
    """
    root = Path(state_root)
    return (root / LEGACY_REVIEW_CSV).is_file() or (root / LEGACY_INDEX_JSON).is_file()


def carried_over_at(db: Db) -> str | None:
    """When this store received its legacy decisions, by either door, else None."""
    rows = db.query(
        "SELECT value FROM meta WHERE key IN (?, ?) ORDER BY key",
        (LEGACY_IMPORTED_AT_KEY, SEEDED_AT_KEY),
    )
    return str(rows[0]["value"]) if rows else None


def human_worth_mark(row: dict[str, str]) -> str | None:
    """The human worth mark a review row carries, or None for a machine row."""
    mark = _text(row.get("network_worth")).lower()
    if mark in HUMAN_WORTH_VALUES:
        return mark
    excluded = (
        _text(row.get("action")).lower() == ReviewAction.EXCLUDE.value
        and _text(row.get("approved")).lower() == ApprovedState.YES.value
    )
    return HumanWorth.NO.value if excluded else None


def human_identity_decision(row: dict[str, str]) -> tuple[str, str] | None:
    """The (action, approved) a human clicked on a review row, or None."""
    action = _text(row.get("action")).lower()
    approved = _text(row.get("approved")).lower()
    if approved in HUMAN_APPROVALS and action in HUMAN_REVIEW_ACTIONS:
        return action, approved
    return None


def _text(value: object) -> str:
    return str(value or "").strip()


def _slug(value: object) -> str:
    """A bare LinkedIn public identifier, or "" for anything that is not one."""
    text = _text(value)
    if not text:
        return ""
    if "linkedin.com/" in text.lower():
        return extract_public_identifier(text)
    text = text.lower().rstrip("/")
    if ":" in text or "@" in text or " " in text or _UUID_RE.match(text) or _PARENT_ID_RE.match(text):
        return ""
    return text


def _csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    return [
        {str(key): _text(value) for key, value in row.items() if key is not None}
        for row in CsvIO.read_dict_rows(path)
    ]


def _json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _last_record(path: Path) -> dict[str, Any]:
    """The record `project_parent_fact` will read: the last JSON object line."""
    record: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            record = parsed
    return record


@dataclass
class Ids:
    """Normalized identifiers of one legacy row, per kind."""

    values: dict[str, set[str]] = field(default_factory=lambda: {kind: set() for kind in KINDS})

    def add(self, kind: str, raw: object) -> None:
        if kind == EMAIL:
            value = normalize_email(_text(raw))
        elif kind == PHONE:
            value = normalize_phone(raw)
        else:
            value = _slug(raw)
        if value:
            self.values[kind].add(value)

    def update(self, other: Ids) -> None:
        for kind in KINDS:
            self.values[kind] |= other.values[kind]

    def copy(self) -> Ids:
        copied = Ids()
        copied.update(self)
        return copied


class ColdIndex:
    """Identifier -> cold parent, kept current while the seed merges parents."""

    def __init__(self, db: Db, people_csv: Path) -> None:
        self.parent_of = {row.person_id: row.parent_id for row in queries.people(db)}
        self.slug_of = {row.parent_id: str(row.display_slug or "") for row in queries.parents(db)}
        self.linkedin_parents: set[str] = set()
        self._survivor: dict[str, str] = {}
        self.index: dict[str, dict[str, set[str]]] = {kind: defaultdict(set) for kind in KINDS}
        for row in queries.identifiers(db):
            self.index[row.kind][row.normalized_value].add(self.parent_of[row.person_id])
        # The store keeps no LinkedIn identifier rows: the fan-in export that
        # ensure-parents read supplies each person's slug.
        for raw in _csv_rows(people_csv):
            person_id = _text(raw.get("id")).lower()
            value = _slug(row_public_identifier(raw))
            if value and person_id in self.parent_of:
                parent_id = self.parent_of[person_id]
                self.index[LINKEDIN][value].add(parent_id)
                self.linkedin_parents.add(parent_id)

    def current(self, parent_id: str) -> str:
        while parent_id in self._survivor:
            parent_id = self._survivor[parent_id]
        return parent_id

    def merge(self, survivor: str, absorbed: str) -> None:
        self._survivor[absorbed] = survivor
        if absorbed in self.linkedin_parents:
            self.linkedin_parents.add(survivor)

    def resolve(self, ids: Ids) -> set[str]:
        parents: set[str] = set()
        for kind in KINDS:
            for value in ids.values[kind]:
                parents |= self.index[kind].get(value, set())
        return {self.current(parent_id) for parent_id in parents}

    def decide(self, primary: Ids, secondary: Ids) -> set[str]:
        """Key-first: the identifiers the artifact is keyed on decide; the
        looked-up ones are consulted only when the key finds nothing."""
        return self.resolve(primary) or self.resolve(secondary)


class LegacyTree:
    """Person-id and slug lookups from the legacy install's own files."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.person: dict[str, Ids] = defaultdict(Ids)
        for row in _csv_rows(self.root / LEGACY_PEOPLE_CSV):
            person_id = _text(row.get("id")).lower()
            if not person_id:
                continue
            ids = self.person[person_id]
            ids.add(LINKEDIN, row_public_identifier(row))
            for value in emails_from_row(row):
                ids.add(EMAIL, value)
            for value in phones_from_row(row):
                ids.add(PHONE, value)
        index = _json_object(self.root / LEGACY_INDEX_JSON) or {}
        self.slugs: dict[str, dict[str, Any]] = index.get("slugs") or {}
        self.index_parents: dict[str, dict[str, Any]] = index.get("parents") or {}
        self.children_of_parent_id: dict[str, list[str]] = {}
        for raw in self.index_parents.values():
            parent_id = _text(raw.get("parent_id")).lower()
            if parent_id:
                self.children_of_parent_id[parent_id] = list(raw.get("children") or [])
        for info in self.slugs.values():
            person_id = _text(info.get("person_id")).lower()
            if not person_id:
                continue
            for value in info.get("emails") or []:
                self.person[person_id].add(EMAIL, value)
            for value in info.get("phones") or []:
                self.person[person_id].add(PHONE, value)
        self.review = _csv_rows(self.root / LEGACY_REVIEW_CSV)
        for row in self.review:
            person_id = _text(row.get("person_id")).lower()
            if not person_id:
                continue
            for value in row.get("match_emails", "").split("|"):
                self.person[person_id].add(EMAIL, value)
            for value in row.get("match_phones", "").split("|"):
                self.person[person_id].add(PHONE, value)

    def key_ids(self, key: str) -> Ids:
        """Identifiers a legacy person/row key carries by itself or by lookup."""
        key = _text(key).lower()
        ids = Ids()
        if not key:
            return ids
        if key.startswith(CANDIDATE_EMAIL_PREFIX):
            ids.add(EMAIL, key.removeprefix(CANDIDATE_EMAIL_PREFIX))
        elif key.startswith(CANDIDATE_PHONE_PREFIX):
            ids.add(PHONE, key.removeprefix(CANDIDATE_PHONE_PREFIX))
        elif key.startswith(MESSAGE_LINKEDIN_PREFIX):
            ids.add(LINKEDIN, key.removeprefix(MESSAGE_LINKEDIN_PREFIX))
        elif key.startswith(PARENT_WORTH_PREFIX):
            ids.update(self.parent_ids(key.removeprefix(PARENT_WORTH_PREFIX)))
        elif _PARENT_ID_RE.match(key):
            ids.update(self.parent_ids(key))
        elif not _UUID_RE.match(key):
            ids.add(LINKEDIN, key)
        if key in self.person:
            ids.update(self.person[key])
        return ids

    def slug_ids(self, child_slug: str) -> Ids:
        info = self.slugs.get(child_slug) or {}
        person_id = _text(info.get("person_id")).lower()
        ids = self.key_ids(person_id) if person_id else Ids()
        for value in info.get("emails") or []:
            ids.add(EMAIL, value)
        for value in info.get("phones") or []:
            ids.add(PHONE, value)
        return ids

    def parent_ids(self, parent_id: str) -> Ids:
        """A legacy parent id: its index.json children plus its raw bundle."""
        ids = Ids()
        for child in self.children_of_parent_id.get(parent_id, []):
            ids.update(self.slug_ids(child))
        ids.update(self.raw_ids(parent_id))
        return ids

    def raw_ids(self, subject: str) -> Ids:
        ids = Ids()
        payload = _json_object(self.root / LEGACY_RAW_DIR / f"{subject}.json") or {}
        for value in payload.get("emails") or []:
            ids.add(EMAIL, value)
        for value in payload.get("phones") or []:
            ids.add(PHONE, value)
        return ids

    def families(self) -> list[list[Ids]]:
        """Every legacy same-person family as the identifiers of its members."""
        families = [
            [self.slug_ids(child) for child in raw.get("children") or []]
            for raw in self.index_parents.values()
            if len(raw.get("children") or []) > 1
        ]
        accepted = {
            frozenset({row.get("slug_a", ""), row.get("slug_b", "")})
            for row in _csv_rows(self.root / LEGACY_MERGE_CANDIDATES_CSV)
        }
        for row in _csv_rows(self.root / LEGACY_MERGE_VERDICTS_CSV):
            pair = frozenset({row.get("slug_a", ""), row.get("slug_b", "")})
            if row.get("same_person", "").lower() == "true" and pair in accepted:
                families.append([self.slug_ids(row["slug_a"]), self.slug_ids(row["slug_b"])])
        return families

    def facts_files(self) -> list[Path]:
        return sorted((self.root / LEGACY_FACTS_DIR).glob("*.jsonl"))

    def research_results(self) -> list[tuple[str, Path]]:
        """(legacy handle, result file) for every research dir holding one."""
        directory = self.root / LEGACY_RESEARCH_DIR
        if not directory.is_dir():
            return []
        results: list[tuple[str, Path]] = []
        for result_dir in sorted(path for path in directory.iterdir() if path.is_dir()):
            path = result_dir / NATIVE_RESULT_FILE
            if not path.is_file():
                path = result_dir / NORMALIZED_RESULT_FILE
            if path.is_file():
                results.append((result_dir.name, path))
        return results

    def synthetic_rows(self) -> int:
        return len(_csv_rows(self.root / LEGACY_SYNTHETIC_CSV))


@dataclass
class _Tally:
    carried: int = 0
    duplicate_dropped: int = 0
    two_plus: int = 0
    unmatched: int = 0

    def one(self, parents: set[str]) -> str | None:
        """The single resolved parent, counting the misses."""
        if len(parents) == 1:
            return next(iter(parents))
        if parents:
            self.two_plus += 1
        else:
            self.unmatched += 1
        return None


class Seed(Node):
    """Carry legacy decisions and paid results onto the cold parents, once."""

    name = "deep_seed"
    inputs = (Artifact(path=str(CANONICAL_DB), external=True),)
    outputs = ()
    payload = SeedManifest
    manifest = ""

    def __init__(
        self,
        *,
        db: Db,
        legacy_root: Path = DEFAULT_LEGACY_ROOT,
        people_csv: Path = DEFAULT_PEOPLE_CSV,
        facts_dir: Path = FACTS_DIR,
        research_dir: Path = DEEP_RESEARCH_DIR,
    ) -> None:
        self.db = db
        self.legacy_root = Path(legacy_root)
        self.people_csv = Path(people_csv)
        self.facts_dir = Path(facts_dir)
        self.research_dir = Path(research_dir)

    def bindings(self) -> dict[str, str]:
        return {str(CANONICAL_DB): str(self.db.db_path)}

    def execute(self) -> SeedManifest:
        carried = carried_over_at(self.db)
        if carried is not None:
            raise SeedRefused(f"store already carries its legacy decisions ({carried}); seed runs once")
        cold = ColdIndex(self.db, self.people_csv)
        if not cold.parent_of:
            raise SeedRefused("store holds no people; run ensure-parents first")
        legacy = LegacyTree(self.legacy_root)

        merges, ambiguous = self._merge_families(cold, legacy)
        facts = self._carry_facts(cold, legacy)
        worth, identity, machine_rows = self._carry_decisions(cold, legacy)
        research = self._carry_research(cold, legacy)

        seeded_at = now_iso()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (SEEDED_AT_KEY, seeded_at))
        return SeedManifest(
            status="completed",
            legacy_root=str(self.legacy_root),
            merges_applied=merges,
            families_ambiguous=ambiguous,
            facts_carried=facts.carried,
            facts_duplicate_dropped=facts.duplicate_dropped,
            facts_two_plus=facts.two_plus,
            facts_unmatched=facts.unmatched,
            worth_carried=worth.carried,
            worth_two_plus=worth.two_plus,
            worth_unmatched=worth.unmatched,
            identity_carried=identity.carried,
            identity_two_plus=identity.two_plus,
            identity_unmatched=identity.unmatched,
            research_carried=research.carried,
            research_duplicate_dropped=research.duplicate_dropped,
            research_two_plus=research.two_plus,
            research_unmatched=research.unmatched,
            machine_review_rows_not_carried=machine_rows,
            synthetic_rows_not_carried=legacy.synthetic_rows(),
            seeded_at=seeded_at,
        )

    def _merge_families(self, cold: ColdIndex, legacy: LegacyTree) -> tuple[int, int]:
        """Merge the cold parents a legacy family spans; a member that itself
        spans two cold parents makes the family ambiguous and it is skipped."""
        merges = ambiguous = 0
        for members in legacy.families():
            resolved = [cold.resolve(ids) for ids in members]
            if any(len(parents) > 1 for parents in resolved):
                ambiguous += 1
                continue
            parents = {parent_id for hit in resolved for parent_id in hit}
            if len(parents) < 2:
                continue
            survivor = min(parents, key=lambda parent_id: (parent_id not in cold.linkedin_parents, parent_id))
            for absorbed in sorted(parents - {survivor}):
                self.db.merge_parents(survivor, absorbed)
                cold.merge(survivor, absorbed)
                merges += 1
        return merges, ambiguous

    def _carry_facts(self, cold: ColdIndex, legacy: LegacyTree) -> _Tally:
        tally = _Tally()
        chosen: dict[str, tuple[str, Path]] = {}
        for path in legacy.facts_files():
            subject = path.stem.lower()
            primary = legacy.key_ids(subject)
            primary.update(legacy.raw_ids(subject))
            secondary = primary.copy()
            record = _last_record(path)
            facts = record.get("facts") if isinstance(record.get("facts"), dict) else record
            owned = facts.get("owned_identifiers") if isinstance(facts.get("owned_identifiers"), dict) else {}
            for value in owned.get("emails") or []:
                secondary.add(EMAIL, value)
            for value in owned.get("phones") or []:
                secondary.add(PHONE, value)
            for value in owned.get("urls") or []:
                secondary.add(LINKEDIN, value)
            parent_id = tally.one(cold.decide(primary, secondary))
            if parent_id is None:
                continue
            updated_at = _text(record.get("updated_at"))
            prior = chosen.get(parent_id)
            if prior is not None and prior[0] >= updated_at:
                tally.duplicate_dropped += 1
                continue
            if prior is not None:
                tally.duplicate_dropped += 1
            chosen[parent_id] = (updated_at, path)
        self.facts_dir.mkdir(parents=True, exist_ok=True)
        for parent_id, (_, path) in chosen.items():
            target = self.facts_dir / f"{parent_id}.jsonl"
            target.write_bytes(path.read_bytes())
            project_parent_fact(self.db, target, parent_id)
        tally.carried = len(chosen)
        return tally

    def _carry_decisions(self, cold: ColdIndex, legacy: LegacyTree) -> tuple[_Tally, _Tally, int]:
        worth, identity, machine_rows = _Tally(), _Tally(), 0
        # Oldest first, so the newest human mark on one parent is the one kept.
        for row in sorted(legacy.review, key=lambda item: item.get("updated_at", "")):
            key = row.get("public_identifier", "").lower()
            mark = human_worth_mark(row)
            decision = human_identity_decision(row)
            if mark is None and decision is None:
                machine_rows += 1
                continue
            primary = legacy.key_ids(key)
            person_id = row.get("person_id", "").lower()
            if person_id:
                primary.update(legacy.key_ids(person_id))
            secondary = primary.copy()
            secondary.add(LINKEDIN, row.get("linkedin_url"))
            for value in row.get("match_emails", "").split("|"):
                secondary.add(EMAIL, value)
            for value in row.get("match_phones", "").split("|"):
                secondary.add(PHONE, value)
            parents = cold.decide(primary, secondary)
            if mark is not None:
                self._carry_worth(row, mark, worth.one(parents), worth)
            if decision is not None and not key.startswith((MESSAGE_LINKEDIN_PREFIX, PARENT_WORTH_PREFIX)):
                self._carry_identity(key, row, decision, identity.one(parents), identity)
        return worth, identity, machine_rows

    def _carry_worth(self, row: dict[str, str], mark: str, parent_id: str | None, tally: _Tally) -> None:
        if parent_id is None:
            return
        self.db.decide_worth(
            parent_id,
            mark,
            note=row.get("user_worth_note") or None,
            decided_at=row.get("updated_at") or None,
        )
        tally.carried += 1

    def _carry_identity(
        self,
        key: str,
        row: dict[str, str],
        decision: tuple[str, str],
        parent_id: str | None,
        tally: _Tally,
    ) -> None:
        if parent_id is None:
            return
        action, approved = decision
        replacement_url = row.get("new_linkedin_url") or None
        replacement_public_identifier = row.get("new_public_identifier") or None
        if action != ReviewAction.RETARGET.value:
            replacement_url = replacement_public_identifier = None
        self._ensure_link(key, parent_id, row)
        source = row.get("source", "")
        try:
            self.db.decide_identity(
                key,
                action,
                approved=approved,
                replacement_url=replacement_url,
                replacement_public_identifier=replacement_public_identifier,
                source=source if source in HUMAN_DECISION_SOURCES else ReviewSource.REVIEW.value,
                decided_at=row.get("updated_at") or None,
            )
        except StoreError as exc:
            print(f"[seed] identity decision not carried for {key}: {exc}", file=sys.stderr)
            tally.unmatched += 1
            return
        tally.carried += 1

    def _ensure_link(self, key: str, parent_id: str, row: dict[str, str]) -> None:
        """A human clicked this candidate: the cold store needs its links row
        before the decision can settle on it."""
        if self.db.query("SELECT 1 FROM links WHERE row_key=?", (key,)):
            return
        candidate = key.startswith("candidate:")
        proposal = bool(row.get("new_linkedin_url") or row.get("new_public_identifier"))
        self.db.project_rows((
            LinkRow(
                key,
                parent_id,
                row.get("public_identifier") or key,
                row_kind_for_key(key).value,
                row.get("linkedin_url") or None,
                None,
                candidate_origin=candidate,
                raw_import=candidate and not proposal,
                source=WriterSource.LEGACY_MIGRATION.value,
                updated_at=row.get("updated_at") or None,
            ),
        ))

    def _carry_research(self, cold: ColdIndex, legacy: LegacyTree) -> _Tally:
        tally = _Tally()
        seen: set[str] = set()
        for legacy_handle, path in legacy.research_results():
            payload = _json_object(path)
            try:
                native = native_research_payload(payload, legacy_handle)
            except ValueError:
                tally.unmatched += 1
                continue
            primary = Ids()
            for child in (legacy.index_parents.get(legacy_handle) or {}).get("children") or []:
                primary.update(legacy.slug_ids(child))
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            source = _text(metadata.get("source_identifier"))
            primary.add(EMAIL if "@" in source else PHONE, source)
            secondary = primary.copy()
            social = payload.get("social") if isinstance(payload.get("social"), dict) else {}
            secondary.add(EMAIL, social.get("primary_email"))
            secondary.add(PHONE, social.get("primary_phone"))
            linkedin_url = _text(native["content"].get("linkedin_url"))
            secondary.add(LINKEDIN, linkedin_url)
            parent_id = tally.one(cold.decide(primary, secondary))
            if parent_id is None:
                continue
            handle = cold.slug_of[parent_id]
            if handle in seen:
                tally.duplicate_dropped += 1
                continue
            seen.add(handle)
            self._project_research(handle, parent_id, native, linkedin_url)
            tally.carried += 1
        return tally

    def _project_research(self, handle: str, parent_id: str, native: dict[str, Any], linkedin_url: str) -> None:
        target = self.research_dir / handle / NATIVE_RESULT_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(native, indent=2, ensure_ascii=False).encode("utf-8")
        target.write_bytes(data)
        artifact_key = f"research:{handle}"
        now = now_iso()
        payload_json = data.decode("utf-8")
        self.db.project_rows((
            ArtifactProjection(
                artifact=ArtifactRow(
                    artifact_key=artifact_key,
                    kind=ArtifactKind.RESEARCH.value,
                    parent_id=parent_id,
                    path=str(target.resolve()),
                    content_fingerprint=hashlib.sha256(data).hexdigest(),
                    status=ProjectionStatus.PROJECTED.value,
                    input_fingerprint=LEGACY_PARALLEL_HANDLE_RESULT,
                    payload_json=payload_json,
                    projected_at=now,
                ),
                research=ResearchRow(
                    handle,
                    parent_id,
                    ResearchStatus.COMPLETE.value if linkedin_url else ResearchStatus.NO_MATCH.value,
                    None,
                    artifact_key,
                    payload_json,
                    now,
                ),
            ),
        ))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Carry legacy Deep Context decisions and paid results onto the cold parents, once."
    )
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--legacy-root", default=str(DEFAULT_LEGACY_ROOT), help="a .powerpacks tree")
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    node = Seed(
        db=open_existing_db(args.db),
        legacy_root=Path(args.legacy_root),
        people_csv=Path(args.people_csv),
    )
    try:
        payload = node.run()
    except SeedRefused as exc:
        print(json.dumps({"primitive": "seed", "status": "refused", "database": args.db, "error": str(exc)}), file=sys.stderr)
        return 1
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
