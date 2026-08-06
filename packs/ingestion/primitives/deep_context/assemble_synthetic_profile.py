"""Assemble synthetic people-rows from deep research (no real LinkedIn found).

Implements packs/ingestion/docs/synthetic-profiles-plan.md: for people the
Parallel.ai deep-research pass could NOT find a real LinkedIn for (stealth
founders, no-LinkedIn contacts, user detaches), build a people-schema row from
the research JSON so they stop being invisible in search. A synthetic row looks
exactly like a real one to the merge/index — no downstream special-casing —
except `enrichment_provider="synthetic"`, a `synth-…` public_identifier, and an
`approved` gate: high-completeness rows are `auto`, the rest sit PENDING in
`overrides/synthetic-people.csv` until the user approves (search is never
polluted by un-reviewed researched profiles). Idempotent upsert keyed by
public_identifier; a row the user decided (approved yes/no) is never rewritten.

Reads:  .powerpacks/deep-context/reconcile/deep-research/<handle>/01_research_parallel.json
        (+ research_queue.csv for contact identity, people.csv for carry columns)
Writes: .powerpacks/network-import/overrides/synthetic-people.csv

Changelog:
  2026-07-30 (boundary parse): the queue row + verdicts fallback are parsed ONCE
    into the frozen `ResearchContact` (`merged()`, later non-empty value wins), so
    `build_synthetic_row` and the identity/collision logic read typed attributes
    instead of `.get()` chains on a merged dict; the phase-1 accumulator is the
    `ParentGroup` dataclass instead of a six-key dict of lists; the seven chained
    counters became `AssemblyCounts`, one field per manifest counter; the queue
    loader joined the other loaders as `load_queue`; and the receipt's
    `research_dir.resolve()` try now wraps only the resolve, not the assignment.
    `execute()` reads top-to-bottom as read -> prune -> select -> build -> merge ->
    write -> stamp. No behavior change.
  2026-07-27 (declared contract): `AssembleSyntheticProfile` is a
    `pipeline/contract.py:Node` ("deep_assemble_synthetic"). The flow moved from
    `main()` into `execute()` unchanged (same flags, same pretty-printed result
    JSON); the typed payload has the result dict's keys verbatim. Declaration-only
    node (`manifest=""`): the write_manifest call here updates ANOTHER node's
    manifest — deep_research's ENRICH_MANIFEST receipt, stamping the chain's
    terminal "completed" — so it stays inside `execute()` and is deliberately NOT
    this node's manifest or a declared output. Inputs/outputs are declared via the
    producer-owned constants (QUEUE_CSV / RESEARCH_PROFILE_TEMPLATE from
    reconcile_deep_research and the canonical override output path) so graph
    edges are string-equal.
  2026-07-23 (audit dedup): now_iso import from common.jsonio instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.apply_retargets import CARRY_COLUMNS
from packs.ingestion.primitives.deep_context.candidates import (
    candidate_carry,
    candidate_person_id,
    candidate_row,
    is_candidate_id,
)
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV as MERGED_PEOPLE_CSV,
    ENRICH_MANIFEST,
    FACTS_DIR,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    VERDICTS_JSONL,
)
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.imports.common import write_manifest
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest, row_model_for
from packs.ingestion.primitives.deep_context.reconcile_deep_research import (
    DR_OUT_DIR,
    QUEUE_CSV,
    RESEARCH_PROFILE_TEMPLATE,
)
from packs.ingestion.primitives.deep_context.db import views
from packs.ingestion.primitives.deep_context.db.models import ApprovedState
from packs.ingestion.primitives.deep_context.db.projectors import project_manifest
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.schemas.candidates_schema import candidate_key_for
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS

ROOT = Path(__file__).resolve().parents[4]
CANONICAL_DB = ROOT / ".powerpacks" / "deep-context" / "deep-context.sqlite"
SYNTHETIC_PEOPLE_CSV = LINKEDIN_OVERRIDES_CSV.parent / "synthetic-people.csv"
USER_APPROVED = frozenset({ApprovedState.YES.value, ApprovedState.NO.value})
# The DECLARED artifact path is the default: these used to be re-spelled as
# repo-root-ABSOLUTE paths, so the CLI wrote to the checkout's `.powerpacks`
# while the declaration (and every other stage) named the cwd-relative one.
# They agreed only when run from the repo root; now they are the same object.
DEFAULT_OUT = SYNTHETIC_PEOPLE_CSV
DEFAULT_PEOPLE_CSV = MERGED_PEOPLE_CSV
SYNTHETIC_PROVENANCE_COLUMNS = [
    "source_parent_slug",
    "source_person_ids",
    "source_candidate_public_identifier",
]
SYNTHETIC_COLUMNS = (
    PEOPLE_SCHEMA_COLUMNS
    + SYNTHETIC_PROVENANCE_COLUMNS
    + ["approved", "synthetic_metadata"]
)
# The declared row shape of synthetic-people.csv, generated FROM SYNTHETIC_COLUMNS
# so the column list keeps one home (this module is the file's only writer).
SyntheticPersonRow = row_model_for("SyntheticPersonRow", SYNTHETIC_COLUMNS)

# Auto-approve bar: research completeness at/above this flows straight into the
# merge (approved=auto); below it the row waits for the user in the review file.
DEFAULT_AUTO_COMPLETENESS = 0.6


@dataclass(frozen=True)
class ResearchContact:
    """One researched subject's identity, parsed ONCE at the input boundary.

    Two loose string maps feed it — the research queue row (`research_queue.csv`)
    and the durable `verdicts.jsonl` fallback for legacy dirs — and this is where
    they stop being maps: everything downstream reads typed attributes. `merged`
    applies the sources in order, a non-empty value overriding the one before it,
    which is exactly the precedence the old dict-update loop had. The queue's other
    columns (bio, known_info, area_code, retarget_hint) are not part of the
    identity and are deliberately dropped here rather than carried unread.
    """

    handle: str
    display_name: str = ""
    primary_email: str = ""
    phone_e164: str = ""
    source_channel: str = ""
    # Lineage back to the dossier this research was queued for.
    source_parent_slug: str = ""
    source_person_ids: str = ""  # JSON array text
    source_candidate_public_identifier: str = ""

    @classmethod
    def merged(cls, handle: str, *sources: dict[str, str]) -> ResearchContact:
        values: dict[str, str] = {"handle": handle}
        names = {f.name for f in fields(cls)}
        for source in sources:
            for key, value in source.items():
                if value and key in names:
                    values[key] = value
        return cls(**values)


@dataclass
class ParentGroup:
    """Every research output that resolves to ONE current parent.

    A later `cluster_merge` can fold two researched people into a single parent, so
    a group can own more than one research dir; phase 2 unions them into exactly one
    synthetic row. Mutable by design — phase 1 accumulates into it.
    """

    current_slug: str
    profiles: list[dict[str, Any]] = field(default_factory=list)
    contacts: list[ResearchContact] = field(default_factory=list)
    person_ids: list[str] = field(default_factory=list)
    candidate_pubs: list[str] = field(default_factory=list)
    handles: list[str] = field(default_factory=list)


@dataclass
class AssemblyCounts:
    """The run's per-outcome tallies — one field per manifest counter, so every
    branch below says which number it is moving."""

    built: int = 0
    auto_approved: int = 0
    pending_review: int = 0
    preserved_user_rows: int = 0
    skipped_with_linkedin: int = 0
    skipped_unusable: int = 0
    skipped_worth_no: int = 0
    pruned_stale_machine_rows: int = 0
    collapsed_merged_parents: int = 0


def synth_public_identifier(email: str, phone: str, handle: str) -> str:
    """Stable synthetic identity key, preferring the strongest contact anchor."""
    if email:
        return f"synth-email-{hashlib.sha1(email.strip().lower().encode()).hexdigest()[:12]}"
    if phone:
        return f"synth-phone-{hashlib.sha1(phone.strip().encode()).hexdigest()[:12]}"
    return f"synth-x-{handle.strip().lower()}"


def _inherit_decision(existing: dict[str, dict[str, str]], pubs: set[str]) -> str:
    """The strongest human decision across a set of colliding synthetic pubs.

    When merged children's stale synthetic rows collapse onto one current parent,
    the survivor must not silently drop a decision any of them carried. Precedence:
    an explicit user gate (`yes`/`no`) wins over a machine gate (`auto`/blank);
    among competing user gates `no` (exclude) wins over `yes` (keep) — the safer,
    more-conservative call — so a person the user excluded stays excluded. Returns
    '' when no colliding row carries a user gate.
    """
    decisions = {
        (existing.get(pub, {}).get("approved") or "").strip().lower()
        for pub in pubs
    }
    if "no" in decisions:
        return "no"
    if "yes" in decisions:
        return "yes"
    return ""


def profile_is_usable(profile: dict[str, Any]) -> bool:
    """Completeness floor: a name plus at least one position or a location."""
    name = ((profile.get("person") or {}).get("full_name") or "").strip()
    if not name:
        return False
    has_position = any((p.get("company_name") or p.get("title")) for p in profile.get("positions") or [])
    loc = profile.get("location") or {}
    has_location = bool(loc.get("city") or loc.get("country"))
    return has_position or has_location


def build_synthetic_row(profile: dict[str, Any], contact: ResearchContact,
                        original: dict[str, str] | None, person_id: str,
                        auto_completeness: float = DEFAULT_AUTO_COMPLETENESS,
                        provenance: dict[str, str] | None = None) -> dict[str, str]:
    """Pure mapping: research JSON + contact identity (+ original people row for carry
    columns) -> synthetic people-schema row. No IO."""
    person = profile.get("person") or {}
    loc = profile.get("location") or {}
    meta = profile.get("metadata") or {}
    social = profile.get("social") or {}
    positions = [p for p in profile.get("positions") or [] if p.get("company_name") or p.get("title")]
    education = profile.get("education") or []
    current = next((p for p in positions if p.get("is_current")), None)
    provenance = provenance or {}

    row = {col: "" for col in SYNTHETIC_COLUMNS}
    pub = synth_public_identifier(contact.primary_email, contact.phone_e164, contact.handle)
    completeness = float(meta.get("estimated_completeness") or 0.0)
    row.update({
        "id": person_id or pub,
        "public_identifier": pub,
        "linkedin_url": "",  # that's the point
        "first_name": person.get("first_name") or "",
        "last_name": person.get("last_name") or "",
        "full_name": person.get("full_name") or contact.display_name,
        "headline": (profile.get("headline") or {}).get("text") or "",
        "summary": (profile.get("summary") or {}).get("text") or "",
        "city": loc.get("city") or "",
        "state": loc.get("state") or "",
        "country": loc.get("country") or "",
        "location_raw": loc.get("raw") or ", ".join(v for v in (loc.get("city"), loc.get("country")) if v),
        "work_experiences": json.dumps(positions, ensure_ascii=False) if positions else "",
        "education": json.dumps(education, ensure_ascii=False) if education else "",
        "current_title": (current or {}).get("title") or "",
        "current_company": (current or {}).get("company_name") or "",
        "entity_urn": f"synthetic:{person_id or pub}",
        "enrichment_provider": "synthetic",
        "enriched_at": now_iso(),
        "twitter_handle": social.get("twitter_handle") or "",
        "source_parent_slug": provenance.get("source_parent_slug") or "",
        "source_person_ids": provenance.get("source_person_ids") or "",
        "source_candidate_public_identifier": (
            provenance.get("source_candidate_public_identifier") or ""
        ),
        "approved": "auto" if completeness >= auto_completeness else "",
        "synthetic_metadata": json.dumps({
            "completeness": completeness,
            "name_confidence": person.get("confidence"),
            "gaps": meta.get("gaps") or [],
            "research_date": meta.get("research_date") or "",
            "research_method": meta.get("research_method") or "",
            "source_channel": meta.get("source_channel") or contact.source_channel,
        }, ensure_ascii=False),
    })
    for col in CARRY_COLUMNS:
        if original and original.get(col):
            row[col] = original[col]
    if not row.get("primary_email") and contact.primary_email:
        row["primary_email"] = contact.primary_email
    if not row.get("primary_phone") and contact.phone_e164:
        row["primary_phone"] = contact.phone_e164
    return row


def _completeness(profile: dict[str, Any]) -> float:
    return float((profile.get("metadata") or {}).get("estimated_completeness") or 0.0)


def _position_key(pos: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(pos.get("company_name") or "").strip().lower(),
        str(pos.get("title") or "").strip().lower(),
        str(pos.get("start_date") or "").strip().lower(),
    )


def _education_key(edu: dict[str, Any]) -> tuple[str, str]:
    return (
        str(edu.get("school_name") or edu.get("school") or "").strip().lower(),
        str(edu.get("degree") or "").strip().lower(),
    )


def merge_research_profiles(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministically union >1 research JSON for the SAME merged parent into one.

    Each merged child was researched separately, so a collapsed parent can carry two
    research outputs. We union positions and education (order-stable, deduped), keep
    the best headline/summary/location (from the most complete profile that has one),
    take the max completeness, and union gaps/identity — no LLM. Called only when a
    parent owns more than one child research dir; a single profile passes through.
    """
    usable = [p for p in profiles if isinstance(p, dict)]
    if len(usable) <= 1:
        return usable[0] if usable else {}
    # Most complete first — its scalars (headline/summary/name/location) win ties.
    ordered = sorted(usable, key=_completeness, reverse=True)

    def first_text(getter) -> str:
        for prof in ordered:
            value = getter(prof)
            if value:
                return value
        return ""

    def first_location() -> dict[str, Any]:
        for prof in ordered:
            loc = prof.get("location") or {}
            if loc.get("city") or loc.get("country"):
                return loc
        return ordered[0].get("location") or {}

    positions: list[dict[str, Any]] = []
    seen_pos: set[tuple[str, str, str]] = set()
    education: list[dict[str, Any]] = []
    seen_edu: set[tuple[str, str]] = set()
    for prof in ordered:
        for pos in prof.get("positions") or []:
            if not isinstance(pos, dict) or not (pos.get("company_name") or pos.get("title")):
                continue
            key = _position_key(pos)
            if key not in seen_pos:
                seen_pos.add(key)
                positions.append(pos)
        for edu in prof.get("education") or []:
            if not isinstance(edu, dict):
                continue
            key = _education_key(edu)
            if key != ("", "") and key not in seen_edu:
                seen_edu.add(key)
                education.append(edu)

    gaps: list[str] = []
    for prof in ordered:
        for gap in (prof.get("metadata") or {}).get("gaps") or []:
            text = str(gap or "").strip()
            if text and text not in gaps:
                gaps.append(text)

    best = ordered[0]
    person = dict(best.get("person") or {})
    metadata = dict(best.get("metadata") or {})
    metadata["estimated_completeness"] = max(_completeness(p) for p in ordered)
    metadata["gaps"] = gaps
    return {
        **best,
        "person": person,
        "headline": {"text": first_text(lambda p: (p.get("headline") or {}).get("text"))},
        "summary": {"text": first_text(lambda p: (p.get("summary") or {}).get("text"))},
        "location": first_location(),
        "positions": positions,
        "education": education,
        "metadata": metadata,
    }


def load_queue(path: Path) -> dict[str, dict[str, str]]:
    """`handle -> research queue row` for the CURRENT queue (empty when absent)."""
    rows: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("handle"):
                    rows[row["handle"]] = row
    return rows


def load_rows(path: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                pub = (row.get("public_identifier") or "").strip().lower()
                if pub:
                    rows[pub] = row
    return rows


def _json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in parsed if str(item).strip()))


def load_verdict_provenance(path: Path) -> dict[str, dict[str, str]]:
    """Recover stable dossier lineage for legacy research directories.

    The current research queue carries these fields directly. Older fixed-name
    queues were overwritten between runs, so verdicts.jsonl is the durable local
    fallback for already-produced research output such as detached LinkedIns.
    """
    by_parent: dict[str, dict[str, str]] = {}
    if not path.exists():
        return by_parent
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        parent_slug = str(row.get("parent_slug") or "").strip()
        if not parent_slug:
            continue
        emails = [str(value).strip() for value in row.get("match_emails") or [] if str(value).strip()]
        phones = [str(value).strip() for value in row.get("match_phones") or [] if str(value).strip()]
        by_parent[parent_slug] = {
            "source_parent_slug": parent_slug,
            "source_person_ids": json.dumps(row.get("person_ids") or [], ensure_ascii=False),
            "source_candidate_public_identifier": str(row.get("candidate_key") or "").strip(),
            "display_name": str(row.get("name") or "").strip(),
            "primary_email": emails[0] if emails else "",
            "phone_e164": phones[0] if phones else "",
        }
    return by_parent


def write_rows(path: Path, rows: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SYNTHETIC_COLUMNS)
        w.writeheader()
        for pub in sorted(rows):
            w.writerow({k: rows[pub].get(k, "") for k in SYNTHETIC_COLUMNS})


def people_lookup(people_csv: Path) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """(by normalized email, by last-10-digit phone) for carry-column lookup."""
    by_email: dict[str, dict[str, str]] = {}
    by_phone: dict[str, dict[str, str]] = {}
    if people_csv.exists():
        with people_csv.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                email = (row.get("primary_email") or "").strip().lower()
                if email and email not in by_email:
                    by_email[email] = row
                digits = "".join(c for c in (row.get("primary_phone") or "") if c.isdigit())[-10:]
                if digits and digits not in by_phone:
                    by_phone[digits] = row
    return by_email, by_phone


class AssembleSyntheticProfileManifest(StageManifest):
    """Typed payload — today's result dict key-for-key (`status` is the base
    field, so it renders first in the emitted JSON; every key and value is
    unchanged)."""
    primitive: str = "assemble_synthetic_profile"
    built: int = 0
    auto_approved: int = 0
    pending_review: int = 0
    preserved_user_rows: int = 0
    skipped_with_linkedin: int = 0
    skipped_unusable: int = 0
    skipped_worth_no: int = 0
    pruned_stale_machine_rows: int = 0
    collapsed_merged_parents: int = 0
    total_rows: int = 0
    out: str = ""
    elapsed_ms: int = 0


class AssembleSyntheticProfile(Node):
    """Builds synthetic people-rows from EXISTING research artifacts — free and
    local. Declaration-only node (`manifest=""`): its one manifest-touching side
    effect updates deep_research's ENRICH_MANIFEST receipt (the chain's terminal
    "completed"), which is another node's manifest and stays inside execute()."""

    name = "deep_assemble_synthetic"
    inputs = (
        Artifact(path=str(QUEUE_CSV), required=False),
        Artifact(path=RESEARCH_PROFILE_TEMPLATE, required=False),
        Artifact(path=str(VERDICTS_JSONL), required=False),
        Artifact(path=str(MERGED_PEOPLE_CSV), required=False),
        # Read to merge-not-clobber the receipt's existing keys; deep_research
        # declares this manifest, so the graph knows its producer.
        Artifact(path=str(ENRICH_MANIFEST), required=False),
    )
    outputs = (
        Artifact(path=str(SYNTHETIC_PEOPLE_CSV), row_model=SyntheticPersonRow, writes="upsert"),
    )
    payload = AssembleSyntheticProfileManifest
    manifest = ""

    def __init__(
        self,
        *,
        db: Db,
        research_dir: Path | None = None,
        queue_csv: Path | None = None,
        people_csv: Path | None = None,
        verdicts_jsonl: Path | None = None,
        out: Path | None = None,
        index_json: Path | None = None,
        facts_dir: Path | None = None,
        auto_completeness: float = DEFAULT_AUTO_COMPLETENESS,
        manifest: str | Path | None = None,
        prune: bool = True,
    ) -> None:
        # prune=False is for SCOPED assembly (the directory's guided-retarget
        # flow passes its one-person queue): the machine-row prune assumes the
        # queue covers the whole enrichment selection, so a partial queue must
        # never trigger it or every other machine-owned synthetic would vanish.
        self.db = db
        self.prune = prune
        self.research_dir = Path(research_dir or DR_OUT_DIR)
        self.queue_csv = Path(queue_csv or QUEUE_CSV)
        self.people_csv = Path(people_csv or DEFAULT_PEOPLE_CSV)
        self.verdicts_jsonl = Path(verdicts_jsonl or VERDICTS_JSONL)
        self.out = Path(out or DEFAULT_OUT)
        self.index_json = Path(index_json or INDEX_JSON)
        self.facts_dir = Path(facts_dir or FACTS_DIR)
        self.auto_completeness = auto_completeness
        # NOT named `manifest`: that is the Node ClassVar ("" = declaration-only),
        # and an instance attribute would shadow it into a template manifest write.
        self.manifest_arg = str(manifest or "").strip()

    def bindings(self) -> dict[str, str]:
        return {
            str(QUEUE_CSV): str(self.queue_csv),
            RESEARCH_PROFILE_TEMPLATE: str(self.research_dir / "{handle}" / "01_research_parallel.json"),
            str(VERDICTS_JSONL): str(self.verdicts_jsonl),
            str(MERGED_PEOPLE_CSV): str(self.people_csv),
            str(SYNTHETIC_PEOPLE_CSV): str(self.out),
        }

    def execute(self) -> AssembleSyntheticProfileManifest:
        started = time.monotonic()
        counts = AssemblyCounts()

        # ---- READ: every input this stage consumes, parsed once, up front. ----
        queue = load_queue(self.queue_csv)
        # No queue file at all means "nothing scopes this run": every research dir is
        # in scope and no machine-owned row is pruned.
        queue_is_current = self.queue_csv.exists()
        by_email, by_phone = people_lookup(self.people_csv)
        existing: dict[str, dict[str, str]] = {}
        for stored in self.db.query(
            "SELECT sp.public_identifier, sp.profile_json, l.decision_action, "
            "l.decision_approved, l.machine_approved FROM synthetic_profiles sp "
            "JOIN links l ON l.row_key=sp.candidate_key ORDER BY sp.public_identifier"
        ):
            try:
                row = json.loads(stored["profile_json"] or "{}")
            except json.JSONDecodeError:
                row = {}
            if not isinstance(row, dict):
                continue
            approved = stored["decision_approved"] or stored["machine_approved"] or row.get("approved") or ""
            if stored["decision_action"] in {"detach", "exclude"} and stored["decision_approved"]:
                approved = "no"
            row["approved"] = approved
            existing[stored["public_identifier"]] = {key: str(value or "") for key, value in row.items()}
        verdict_provenance = load_verdict_provenance(self.verdicts_jsonl)
        parent_worth = {
            person_id: row
            for row in views.worth_rows(self.db)
            for person_id in row["person_ids"]
        }
        # Child -> current-parent membership. A later cluster_merge can fold two former
        # parents into one; the per-person research dirs keyed on the OLD parent slugs are
        # re-keyed here so their outputs GROUP on the current parent instead of minting a
        # stale row each. No re-fetch — the existing research JSON is reused as-is.
        membership = self.db.query(
            "SELECT pe.person_id, pe.parent_id, p.display_slug "
            "FROM people pe JOIN parents p USING(parent_id) ORDER BY pe.person_id"
        )
        parent_map = {row["person_id"]: row["display_slug"] or row["parent_id"] for row in membership}
        parent_id_by_slug = {row["display_slug"] or row["parent_id"]: row["parent_id"] for row in membership}
        parent_id_by_person = {row["person_id"]: row["parent_id"] for row in membership}
        projection_rows: list[tuple[str, str, list[str], dict[str, str]]] = []

        # ---- PRUNE: machine-owned rows this queue no longer covers. -----------
        # The output is fixed and overwrite-in-place. Rebuild machine-owned rows
        # only from this queue; otherwise an old model-Yes synthetic could survive
        # after the current People decision moved to No. Explicit user gates remain
        # sticky and are never pruned here.
        if queue_is_current and self.prune:
            for pub, row in list(existing.items()):
                approved = str(row.get("approved") or "").strip().lower()
                handle = str(row.get("source_parent_slug") or "").strip()
                if not handle and pub.startswith("synth-x-"):
                    handle = pub.removeprefix("synth-x-")
                if handle and approved not in USER_APPROVED:
                    existing.pop(pub, None)
                    counts.pruned_stale_machine_rows += 1

        # ---- SELECT: group every usable no-LinkedIn research output under the
        # current parent that owns its person_ids. Entries sharing a parent
        # collapse into one synthetic row in the BUILD pass below.
        groups: dict[str, ParentGroup] = {}
        research_dirs = sorted(self.research_dir.iterdir()) if self.research_dir.exists() else []
        for pdir in research_dirs:
            if queue_is_current and pdir.name not in queue:
                continue
            research_json = pdir / "01_research_parallel.json"
            if not research_json.is_file():
                continue
            try:
                profile = json.loads(research_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue  # unreadable/corrupt research output: nothing to assemble
            if ((profile.get("social") or {}).get("linkedin_url") or "").strip():
                counts.skipped_with_linkedin += 1  # the retarget path owns this person
                continue
            if not profile_is_usable(profile):
                counts.skipped_unusable += 1
                continue
            contact = ResearchContact.merged(
                pdir.name,
                verdict_provenance.get(pdir.name) or {},
                queue.get(pdir.name) or {},
            )
            person_ids = _json_list(contact.source_person_ids)
            # The CURRENT parent that owns any of these person_ids (via index
            # membership), falling back to the stale slug — and finally to the dir
            # name — when the parent is still live/unindexed. Always non-empty, so
            # it is also the group key.
            current_slug = ""
            for pid in person_ids:
                current_slug = parent_map.get(pid.strip().lower(), "")
                if current_slug:
                    break
            current_slug = current_slug or contact.source_parent_slug or pdir.name
            entry = groups.setdefault(current_slug, ParentGroup(current_slug=current_slug))
            entry.profiles.append(profile)
            entry.contacts.append(contact)
            entry.handles.append(pdir.name)
            for pid in person_ids:
                if pid not in entry.person_ids:
                    entry.person_ids.append(pid)
            cand_pub = contact.source_candidate_public_identifier
            if cand_pub and cand_pub not in entry.candidate_pubs:
                entry.candidate_pubs.append(cand_pub)

        # ---- BUILD: one synthetic per current parent (union the research; retain
        # both candidate identities so the human can still pick), merged into the
        # existing file so a prior decision is preserved.
        for group_key in sorted(groups):
            entry = groups[group_key]
            if len(entry.profiles) > 1:
                counts.collapsed_merged_parents += 1
            profile = merge_research_profiles(entry.profiles)
            # The strongest contact anchor wins the primary identity/pub; the rest ride
            # along in provenance so both LinkedIn options stay visible in review.
            primary = next(
                (c for c in entry.contacts if c.primary_email or c.phone_e164),
                entry.contacts[0],
            )
            contact = replace(primary, handle=entry.current_slug or entry.handles[0])
            provenance = {
                "source_parent_slug": entry.current_slug or entry.handles[0],
                "source_person_ids": json.dumps(entry.person_ids, ensure_ascii=False),
                "source_candidate_public_identifier": (
                    contact.source_candidate_public_identifier
                    or (entry.candidate_pubs[0] if entry.candidate_pubs else "")
                ),
            }

            # Who this subject already is locally: a people.csv row supplies the carry
            # columns, and a subject that is only an import candidate carries its
            # candidate contact identity (emails/phones/counts/channels) instead.
            email = contact.primary_email.strip().lower()
            digits = "".join(ch for ch in contact.phone_e164 if ch.isdigit())[-10:]
            original = by_email.get(email) or (by_phone.get(digits) if digits else None)
            person_id = (original or {}).get("id", "") or (entry.person_ids[0] if entry.person_ids else "")
            if original is None:
                crow = candidate_row(candidate_key_for(email, contact.phone_e164))
                if crow:
                    original = candidate_carry(crow)
                    person_id = candidate_person_id(crow.get("candidate_key", ""))

            # Worth gate: the parent-level decision, falling back to the row's own.
            worth_row = parent_worth.get(str(person_id).lower())
            worth_decision = str((worth_row or {}).get("effective") or "maybe")
            if is_candidate_id(person_id) and worth_decision == "no":
                counts.skipped_worth_no += 1  # not worth adding — never mint a row
                continue

            row = build_synthetic_row(
                profile,
                contact,
                original,
                person_id,
                self.auto_completeness,
                provenance=provenance,
            )
            # Before provenance was persisted, handle-only subjects minted a
            # ``synth-x-<parent>`` key. Keep that stable identity when backfilling so
            # review decisions and any prior fan-in references do not fork.
            for handle in entry.handles:
                legacy_pub = f"synth-x-{handle}".lower()
                if legacy_pub in existing:
                    row["public_identifier"] = legacy_pub
                    row["id"] = existing[legacy_pub].get("id") or row["id"]
                    row["entity_urn"] = existing[legacy_pub].get("entity_urn") or f"synthetic:{row['id']}"
                    break
            pub = row["public_identifier"].lower()

            # ---- MERGE this row with what is already on disk. -----------------
            # When two stale rows collapse onto one current parent, the survivor inherits
            # the strongest human decision across every colliding row (its own pub + the
            # pubs the merged children would have minted — see _inherit_decision). An
            # explicit user gate (yes/no) beats a machine gate (auto/blank), and among user
            # gates `no` (exclude) beats `yes` (keep). A human decision is never silently
            # dropped on collapse, even when the survivor's own gate was the weaker one.
            colliding_pubs = {pub}
            for sibling in entry.contacts:
                colliding_pubs.add(synth_public_identifier(
                    sibling.primary_email, sibling.phone_e164, sibling.handle).lower())
            inherited = _inherit_decision(existing, colliding_pubs)
            previous = existing.get(pub) or {}
            if (previous.get("approved") or "").strip().lower() in USER_APPROVED:
                # The user's gate is sticky, but missing lineage is safe to repair.
                for column in SYNTHETIC_PROVENANCE_COLUMNS:
                    if not previous.get(column) and row.get(column):
                        previous[column] = row[column]
                # A collapsing sibling may carry a STRONGER decision than the survivor's own
                # (inherited already folds in previous's gate, so it never weakens it).
                if inherited:
                    previous["approved"] = inherited
                existing[pub] = previous
                counts.preserved_user_rows += 1
            else:
                if inherited:
                    row["approved"] = inherited
                existing[pub] = row
                counts.built += 1
                if row["approved"] == "auto":
                    counts.auto_approved += 1
                elif row["approved"] == "":
                    counts.pending_review += 1
            # Drop the sibling rows the merged children would have minted so a collapse
            # leaves exactly one synthetic per current parent.
            for other_pub in colliding_pubs:
                if other_pub != pub:
                    existing.pop(other_pub, None)
            parent_id = next(
                (parent_id_by_person[pid] for pid in entry.person_ids if pid in parent_id_by_person),
                parent_id_by_slug.get(entry.current_slug, ""),
            )
            if parent_id:
                projection_rows.append((pub, parent_id, entry.person_ids, existing[pub]))

        # ---- WRITE the upserted output and report. ----------------------------
        write_rows(self.out, existing)
        result = AssembleSyntheticProfileManifest(
            status="completed",
            built=counts.built,
            auto_approved=counts.auto_approved,
            pending_review=counts.pending_review,
            preserved_user_rows=counts.preserved_user_rows,
            skipped_with_linkedin=counts.skipped_with_linkedin,
            skipped_unusable=counts.skipped_unusable,
            skipped_worth_no=counts.skipped_worth_no,
            pruned_stale_machine_rows=counts.pruned_stale_machine_rows,
            collapsed_merged_parents=counts.collapsed_merged_parents,
            total_rows=len(existing),
            out=str(self.out), elapsed_ms=int((time.monotonic() - started) * 1000),
        )

        # ---- STAMP ANOTHER node's manifest: merge-update deep_research's
        # ENRICH_MANIFEST receipt to the chain's terminal "completed". Stays here,
        # not in the Node template — this node is declaration-only (manifest="").
        manifest_text = self.manifest_arg
        if not manifest_text:
            try:
                on_canonical_path = self.research_dir.resolve() == DR_OUT_DIR.resolve()
            except (OSError, RuntimeError):
                on_canonical_path = False  # unresolvable path: not the canonical one
            if on_canonical_path:
                manifest_text = str(ENRICH_MANIFEST)
        if manifest_text:
            manifest_path = Path(manifest_text)
            if manifest_path.name != "manifest.json":
                raise SystemExit("--manifest must end in manifest.json")
            try:
                current = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = {}  # no receipt yet, or an unreadable one: start fresh
            receipt = {
                **current,
                "stage": "enrich",
                "status": "completed",
                "assembly": result.to_payload(),
                "outputs": {
                    **(current.get("outputs") or {}),
                    "synthetic_people_csv": str(self.out),
                },
            }
            synthetic_dir = manifest_path.parent / "synthetic"
            synthetic_dir.mkdir(parents=True, exist_ok=True)
            inventory = [
                item for item in current.get("artifacts") or []
                if not str(item.get("artifact_key") or "").startswith("synthetic:")
            ]
            for pub, parent_id, person_ids, row in projection_rows:
                artifact_path = synthetic_dir / f"{hashlib.sha1(pub.encode()).hexdigest()}.json"
                data = json.dumps(row, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
                artifact_path.write_bytes(data)
                inventory.append({
                    "artifact_key": f"synthetic:{pub}",
                    "kind": "synthetic",
                    "path": artifact_path.relative_to(manifest_path.parent).as_posix(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "parent_id": parent_id,
                    "candidate_key": pub,
                    "public_identifier": pub,
                    "person_ids": person_ids,
                    "display_name": row.get("full_name") or row.get("name") or "",
                    "approved": row.get("approved") or "",
                })
            receipt["artifacts"] = inventory
            receipt.pop("updated_at", None)
            receipt.pop("created_at", None)
            write_manifest(
                manifest_path.parent.name, receipt,
                import_dir=manifest_path.parent.parent)
            project_manifest(self.db, manifest_path)
        return result


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Build synthetic people-rows for researched people with no real LinkedIn (free, local — reads existing research artifacts).")
    ap.add_argument("--research-dir", default=str(DR_OUT_DIR))
    ap.add_argument("--queue-csv", default=str(QUEUE_CSV))
    ap.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    ap.add_argument("--verdicts-jsonl", default=str(VERDICTS_JSONL))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--index-json", default=str(INDEX_JSON),
                    help="Deep-context index.json (child->current-parent membership) for re-keying merged parents")
    ap.add_argument("--facts-dir", default=str(FACTS_DIR))
    ap.add_argument("--db", default=str(CANONICAL_DB),
                    help="Canonical Deep Context SQLite database")
    ap.add_argument("--auto-completeness", type=float, default=DEFAULT_AUTO_COMPLETENESS,
                    help="Research completeness at/above this auto-approves the row (default %(default)s)")
    ap.add_argument("--manifest", help="Fixed Enrich Contacts manifest (defaults on the canonical research path)")
    args = ap.parse_args(argv)
    payload = AssembleSyntheticProfile(
        db=Db(Path(args.db)),
        research_dir=Path(args.research_dir),
        queue_csv=Path(args.queue_csv),
        people_csv=Path(args.people_csv),
        verdicts_jsonl=Path(args.verdicts_jsonl),
        out=Path(args.out),
        index_json=Path(args.index_json),
        facts_dir=Path(args.facts_dir),
        auto_completeness=args.auto_completeness,
        manifest=args.manifest,
    ).run()
    print(json.dumps(payload.to_payload(), indent=2))


if __name__ == "__main__":
    main()
