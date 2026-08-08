"""Paths and small identity helpers shared by Deep Context and logbook."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from packs.ingestion.primitives.common.contact_fields import normalize_name_key
from packs.ingestion.primitives.common.paths import (
    DEFAULT_BASE_DIR,
    DEFAULT_PROFILE_CACHE_DIR,
)
from packs.ingestion.primitives.deep_context.db.models import (
    OwnerEducation,
    OwnerProfile,
    OwnerWork,
)
# wacli's E.164-ish canonicalizer (bare 10 digits -> +1, JID-aware). Despite the
# alias, this is not primitives.common.contact_fields.normalize_phone (a stricter,
# no-country-code-default function) and duplicates .canonicalize_phone's digit
# logic minus JID handling.
from packs.ingestion.primitives.discover.messages.wacli.util import (
    canonicalize_phone as normalize_phone,
)

_REPO_ROOT = Path(__file__).resolve().parents[5]
normalize_name = normalize_name_key

# --- Fixed output layout (one dir, overwrite in place; no ledgers, no run ids) ---
# Below, "written by" / "read by" name the stage subpackage, not the file — most
# reads go through CANONICAL_DB (queries.py), not by reopening these paths.
ROOT = Path(".powerpacks/deep-context")
CANONICAL_DB = ROOT / "deep-context.sqlite"  # the pipeline's real state; nearly every stage opens it directly
RAW_DIR = ROOT / "raw"  # written: collection (collect_person_context); read: synthesis, migration's one-time import
FACTS_DIR = ROOT / "facts"  # written: synthesis (synthesize_person_context); read: migration's one-time import only
# DOSSIER_DIR: written by synthesis (compose_dossier); read by merge_candidates
# (clustering evidence) and synthesis.validate_dossiers.
DOSSIER_DIR = ROOT / "dossiers"
INDEX_MD = ROOT / "index.md"  # written and only read by compose_dossier — human catalog, not pipeline state
MERGE_CSV = ROOT / "merge-candidates.csv"
MERGE_MD = ROOT / "merge-candidates.md"
PARENTS_DIR = ROOT / "parents"  # merged canonical-person dossiers (link to children)

# Per-parent artifacts use one fixed path template per stage.
RAW_BUNDLE_TEMPLATE = str(RAW_DIR / "{parent_id}.json")
RAW_MANIFEST = RAW_DIR / "manifest.json"
FACTS_TEMPLATE = str(FACTS_DIR / "{parent_id}.jsonl")
FACTS_MANIFEST = FACTS_DIR / "manifest.json"
DOSSIER_TEMPLATE = str(DOSSIER_DIR / "{slug}.md")
DOSSIERS_MANIFEST = DOSSIER_DIR / "manifest.json"
# merge_candidates (cluster_merge_candidates) writes MERGE_CSV/MERGE_MD/MERGE_MANIFEST for human
# review only — no stage reads them back; accepted merges live in CANONICAL_DB via the review flow.
MERGE_MANIFEST = DOSSIER_DIR / "merge_manifest.json"
PARENT_TEMPLATE = str(PARENTS_DIR / "{slug}.md")
PARENTS_MANIFEST = PARENTS_DIR / "manifest.json"
# PARENT_TEMPLATE/PARENTS_MANIFEST: written by merge_candidates (build_parents) and read back
# within that same subpackage (rendering, its own manifest) — later stages go through CANONICAL_DB.

RECONCILE_DIR = ROOT / "reconcile"
# reconcile_linkedin also writes RECONCILE_DIR/"manifest.json" directly (not exported here) —
# distinct from ENRICH_MANIFEST one level down in deep-research/; don't confuse the two.
# written by enrich (reconcile_deep_research, prefetch_profiles, assemble_synthetic_profile)
DEEP_RESEARCH_DIR = RECONCILE_DIR / "deep-research"
ENRICH_MANIFEST = DEEP_RESEARCH_DIR / "manifest.json"
VERDICTS_JSONL = RECONCILE_DIR / "verdicts.jsonl"  # full per-candidate judge record
# written by enrich.reconcile_linkedin; read by enrich.identity_reconcile as the paid judge-verdict
# cache (a re-fetch here re-bills), and once by migration for pre-SQLite installs.
REVIEW_DIR = ROOT / "review"  # staged human review UI state + cached avatars
REVIEW_MANIFEST = REVIEW_DIR / "manifest.json"  # display-only review receipt
# review.cli/heal_review/sqlite_adapter write and echo this manifest for the FE; the actual
# review decisions live in CANONICAL_DB, not here — losing this file loses no state.

DEFAULT_PEOPLE_CSV = DEFAULT_BASE_DIR / "merged" / "people.csv"
# Written by imports.merge_people (the network-import fan-in stage, outside deep_context).
# Read here by ensure_parents (bootstraps canonical parents) and check_readiness (counts).
PROFILE_CACHE_DIR = DEFAULT_PROFILE_CACHE_DIR
PROFILE_CACHE_TEMPLATE = str(PROFILE_CACHE_DIR / "{public_identifier}.json")
# The shared paid LinkedIn-profile cache, keyed by public_identifier and written by imports'
# profile-fetch primitives outside deep_context. Read here by build_owner, reconcile_linkedin,
# prefetch_profiles, and review healing — a hit here means no RapidAPI spend.
OVERRIDES_DIR = DEFAULT_BASE_DIR / "overrides"
LINKEDIN_OVERRIDES_CSV = OVERRIDES_DIR / "review.csv"
# Legacy pre-SQLite review-decisions file. Nothing in this repo writes it anymore:
# check_readiness only checks for its presence (legacy_artifacts_present) and migration
# reads it once to import old decisions into CANONICAL_DB.
RETARGET_PEOPLE_CSV = OVERRIDES_DIR / "retarget-people.csv"  # written and read only by realize.apply_retargets
OWNER_JSON = ROOT / "owner.json"  # your bio timeline, injected as a reasoning anchor
# build_owner writes this file, but most consumers never read it back: build_owner also
# projects it into CANONICAL_DB's owner_context table, and every downstream reader (synthesis,
# enrich judges) goes through queries.owner_profile()/owner_background() instead. The file's
# only direct reader is synthesize_person_context's optional input-artifact declaration.


def load_env() -> None:
    """Load the nearest .env so OPENAI_API_KEY etc. land in os.environ.

    Walks up from the cwd and this file's tree; first .env found wins. No-op if
    none exists (the key may already be exported)."""
    for base in (Path.cwd(), *Path.cwd().parents, _REPO_ROOT):
        env_path = base / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
            return


# --- Identity normalization -------------------------------------------------


def phone_digits(raw: str) -> str:
    """Comparable digit key, dropping a US country code so +1NXX == NXX."""
    digits = re.sub(r"[^\d]", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


_IDENT_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")

_TOLL_FREE_PREFIXES = ("800", "833", "844", "855", "866", "877", "888")


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if len(t) >= 3}


def _is_toll_free(value: str) -> bool:
    """NANP toll-free: a 10-digit key (leading 1/+1 already stripped) in 8XX."""
    digits = phone_digits(value)
    return len(digits) == 10 and digits.startswith(_TOLL_FREE_PREFIXES)


def _phone_country(value: str) -> str:
    """Coarse country-code comparison key from an E.164 phone."""
    e164 = normalize_phone(value)
    if not e164:
        return ""
    if len(e164) == 12 and e164[1] in "17":
        return e164[1]
    return e164[1:3]


def contact_identifiers(
    values: list[str] | None,
    *,
    name: str = "",
    known: list[str] | tuple[str, ...] = (),
    owner_emails: list[str] | tuple[str, ...] = (),
    owner_phones: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """Keep contact-owned emails and at most two plausible personal phones.

    Not the same job as contact_fields.identifier_emails/identifier_phones (those
    extract merge-judge blocking keys); this ranks and caps values for display.
    """
    owner_e = {str(e or "").strip().lower() for e in owner_emails} - {""}
    owner_p = {phone_digits(str(p)) for p in owner_phones} - {""}
    known_l = {str(v or "").strip().lower() for v in known} - {""}
    known_p = {phone_digits(str(v)) for v in known if "@" not in str(v) and len(phone_digits(str(v))) >= 7}
    tokens = _name_tokens(name)
    out: list[str] = []
    phones: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        value = str(raw or "").strip().strip(".,;:")
        low = value.lower()
        if not value or low in seen:
            continue
        # Accept slash only in phone-like values; reject date-shaped input.
        if "/" in value:
            if (
                re.fullmatch(r"[+()\d\s./\-]+", value)
                and not re.fullmatch(r"\d{1,4}/\d{1,2}/\d{1,4}", value.strip())
                and len(phone_digits(value)) >= 10
            ):
                normalized = normalize_phone(value)
                digits = phone_digits(normalized)
                if normalized and digits not in owner_p and digits not in seen:
                    seen.add(digits)
                    phones.append(normalized)
            continue
        if _IDENT_EMAIL_RE.match(value):
            if low in owner_e:
                continue
            local, _, domain = low.partition("@")
            hay = local + " " + domain.rsplit(".", 1)[0]
            if low in known_l or any(t in hay for t in tokens):
                seen.add(low)
                out.append(value)
            continue
        digits = phone_digits(value)
        if re.fullmatch(r"[+()\d\s.\-]{7,}", value) and len(digits) >= 7:
            if digits in owner_p or digits in seen:
                continue
            seen.add(digits)
            phones.append(value)
    phones = [p for p in phones if not _is_toll_free(p)] or phones
    ranked = [p for p in phones if phone_digits(p) in known_p] + [p for p in phones if phone_digits(p) not in known_p]
    kept: list[str] = []
    for phone in ranked:
        if len(kept) == 2:
            break
        if not kept or phone_digits(phone) in known_p:
            kept.append(phone)
            continue
        first, candidate = _phone_country(kept[0]), _phone_country(phone)
        if first and candidate and first != candidate:
            kept.append(phone)
    return out + kept


def slugify(name: str, person_id: str) -> str:
    """Stable dossier filename stem: name-slug + short id suffix (collision-proof)."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "person"
    pid = (person_id or "").lower()
    if pid.startswith("candidate:"):
        suffix = hashlib.sha1(pid.encode("utf-8")).hexdigest()[:8]
    elif pid.startswith("parent-"):
        suffix = re.sub(r"[^a-z0-9]+", "", pid.removeprefix("parent-"))[:8]
    else:
        suffix = re.sub(r"[^a-z0-9]+", "", pid)[:8] or "unknown"
    return f"{base}-{suffix}"


# --- Message-reader person shape -------------------------------------------


@dataclass
class Person:
    # Opaque lookup key, not one id type: collection.planning.source_parents fills this
    # with a canonical parent_id (message-store reads run per merged identity); logbook
    # fills it with a raw people.csv row id — logbook has no parent/child merge concept.
    person_id: str
    full_name: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    source_channels: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return slugify(self.full_name, self.person_id)


def _span(entry: OwnerEducation | OwnerWork) -> str:
    start, end = entry.start, entry.end
    return (
        f"{start}-{end}"
        if start and end
        else f"until {end}"
        if end
        else f"{start}-present"
        if start
        else "dates unknown"
    )


def owner_background_block(owner: OwnerProfile) -> str:
    """Render the owner's bio into a compact prompt block for overlap inference.

    Called from selection.build_system_prompt (synthesis) and
    dossier_evidence.owner_background (every enrich identity judge) — the one
    place owner facts enter a prompt.
    """
    lines = [f"MAILBOX OWNER BACKGROUND (me): {owner.name}".strip()]
    for education in owner.education:
        note = f" ({education.note})" if education.note else ""
        lines.append(f"- School: {education.school} [{_span(education)}]{note}")
    for job in owner.work:
        title = f" as {job.title}" if job.title else ""
        lines.append(f"- Work: {job.company}{title} [{_span(job)}]")
    if owner.locations:
        lines.append(f"- Locations over time: {', '.join(owner.locations)}")
    if owner.notes:
        lines.append(f"- Notes: {owner.notes}")
    return "\n".join(lines)


def emit(payload: dict[str, Any]) -> None:
    """Print a primitive's manifest as a single JSON line on stdout.

    Single-line/compact; primitives.common.jsonio.emit instead pretty-prints
    with sorted keys — the two are not interchangeable output formats.
    """
    print(json.dumps(payload, ensure_ascii=False))
