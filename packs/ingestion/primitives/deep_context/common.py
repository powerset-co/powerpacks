"""Shared helpers for the deep-context dossier pipeline.

Paths, the merged-people reader, identity normalization (phone/email/name), the
dossier slug scheme, the privacy gate, and small manifest/JSONL utilities. Kept
dependency-light (stdlib + repo schema helpers) so every stage imports the same
identity logic and nothing drifts.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.schemas.people_schema import parse_jsonish  # noqa: E402

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
PARENTS_DIR = ROOT / "parents"    # merged canonical-person dossiers (link to children)

# Phase 3 — reconcile parents against their attached LinkedIn profile ("self-heal").
RECONCILE_DIR = ROOT / "reconcile"
VERDICTS_JSONL = RECONCILE_DIR / "verdicts.jsonl"   # full per-candidate judge record
VERDICTS_CSV = RECONCILE_DIR / "verdicts.csv"       # flat review table
REVIEW_QUEUE_CSV = RECONCILE_DIR / "review-queue.csv"  # low-confidence rows needing feedback

DEFAULT_PEOPLE_CSV = Path(".powerpacks/network-import/merged/people.csv")
# RapidAPI LinkedIn lookup cache (one JSON per public_identifier) — the "linkedin lookups".
PROFILE_CACHE_DIR = Path(".powerpacks/network-import/profile_cache_v2")
# Durable self-heal override: reconcile writes it, the fan-in merge re-applies it every run
# (a merge INPUT, not a deep-context output — so it survives re-merges/index rebuilds).
OVERRIDES_DIR = Path(".powerpacks/network-import/overrides")
LINKEDIN_OVERRIDES_CSV = OVERRIDES_DIR / "linkedin-reconcile.csv"
# Enriched re-attach rows (retargets), auto-ingested by the fan-in merge.
RETARGET_PEOPLE_CSV = OVERRIDES_DIR / "retarget-people.csv"
# Contact-only rows that fold a parent's children onto its kept LinkedIn (auto-ingested).
CONSOLIDATE_PEOPLE_CSV = OVERRIDES_DIR / "consolidate-people.csv"
OWNER_JSON = ROOT / "owner.json"  # your bio timeline, injected as a reasoning anchor

# Channel labels as they appear in people.csv `source_channels`.
GMAIL_CHANNEL = "gmail_msgvault"
IMESSAGE_CHANNEL = "imessage"
WHATSAPP_CHANNEL = "whatsapp"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def normalize_name(raw: str) -> str:
    """Lowercased, whitespace-collapsed name key for fuzzy lookup."""
    return re.sub(r"\s+", " ", (raw or "").strip()).lower()


def slugify(name: str, person_id: str) -> str:
    """Stable dossier filename stem: name-slug + short id suffix (collision-proof)."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "person"
    suffix = re.sub(r"[^a-z0-9]+", "", (person_id or "").lower())[:8] or "unknown"
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


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
