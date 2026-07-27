"""Shared helpers for the deep-context dossier pipeline.

Paths, the merged-people reader, identity normalization (phone/email/name), the
dossier slug scheme, the privacy gate, and small manifest/JSONL utilities. Kept
dependency-light (stdlib + repo schema helpers) so every stage imports the same
identity logic and nothing drifts.

Changelog:
  2026-07-24: added the index.json document contract (load_index / write_index /
    derive_lookup_maps) so compose_dossier and build_parents each own exactly one
    key and the three by_* lookup maps are a pure projection of both. write_json is
    re-imported for that internal use.
  2026-07-23 (audit dedup): now_iso / write_json / plain normalize_email deleted
    here and moved to the canonical common.jsonio / common.contact_fields homes.
    normalize_email is re-imported (used internally by _collect_emails); now_iso
    is re-imported purely so the off-limits review_web modules can keep importing
    it from here. The compact `emit` (single-line JSON) is intentionally NOT
    folded — it differs from jsonio's pretty emit.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.schemas.people_schema import parse_jsonish  # noqa: E402
from packs.ingestion.primitives.common.contact_fields import normalize_email  # noqa: E402
# now_iso is re-exported here so review_web/ (off-limits) keeps importing it from
# deep_context.common; the canonical home is common.jsonio.
from packs.ingestion.primitives.common.jsonio import now_iso, write_json  # noqa: E402,F401

# --- Fixed output layout (one dir, overwrite in place; no ledgers, no run ids) ---
ROOT = Path(".powerpacks/deep-context")
RAW_DIR = ROOT / "raw"            # ephemeral per-person sampled message bundles
FACTS_DIR = ROOT / "facts"        # per-person extracted-fact JSONL (checkpoint)
DOSSIER_DIR = ROOT / "dossiers"   # final markdown dossiers
EMBED_DIR = ROOT / "embeddings"   # dossier-summary vectors (merge clustering)
INDEX_JSON = ROOT / "index.json"  # lookup map: phone/email/name -> slug
INDEX_MD = ROOT / "index.md"      # human catalog
MERGE_CSV = ROOT / "merge-candidates.csv"
MERGE_MD = ROOT / "merge-candidates.md"
MERGE_VERDICTS_CSV = ROOT / "merge-verdicts.csv"  # full judge log incl. rejections
PARENTS_DIR = ROOT / "parents"    # merged canonical-person dossiers (link to children)

# Declared-contract path templates (`pipeline/contract.py`). Per-person files are
# unenumerable at declaration time, so the graph names them as `{person_id}` /
# `{slug}` templates — same mechanism as gmail discovery's `{account_slug}`.
# Producer and consumer must use the SAME constant: graph edges are string
# equality on the declared path.
RAW_BUNDLE_TEMPLATE = str(RAW_DIR / "{person_id}.json")
RAW_MANIFEST = RAW_DIR / "manifest.json"
FACTS_TEMPLATE = str(FACTS_DIR / "{person_id}.jsonl")
FACTS_MANIFEST = FACTS_DIR / "manifest.json"
DOSSIER_TEMPLATE = str(DOSSIER_DIR / "{slug}.md")
DOSSIERS_MANIFEST = DOSSIER_DIR / "manifest.json"
MERGE_MANIFEST = DOSSIER_DIR / "merge_manifest.json"
PARENT_TEMPLATE = str(PARENTS_DIR / "{slug}.md")
PARENTS_MANIFEST = PARENTS_DIR / "manifest.json"

# Phase 3 — reconcile parents against their attached LinkedIn profile ("self-heal").
RECONCILE_DIR = ROOT / "reconcile"
DEEP_RESEARCH_DIR = RECONCILE_DIR / "deep-research"
ENRICH_MANIFEST = DEEP_RESEARCH_DIR / "manifest.json"
VERDICTS_JSONL = RECONCILE_DIR / "verdicts.jsonl"   # full per-candidate judge record
VERDICTS_CSV = RECONCILE_DIR / "verdicts.csv"       # flat review table
SUMMARY_MD = RECONCILE_DIR / "summary.md"           # the ONE report to read (what changed + review)
REVIEW_DIR = ROOT / "review"                         # staged human review UI state + cached avatars
REVIEW_MANIFEST = REVIEW_DIR / "manifest.json"      # fixed completion signal for the agent

DEFAULT_PEOPLE_CSV = Path(".powerpacks/network-import/merged/people.csv")
# RapidAPI LinkedIn lookup cache (one JSON per public_identifier) — the "linkedin lookups".
PROFILE_CACHE_DIR = Path(".powerpacks/network-import/profile_cache_v2")
# Declared-contract template for the cache (prefetch writes it; reconcile,
# apply-retargets, and the review UI read it).
PROFILE_CACHE_TEMPLATE = str(PROFILE_CACHE_DIR / "{public_identifier}.json")
# Durable Deep Context review decisions. Realized identities are persisted from
# this file to directory.csv before fan-in, so they survive source re-imports.
OVERRIDES_DIR = Path(".powerpacks/network-import/overrides")
LINKEDIN_OVERRIDES_CSV = OVERRIDES_DIR / "review.csv"
# Enriched re-attach rows (retargets), persisted to directory.csv at realization.
RETARGET_PEOPLE_CSV = OVERRIDES_DIR / "retarget-people.csv"
# Contact-only rows that fold a parent's children onto its kept LinkedIn, persisted at realization.
CONSOLIDATE_PEOPLE_CSV = OVERRIDES_DIR / "consolidate-people.csv"
OWNER_JSON = ROOT / "owner.json"  # your bio timeline, injected as a reasoning anchor

# Channel labels as they appear in people.csv `source_channels`.
GMAIL_CHANNEL = "gmail_msgvault"
IMESSAGE_CHANNEL = "imessage"
WHATSAPP_CHANNEL = "whatsapp"


def load_env() -> None:
    """Load the nearest .env so OPENAI_API_KEY etc. land in os.environ.

    Walks up from the cwd and this file's tree; first .env found wins. No-op if
    none exists (the key may already be exported)."""
    from dotenv import load_dotenv

    for base in (Path.cwd(), *Path.cwd().parents, _REPO_ROOT):
        env_path = base / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
            return


# --- Identity normalization -------------------------------------------------

def normalize_phone(raw: str) -> str:
    """Best-effort E.164 (mirrors the messages-pack canonicalizer)."""
    value = (raw or "").strip()
    digits = re.sub(r"[^\d]", "", value)
    if len(digits) < 7:
        return ""
    if value.startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) <= 15:
        return f"+{digits}"
    return ""


def phone_digits(raw: str) -> str:
    """Comparable digit key, dropping a US country code so +1NXX == NXX."""
    digits = re.sub(r"[^\d]", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def normalize_name(raw: str) -> str:
    """Lowercased, whitespace-collapsed name key for fuzzy lookup."""
    return re.sub(r"\s+", " ", (raw or "").strip()).lower()


def slugify(name: str, person_id: str) -> str:
    """Stable dossier filename stem: name-slug + short id suffix (collision-proof)."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "person"
    pid = (person_id or "").lower()
    if pid.startswith("candidate:"):
        # Every candidate id shares the "candidate:" prefix, so the first-8
        # alnum suffix below would collapse to "candidat" for all of them and
        # same-named candidates would collide. Hash the whole id instead.
        suffix = hashlib.sha1(pid.encode("utf-8")).hexdigest()[:8]
    elif pid.startswith("parent-"):
        # parent_id already carries a SHA-1 digest. Using the generic first
        # eight alphanumerics produced "parent" + only two digest characters,
        # leaving just 256 suffixes per same-named parent.
        suffix = re.sub(r"[^a-z0-9]+", "", pid.removeprefix("parent-"))[:8]
    else:
        suffix = re.sub(r"[^a-z0-9]+", "", pid)[:8] or "unknown"
    return f"{base}-{suffix}"


def parse_list(value: Any) -> list[str]:
    """Parse a JSON-array-or-bare-string list column into clean string values."""
    parsed = parse_jsonish(value, None)
    if isinstance(parsed, list):
        items = parsed
    else:
        # Bare/non-JSON value (e.g. a single "a@x.com") -> one-item list.
        raw = parsed if parsed not in (None, "") else value
        items = [raw] if str(raw or "").strip() else []
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


# --- The lookup index document (index.json) ---------------------------------
# ONE writer per key, so the two stages that touch this file cannot race:
#
#   slugs    OWNED BY compose_dossier   one entry per child dossier
#   parents  OWNED BY build_parents     one entry per canonical person
#   by_email / by_phone / by_name       DERIVED from slugs + parents, never appended to
#
# A writer loads the whole document, replaces ONLY the key it owns, and re-derives
# the lookup maps from the merged result, so ordering stops mattering: compose after
# parents and parents after compose produce the same document. This is why the maps
# are not stored incrementally — the previous append-then-overwrite arrangement had
# compose reset the document (dropping `parents`) while build_parents appended to
# maps it did not own.
#
# Each `slugs` entry therefore carries its own identity (`emails`, `phones`, `name`,
# `full_name`); a parent's identifiers are the union of its children's, so the whole
# projection reads only this file and survives `purge-raw`.

LOOKUP_MAPS = ("by_email", "by_phone", "by_name")


def derive_lookup_maps(index: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """The three by_* lookup maps as a pure projection of `slugs` + `parents`.

    A child contributes its stored emails/phones plus both of its name keys (the
    canonical name and the raw contact name); a parent contributes the UNION of its
    children's identifiers under its own name. Slugs come before parents and every
    list is append-once, so the same document always yields the same maps.
    """
    slugs = index.get("slugs") or {}
    maps: dict[str, dict[str, list[str]]] = {name: {} for name in LOOKUP_MAPS}

    def add(target: dict[str, list[str]], key: str, slug: str) -> None:
        if key and slug not in target.setdefault(key, []):
            target[key].append(slug)

    def add_record(slug: str, emails: list[str], phones: list[str], name_keys: list[str]) -> None:
        for email in emails:
            add(maps["by_email"], str(email or "").strip().lower(), slug)
        for phone in phones:
            add(maps["by_phone"], phone_digits(str(phone or "")), slug)
        for name_key in name_keys:
            add(maps["by_name"], name_key, slug)

    for slug, record in slugs.items():
        add_record(slug, record.get("emails") or [], record.get("phones") or [],
                   sorted({normalize_name(record.get("name") or ""),
                           normalize_name(record.get("full_name") or "")}))
    for parent_slug, parent in (index.get("parents") or {}).items():
        emails: list[str] = []
        phones: list[str] = []
        for child in parent.get("children") or []:
            child_record = slugs.get(child) or {}
            emails += [e for e in (child_record.get("emails") or []) if e not in emails]
            phones += [p for p in (child_record.get("phones") or []) if p not in phones]
        add_record(parent_slug, emails, phones, [normalize_name(parent.get("name") or "")])
    return maps


def parent_identifiers(index: dict[str, Any], child_slugs: list[str]) -> tuple[list[str], list[str]]:
    """(emails, phones) a parent inherits from its children — the same union
    `derive_lookup_maps` projects, so a parent dossier and the lookup maps can never
    disagree about which addresses belong to that person."""
    slugs = index.get("slugs") or {}
    emails: list[str] = []
    phones: list[str] = []
    for child in child_slugs:
        record = slugs.get(child) or {}
        emails += [e for e in (record.get("emails") or []) if e not in emails]
        phones += [p for p in (record.get("phones") or []) if p not in phones]
    return emails, phones


def load_index(path: Path) -> dict[str, Any]:
    """The whole index document ({} when absent or unreadable)."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_index(path: Path, index: dict[str, Any]) -> None:
    """Write the index with its lookup maps re-derived from the two record maps."""
    document = {
        "slugs": index.get("slugs") or {},
        "parents": index.get("parents") or {},
        **{key: value for key, value in index.items()
           if key not in {"slugs", "parents", *LOOKUP_MAPS}},
        **derive_lookup_maps(index),
    }
    write_json(path, document)


# --- Person model + reader --------------------------------------------------

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

    def has_channel(self, channel: str) -> bool:
        return channel in self.source_channels


def _collect_emails(row: dict[str, str]) -> list[str]:
    emails: list[str] = []
    for value in [row.get("primary_email", ""), *parse_list(row.get("all_emails"))]:
        norm = normalize_email(value)
        if norm and "@" in norm and norm not in emails:
            emails.append(norm)
    return emails


def _collect_phones(row: dict[str, str]) -> list[str]:
    phones: list[str] = []
    for value in [row.get("primary_phone", ""), *parse_list(row.get("all_phones"))]:
        norm = normalize_phone(value)
        if norm and norm not in phones:
            phones.append(norm)
    return phones


def load_people(
    people_csv: Path,
    *,
    limit: int = 0,
    person_id: str = "",
    require_channels: bool = True,
) -> Iterator[Person]:
    """Yield ``Person`` rows from the merged people.csv.

    ``require_channels`` keeps only people whose ``source_channels`` include at
    least one of the three message sources (Gmail / iMessage / WhatsApp) — the
    only people who could have message context. Zero-interaction contacts are
    skipped naturally downstream when no messages are found.
    """
    message_channels = {GMAIL_CHANNEL, IMESSAGE_CHANNEL, WHATSAPP_CHANNEL}
    yielded = 0
    with people_csv.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pid = str(row.get("id") or "").strip()
            if not pid:
                continue
            if person_id and pid != person_id:
                continue
            channels = [c.strip() for c in str(row.get("source_channels") or "").split(",") if c.strip()]
            if require_channels and not (set(channels) & message_channels):
                continue
            person = Person(
                person_id=pid,
                full_name=str(row.get("full_name") or "").strip(),
                emails=_collect_emails(row),
                phones=_collect_phones(row),
                source_channels=channels,
            )
            if require_channels and not person.emails and not person.phones:
                continue
            yield person
            yielded += 1
            if limit and yielded >= limit:
                return


# --- Small IO utilities -----------------------------------------------------

def load_owner(path: Path = OWNER_JSON) -> dict[str, Any] | None:
    """Load the mailbox owner's bio timeline (owner.json) if present."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _span(entry: dict[str, Any]) -> str:
    start, end = entry.get("start"), entry.get("end")
    if start and end:
        return f"{start}-{end}"
    if end:
        return f"until {end}"
    if start:
        return f"{start}-present"
    return "dates unknown"


def owner_background_block(owner: dict[str, Any]) -> str:
    """Render the owner's bio into a compact prompt block for overlap inference."""
    lines = [f"MAILBOX OWNER BACKGROUND (me): {owner.get('name', '')}".strip()]
    for ed in owner.get("education") or []:
        note = f" ({ed['note']})" if ed.get("note") else ""
        lines.append(f"- School: {ed.get('school', '')} [{_span(ed)}]{note}")
    for job in owner.get("work") or []:
        title = f" as {job['title']}" if job.get("title") else ""
        lines.append(f"- Work: {job.get('company', '')}{title} [{_span(job)}]")
    if owner.get("locations"):
        lines.append(f"- Locations over time: {', '.join(owner['locations'])}")
    if owner.get("notes"):
        lines.append(f"- Notes: {owner['notes']}")
    return "\n".join(lines)


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def emit(payload: dict[str, Any]) -> None:
    """Print a primitive's manifest as a single JSON line on stdout."""
    print(json.dumps(payload, ensure_ascii=False))
