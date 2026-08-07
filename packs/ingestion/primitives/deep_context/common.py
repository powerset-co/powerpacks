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
from packs.ingestion.primitives.discover.messages.wacli.util import (
    canonicalize_phone as normalize_phone,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
normalize_name = normalize_name_key

# --- Fixed output layout (one dir, overwrite in place; no ledgers, no run ids) ---
ROOT = Path(".powerpacks/deep-context")
CANONICAL_DB = ROOT / "deep-context.sqlite"
RAW_DIR = ROOT / "raw"            # one sampled message bundle per parent
FACTS_DIR = ROOT / "facts"        # one extracted-fact JSONL per parent
DOSSIER_DIR = ROOT / "dossiers"   # final markdown dossiers
INDEX_MD = ROOT / "index.md"      # human catalog
MERGE_CSV = ROOT / "merge-candidates.csv"
MERGE_MD = ROOT / "merge-candidates.md"
PARENTS_DIR = ROOT / "parents"    # merged canonical-person dossiers (link to children)

# Per-parent artifacts use one fixed path template per stage.
RAW_BUNDLE_TEMPLATE = str(RAW_DIR / "{parent_id}.json")
RAW_MANIFEST = RAW_DIR / "manifest.json"
FACTS_TEMPLATE = str(FACTS_DIR / "{parent_id}.jsonl")
FACTS_MANIFEST = FACTS_DIR / "manifest.json"
DOSSIER_TEMPLATE = str(DOSSIER_DIR / "{slug}.md")
DOSSIERS_MANIFEST = DOSSIER_DIR / "manifest.json"
MERGE_MANIFEST = DOSSIER_DIR / "merge_manifest.json"
PARENT_TEMPLATE = str(PARENTS_DIR / "{slug}.md")
PARENTS_MANIFEST = PARENTS_DIR / "manifest.json"

RECONCILE_DIR = ROOT / "reconcile"
DEEP_RESEARCH_DIR = RECONCILE_DIR / "deep-research"
ENRICH_MANIFEST = DEEP_RESEARCH_DIR / "manifest.json"
VERDICTS_JSONL = RECONCILE_DIR / "verdicts.jsonl"   # full per-candidate judge record
REVIEW_DIR = ROOT / "review"                         # staged human review UI state + cached avatars
REVIEW_MANIFEST = REVIEW_DIR / "manifest.json"      # display-only review receipt

DEFAULT_PEOPLE_CSV = DEFAULT_BASE_DIR / "merged" / "people.csv"
PROFILE_CACHE_DIR = DEFAULT_PROFILE_CACHE_DIR
PROFILE_CACHE_TEMPLATE = str(PROFILE_CACHE_DIR / "{public_identifier}.json")
OVERRIDES_DIR = DEFAULT_BASE_DIR / "overrides"
LINKEDIN_OVERRIDES_CSV = OVERRIDES_DIR / "review.csv"
RETARGET_PEOPLE_CSV = OVERRIDES_DIR / "retarget-people.csv"
OWNER_JSON = ROOT / "owner.json"  # your bio timeline, injected as a reasoning anchor

# Channel labels as they appear in people.csv `source_channels`.
GMAIL_CHANNEL = "gmail_msgvault"
IMESSAGE_CHANNEL = "imessage"
WHATSAPP_CHANNEL = "whatsapp"

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


def contact_identifiers(values: list[str] | None, *, name: str = "",
                        known: list[str] | tuple[str, ...] = (),
                        owner_emails: list[str] | tuple[str, ...] = (),
                        owner_phones: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Keep contact-owned emails and at most two plausible personal phones."""
    owner_e = {str(e or "").strip().lower() for e in owner_emails} - {""}
    owner_p = {phone_digits(str(p)) for p in owner_phones} - {""}
    known_l = {str(v or "").strip().lower() for v in known} - {""}
    known_p = {phone_digits(str(v)) for v in known
               if "@" not in str(v) and len(phone_digits(str(v))) >= 7}
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
            if (re.fullmatch(r"[+()\d\s./\-]+", value)
                    and not re.fullmatch(r"\d{1,4}/\d{1,2}/\d{1,4}", value.strip())
                    and len(phone_digits(value)) >= 10):
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
    ranked = ([p for p in phones if phone_digits(p) in known_p]
              + [p for p in phones if phone_digits(p) not in known_p])
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
        f"{start}-{end}" if start and end else f"until {end}" if end
        else f"{start}-present" if start else "dates unknown"
    )


def owner_background_block(owner: OwnerProfile) -> str:
    """Render the owner's bio into a compact prompt block for overlap inference."""
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
    """Print a primitive's manifest as a single JSON line on stdout."""
    print(json.dumps(payload, ensure_ascii=False))
