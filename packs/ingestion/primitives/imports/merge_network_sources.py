#!/usr/bin/env python3
"""Merge/dedupe local network-import sources into one people schema CSV.

Dedupe rule:
1. Merge rows with the same LinkedIn public identifier / URL.
2. Keep non-LinkedIn rows separate, but emit similar-name review pairs in
   `possible_duplicates_review.csv` (similar names without shared LinkedIn are
   never auto-merged).

Stdlib-only. Local artifacts only. No uploads or external API calls. Accepts
only explicit `--input` paths — it never scans `.powerpacks` for run
artifacts. Product fan-in should pass reviewed, stable per-source artifacts
such as `import/gmail/people.csv` and `import/messages/people.csv`; raw
`messages/contacts.csv` must pass through `$import-messages` review and
materialization first.

Usage:
    merge_network_sources.py run \
        --input .powerpacks/network-import/import/gmail/people.csv \
        --input .powerpacks/network-import/import/messages/people.csv

Outputs under `.powerpacks/network-import/merged/`: canonical `people.csv`,
`people_harmonic_all.merged.csv` (temporary compatibility alias),
`network_contacts.csv`, `possible_duplicates_review.csv`, and
`merge_manifest.json`.

Changelog:
  2026-07-23 (audit):
    - merge_network_sources.README.md sidecar folded into this docstring.
    - Moved from primitives/merge_network_sources/ into
      imports/; the duplicated try/except import block became
      the single repo-root bootstrap stanza.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repo-root bootstrap so packs.* imports work in module AND script mode
# (uv run .../merge_network_sources.py); must be in-file because script mode
# never imports the package __init__.
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.schemas.people_schema import (  # noqa: E402
    LIST_VALUE_COLUMNS,
    PEOPLE_SCHEMA_COLUMNS,
    latest_interaction,
    merge_interaction_counts,
    normalize_people_row,
    stable_linkedin_key,
    extract_public_identifier,
)
from packs.ingestion.schemas.linkedin_profile_normalizer import normalize_linkedin_profile  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(".powerpacks/network-import/merged")
# Durable self-heal override written by $deep-context reconcile, re-applied every merge.
DEFAULT_OVERRIDES = Path(".powerpacks/network-import/overrides/review.csv")
DEFAULT_RETARGET_PEOPLE = Path(".powerpacks/network-import/overrides/retarget-people.csv")
DEFAULT_CONSOLIDATE_PEOPLE = Path(".powerpacks/network-import/overrides/consolidate-people.csv")
MERGED_COLUMNS = PEOPLE_SCHEMA_COLUMNS + ["merge_key", "merge_confidence", "merge_sources",
    "merged_row_count", "needs_review", "linkedin_verified", "linkedin_verified_confidence",
    "linkedin_verified_reason"]
# Merged channel-wise (max) / by recency instead of first-non-empty choose().
INTERACTION_MERGE_COLUMNS = {"interaction_counts", "last_interaction"}
REVIEW_COLUMNS = ["left_id", "right_id", "left_name", "right_name", "similarity", "left_sources", "right_sources", "reason"]
NETWORK_CONTACT_COLUMNS = [
    "contact_id",
    "merge_key",
    "display_name",
    "linkedin_url",
    "public_identifier",
    "primary_email",
    "primary_phone",
    "source_channels",
    "source_count",
    "needs_review",
]
NETWORK_CONTACT_SOURCE_COLUMNS = [
    "contact_id",
    "merge_key",
    "source_channel",
    "source_identifier",
    "source_artifact",
    "display_name",
    "linkedin_url",
    "public_identifier",
    "primary_email",
    "primary_phone",
]
NETWORK_COMPANY_COLUMNS = [
    "company_id",
    "company_key",
    "company_name",
    "company_urn",
    "source_channels",
    "contact_count",
    "contact_ids",
    "contact_names",
]
MAX_SOURCE_ARTIFACTS_PER_ROW = 12
MAX_SOURCE_ARTIFACT_TEXT = 4096


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return list(CsvIO.dict_reader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in fieldnames})


def sha(value: str, n: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:n]


def normalize_name(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    parts = [p for p in value.split() if p not in {"jr", "sr", "ii", "iii", "phd", "mba", "md"}]
    return " ".join(parts)


def row_name(row: dict[str, str]) -> str:
    return row.get("full_name") or " ".join(x for x in [row.get("first_name", ""), row.get("last_name", "")] if x).strip() or row.get("name", "") or row.get("display_name", "")


def listish_values(value: str) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        parsed = None
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item).strip()]
    return [part.strip() for part in re.split(r"[,;]", value) if part.strip()]


def source_artifact_values(value: Any, *, _depth: int = 0) -> list[str]:
    """Flatten source_artifacts provenance without preserving nested JSON blobs.

    Earlier merges could include an already-merged row whose source_artifacts was
    itself a JSON array. Treating that whole JSON array as one artifact caused
    exponential growth on repeated fan-ins. Keep only a bounded, flat list of
    readable artifact references; source_artifacts is debug provenance, not a
    searchable/indexing payload.
    """
    if value is None or _depth > 6:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(source_artifact_values(item, _depth=_depth + 1))
        return out
    text = str(value).strip()
    if not text:
        return []
    if len(text) > MAX_SOURCE_ARTIFACT_TEXT and not text.startswith("["):
        return [text[:MAX_SOURCE_ARTIFACT_TEXT] + "…"]
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except Exception:
            # Malformed huge provenance is not useful enough to retain.
            return [] if len(text) > MAX_SOURCE_ARTIFACT_TEXT else [text]
        return source_artifact_values(parsed, _depth=_depth + 1)
    return [text]


def compact_source_artifacts(values: list[Any], *, limit: int = MAX_SOURCE_ARTIFACTS_PER_ROW) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        for artifact in source_artifact_values(value):
            artifact = artifact.strip()
            if not artifact or artifact in seen:
                continue
            seen.add(artifact)
            out.append(artifact)
    out.sort()
    if len(out) > limit:
        remaining = len(out) - limit
        out = out[:limit] + [f"... {remaining} more source artifact(s) omitted"]
    return json.dumps(out, ensure_ascii=False)


def usable_rapidapi_payload(value: str) -> bool:
    if not value:
        return False
    try:
        payload = json.loads(value)
    except Exception:
        return False
    if not isinstance(payload, dict) or not payload:
        return False
    return normalize_linkedin_profile(payload).get("success") is True


def has_rapidapi_profile(row: dict[str, Any]) -> bool:
    return usable_rapidapi_payload(str(row.get("rapidapi_response") or ""))


def keep_people_csv_row(row: dict[str, Any]) -> bool:
    # Synthetic rows (deep-researched people with NO real LinkedIn) have neither a
    # LinkedIn key nor a rapidapi payload by design. Their approved gate
    # (auto = high research completeness, yes = user approved) is enforced at
    # LOAD time in load_people_file — normalization strips the non-schema
    # `approved` column, so a synthetic row reaching this gate either passed the
    # load filter or was already admitted into a prior merge. Real rows keep the
    # strict LinkedIn+rapidapi requirement unchanged.
    if (row.get("enrichment_provider") or "").strip().lower() == "synthetic":
        return bool((row.get("public_identifier") or "").strip())
    return bool(stable_linkedin_key(row)) and has_rapidapi_profile(row)


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def normalize_phone(value: str) -> str:
    phone = (value or "").strip()
    if not phone:
        return ""
    digits = re.sub(r"\D+", "", phone)
    return f"+{digits}" if phone.startswith("+") and digits else digits


# --- self-heal override (from $deep-context reconcile) ----------------------

def _phone10(value: str) -> str:
    """Loose phone key (last 10 digits) so +1 / country-code variants still match."""
    digits = re.sub(r"\D+", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _row_emails(row: dict[str, Any]) -> set[str]:
    return {normalize_email(e) for e in [row.get("primary_email", ""), *listish_values(row.get("all_emails", ""))]
            if normalize_email(e)}


def _row_phones(row: dict[str, Any]) -> set[str]:
    return {_phone10(p) for p in [row.get("primary_phone", ""), *listish_values(row.get("all_phones", ""))]
            if _phone10(p)}


def row_public_identifier(row: dict[str, Any]) -> str:
    pub = (row.get("public_identifier") or "").strip().lower()
    return pub or extract_public_identifier(row.get("linkedin_url") or "").lower()


def load_overrides(path: Path | None) -> dict[str, dict[str, Any]]:
    """public_identifier -> {action, emails, phones, confidence, reason} from the override CSV."""
    overrides: dict[str, dict[str, Any]] = {}
    if not path or not Path(path).exists():
        return overrides
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pub = (row.get("public_identifier") or "").strip().lower()
            if not pub:
                continue
            overrides[pub] = {
                "action": (row.get("action") or "").strip().lower(),
                "approved": (row.get("approved") or "").strip().lower(),
                "emails": {normalize_email(e) for e in (row.get("match_emails") or "").split("|") if normalize_email(e)},
                "phones": {_phone10(p) for p in (row.get("match_phones") or "").split("|") if _phone10(p)},
                "confidence": row.get("confidence", ""),
                "reason": row.get("reason", ""),
                # Machine-owned columns (absent in older files -> blank):
                # llm_reject* describes profile proposals; llm_worth is mirrored
                # from message synthesis.
                "person_id": (row.get("person_id") or "").strip(),
                "llm_reject": (row.get("llm_reject") or "").strip().lower(),
                "llm_reject_confidence": row.get("llm_reject_confidence", ""),
                "llm_worth": (row.get("llm_worth") or "").strip().lower(),
                # user-owned worth mark (yes|maybe|no) — the unified 'effective no' input.
                "network_worth": (row.get("network_worth") or "").strip().lower(),
            }
    return overrides


# Decisions apply only when high-confidence (auto) or the user approved (yes).
APPLIED_APPROVALS = {"auto", "yes"}

# --- unified "effective no" (worth drop) ------------------------------------
# ONE concept: a user no (network_worth=no mark, or an approved `exclude` action) or a
# machine no (synthesis's mirrored `llm_worth=no`) drops the person from the
# searchable network. A user rescue
# (network_worth=yes, or a keep-ish approved=yes decision) protects from any MACHINE
# no; the user's own no is unconditional. Nothing destructive: flip the mark back and
# the person returns at the next merge.

# Actions that are NOT keep-ish: an approved detach/exclude never rescues a person.
_NON_KEEP_ACTIONS = {"detach", "exclude"}


def _user_no(ov: dict[str, Any]) -> bool:
    """The user's own no: an explicit network_worth mark wins either way; with no mark,
    an approved `exclude` action counts as the same user no."""
    mark = ov.get("network_worth") or ""
    if mark in ("yes", "maybe", "no"):
        return mark == "no"
    return ov.get("action") == "exclude" and ov.get("approved") in APPLIED_APPROVALS


def _machine_no(ov: dict[str, Any]) -> bool:
    return ov.get("llm_worth") == "no"


def _user_rescued(ov: dict[str, Any]) -> bool:
    return ov.get("network_worth") == "yes" or (
        ov.get("approved") == "yes" and ov.get("action") not in _NON_KEEP_ACTIONS)


def _row_effective_no(ov: dict[str, Any]) -> bool:
    """Unified 'effective no' for ONE override row: the user said no (unconditional),
    or the machine said no and no user rescue protects the row."""
    if _user_no(ov):
        return True
    return _machine_no(ov) and not _user_rescued(ov)


def user_no_person_ids(overrides: dict[str, dict[str, Any]]) -> set[str]:
    """person_ids the USER explicitly rejected (worth mark or approved exclude)."""
    return {ov.get("person_id") or "" for ov in overrides.values()
            if ov.get("person_id") and _user_no(ov)}


def worth_dropped_person_ids(overrides: dict[str, dict[str, Any]]) -> set[str]:
    """person_ids to drop entirely at merge — the unified 'effective no': a user
    network_worth=no or approved exclude (unconditional), or a machine no
    (`llm_worth=no`) unless the user rescued the person (network_worth=yes, or a
    keep-ish approved=yes decision)."""
    user_no: set[str] = set()
    flagged: set[str] = set()
    rescued: set[str] = set()
    for ov in overrides.values():
        pid = ov.get("person_id") or ""
        if not pid:
            continue
        if _user_no(ov):
            user_no.add(pid)
        if _machine_no(ov):
            flagged.add(pid)
        if _user_rescued(ov):
            rescued.add(pid)
    return user_no | (flagged - rescued)


def spam_dropped_person_ids(overrides: dict[str, dict[str, Any]]) -> set[str]:
    """Compatibility counter: the retired LinkedIn spam screen drops nobody."""
    return set()


def _scope_matches(ov: dict[str, Any], row: dict[str, Any]) -> bool:
    """Scope the override to the right person: an unscoped override matches by
    public_identifier alone; a scoped one needs an email/phone in common."""
    if not ov["emails"] and not ov["phones"]:
        return True
    return bool(ov["emails"] & _row_emails(row)) or bool(ov["phones"] & _row_phones(row))


def apply_overrides(rows: list[dict[str, Any]], overrides: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Re-apply reconcile's self-heal during the fan-in. The unified worth drop
    ('effective no' — user network_worth=no / approved `exclude`, or a machine no via
    synthesis-owned `llm_worth` with no user rescue) removes the person entirely,
    regardless of the per-row decision state. Then, ONLY for approved decisions
    (auto = high-confidence, or a user `yes`): `detach`/`retarget` clear the wrong
    LinkedIn (so the LinkedIn-only people.csv drops that old row — for a retarget the
    correct enriched row arrives via retarget-people.csv); `verify` annotates the
    surviving row. Idempotent."""
    detached = verified = retargeted = excluded = spam_dropped = worth_dropped = 0
    dropped_pids: set[str] = set()
    if not overrides:
        return {"detached": 0, "verified": 0, "retargeted": 0, "excluded": 0,
                "spam_dropped": 0, "worth_dropped": 0, "worth_dropped_person_ids": []}
    worth_pids = worth_dropped_person_ids(overrides)
    user_no_pids = user_no_person_ids(overrides)
    spam_pids = spam_dropped_person_ids(overrides)
    for row in rows:
        # A synthetic row's review.csv row is keyed by its ORIGINAL person id (the
        # row's `id`), not its synth- public_identifier: a user no there blocks the merge.
        if (row.get("enrichment_provider") or "").strip().lower() == "synthetic":
            ov = overrides.get((row.get("id") or "").strip().lower())
            if ov and _user_no(ov):
                row["__excluded__"] = True
                worth_dropped += 1
                dropped_pids.add(ov.get("person_id") or (row.get("id") or "").strip().lower())
                continue
        ov = overrides.get(row_public_identifier(row))
        if not ov or not _scope_matches(ov, row):
            continue
        # Unified worth drop: person-level when the override knows its person_id (one
        # effective no drops every row of that person), else this row's own state.
        pid = ov.get("person_id") or ""
        effective_no = (pid in worth_pids) if pid else _row_effective_no(ov)
        if effective_no:
            # LinkedIn connections are GROUND TRUTH: the user is literally
            # connected, so a MACHINE no (worth judgment) never drops a
            # linkedin_csv person — only the user's own no/exclude can.
            user_said_no = (pid in user_no_pids) if pid else _user_no(ov)
            if not user_said_no and "linkedin_csv" in row_source_channels(row):
                effective_no = False
        if effective_no:
            row["__excluded__"] = True
            worth_dropped += 1
            # sibling counters retained in the manifest for backwards compatibility.
            excluded += ov.get("action") == "exclude" and ov.get("approved") in APPLIED_APPROVALS
            spam_dropped += pid in spam_pids
            dropped_pids.add(pid or row_public_identifier(row))
            continue
        if ov["approved"] not in APPLIED_APPROVALS:
            continue
        if ov["action"] in ("detach", "retarget"):
            row["linkedin_url"] = ""
            row["public_identifier"] = ""
            detached += ov["action"] == "detach"
            retargeted += ov["action"] == "retarget"
        elif ov["action"] == "verify":
            row["linkedin_verified"] = "confirmed"
            row["linkedin_verified_confidence"] = ov["confidence"]
            row["linkedin_verified_reason"] = ov["reason"]
            verified += 1
    return {"detached": detached, "verified": verified, "retargeted": retargeted, "excluded": excluded,
            "spam_dropped": spam_dropped, "worth_dropped": worth_dropped,
            "worth_dropped_person_ids": sorted(dropped_pids)}


def stable_source_key(row: dict[str, str]) -> str:
    for email in [row.get("primary_email", ""), *listish_values(row.get("all_emails", ""))]:
        email = normalize_email(email)
        if email:
            return f"email:{email}"
    for phone in [row.get("primary_phone", ""), *listish_values(row.get("all_phones", ""))]:
        phone = normalize_phone(phone)
        if phone:
            return f"phone:{phone}"
    handle = (row.get("twitter_handle") or "").strip().lower().lstrip("@")
    if handle:
        return f"twitter:{handle}"
    source_id = (row.get("id") or "").strip()
    if source_id and not source_id.startswith("merged:"):
        return f"id:{source_id}"
    name = normalize_name(row_name(row))
    channel = ",".join(row_source_channels(row))
    if name:
        return f"name:{sha(channel + ':' + name, 16)}"
    return f"row:{sha(json.dumps({col: row.get(col, '') for col in PEOPLE_SCHEMA_COLUMNS if col != 'source_artifacts'}, sort_keys=True), 16)}"


def source_label(path: Path) -> str:
    text = str(path)
    if "/linkedin/" in text:
        return "linkedin"
    if "/twitter/" in text:
        return "twitter"
    if "/gmail/" in text:
        return "gmail"
    if "/messages/" in text:
        return "messages"
    return path.parent.name


def message_row_to_people(row: dict[str, str], path: Path) -> dict[str, str]:
    linkedin = row.get("matched_linkedin_url", "")
    full_name = row.get("matched_name") or row.get("name") or ""
    parts = full_name.split(" ", 1)
    people = {
        "id": row.get("matched_person_id") or f"message:{sha((row.get('phone') or '') + full_name)}",
        "linkedin_url": linkedin,
        "public_identifier": extract_public_identifier(linkedin),
        "first_name": parts[0] if parts else "",
        "last_name": parts[1] if len(parts) > 1 else "",
        "full_name": full_name,
        "primary_phone": row.get("phone", ""),
        "all_phones": row.get("phone", ""),
        "source_channels": row.get("source") or "messages",
        "source_artifacts": str(path),
        "summary": f"message_count={row.get('message_count','')}; last_message={row.get('last_message','')}",
        "enrichment_provider": row.get("match_method") or "messages_contact_match",
        # interaction_counts/last_interaction stay empty on this path: raw
        # contacts.csv has no approval state, and counts must only enter the
        # network through user-approved review rows (review_row_to_messages_people).
    }
    return normalize_people_row(people)


def load_people_file(path: Path) -> list[dict[str, str]]:
    rows = read_csv(path)
    label = source_label(path)
    out: list[dict[str, str]] = []
    for row in rows:
        if path.name == "contacts.csv" and label == "messages":
            normalized = message_row_to_people(row, path)
        else:
            # The synthetic approved gate (auto/yes) must be decided HERE, on the
            # raw row: `approved` is not a people-schema column, so
            # normalize_people_row strips it and no later stage can see it.
            if (
                path.name == "synthetic-people.csv"
                and (row.get("approved") or "").strip().lower() not in APPLIED_APPROVALS
            ):
                continue
            normalized = normalize_people_row(row)
            normalized["source_artifacts"] = normalized.get("source_artifacts") or str(path)
            normalized["source_channels"] = normalized.get("source_channels") or label
        out.append(normalized)
    return out


def choose(current: str, incoming: str) -> str:
    if not current and incoming:
        return incoming
    if incoming and len(incoming) > len(current) and current in {"", "[]", "{}"}:
        return incoming
    return current


def row_source_channels(row: dict[str, str]) -> list[str]:
    channels: list[str] = []
    for src in (row.get("source_channels") or "").split(","):
        src = src.strip()
        if src and src not in channels:
            channels.append(src)
    return channels or ["unknown"]


def source_identifier(row: dict[str, str], channel: str = "") -> str:
    linkedin_key = stable_linkedin_key(row)
    if linkedin_key:
        return row.get("linkedin_url") or linkedin_key
    if channel.startswith("gmail") or row.get("primary_email"):
        return row.get("primary_email") or row.get("all_emails", "")
    if channel in {"imessage", "whatsapp", "messages"} or row.get("primary_phone"):
        return row.get("primary_phone") or row.get("all_phones", "")
    if channel == "twitter" or row.get("twitter_handle"):
        return row.get("twitter_handle", "")
    return row.get("id") or row_name(row)


def network_contact_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "contact_id": row.get("id", ""),
        "merge_key": row.get("merge_key", ""),
        "display_name": row_name(row),
        "linkedin_url": row.get("linkedin_url", ""),
        "public_identifier": row.get("public_identifier", ""),
        "primary_email": row.get("primary_email", ""),
        "primary_phone": row.get("primary_phone", ""),
        "source_channels": row.get("source_channels", ""),
        "source_count": len([s for s in (row.get("source_channels") or "").split(",") if s.strip()]),
        "needs_review": row.get("needs_review", "false"),
    }


def normalize_company_key(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return value or "unknown"


def network_company_rows(people_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    companies: dict[str, dict[str, Any]] = {}
    for row in people_rows:
        company_name = (row.get("current_company") or "").strip()
        company_urn = (row.get("current_company_urn") or "").strip()
        if not company_name and not company_urn:
            continue
        key = company_urn or f"name:{normalize_company_key(company_name)}"
        rec = companies.setdefault(key, {
            "company_id": f"company:{sha(key, 16)}",
            "company_key": key,
            "company_name": company_name,
            "company_urn": company_urn,
            "source_channels": set(),
            "contact_ids": [],
            "contact_names": [],
        })
        if company_name and not rec.get("company_name"):
            rec["company_name"] = company_name
        if company_urn and not rec.get("company_urn"):
            rec["company_urn"] = company_urn
        for src in (row.get("source_channels") or "").split(","):
            if src.strip():
                rec["source_channels"].add(src.strip())
        contact_id = row.get("id") or row.get("merge_key") or ""
        if contact_id and contact_id not in rec["contact_ids"]:
            rec["contact_ids"].append(contact_id)
        name = row_name(row)
        if name and name not in rec["contact_names"]:
            rec["contact_names"].append(name)
    out: list[dict[str, Any]] = []
    for rec in companies.values():
        out.append({
            "company_id": rec["company_id"],
            "company_key": rec["company_key"],
            "company_name": rec.get("company_name", ""),
            "company_urn": rec.get("company_urn", ""),
            "source_channels": ",".join(sorted(rec["source_channels"])),
            "contact_count": len(rec["contact_ids"]),
            "contact_ids": json.dumps(rec["contact_ids"], ensure_ascii=False),
            "contact_names": json.dumps(rec["contact_names"], ensure_ascii=False),
        })
    out.sort(key=lambda row: (-int(row["contact_count"]), str(row.get("company_name") or row.get("company_key"))))
    return out


def source_fact_rows(contact: dict[str, Any], source_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in source_rows:
        artifacts = source_artifact_values(row.get("source_artifacts", ""))[:MAX_SOURCE_ARTIFACTS_PER_ROW] or [""]
        for channel in row_source_channels(row):
            identifier = source_identifier(row, channel)
            for artifact in artifacts:
                key = (channel, identifier, artifact)
                if key in seen:
                    continue
                seen.add(key)
                facts.append({
                    "contact_id": contact.get("id", ""),
                    "merge_key": contact.get("merge_key", ""),
                    "source_channel": channel,
                    "source_identifier": identifier,
                    "source_artifact": artifact,
                    "display_name": row_name(row),
                    "linkedin_url": row.get("linkedin_url", ""),
                    "public_identifier": row.get("public_identifier", ""),
                    "primary_email": row.get("primary_email", ""),
                    "primary_phone": row.get("primary_phone", ""),
                })
    return facts


def union_list_column(rows: list[dict[str, str]], col: str) -> str:
    """Set-union a LIST_VALUE_COLUMNS column across all rows in a merge group.

    Preserves first-seen order, normalizes emails/phones via listish_values,
    and emits a JSON string list (the canonical storage shape).
    """
    primary_col = "primary_email" if col == "all_emails" else "primary_phone"
    seen: list[str] = []
    for row in rows:
        for value in [row.get(primary_col, ""), *listish_values(row.get(col, ""))]:
            value = (value or "").strip()
            if col == "all_emails":
                value = normalize_email(value)
            elif col == "all_phones":
                value = normalize_phone(value)
            if value and value not in seen:
                seen.append(value)
    return json.dumps(seen, ensure_ascii=False) if seen else ""


def merge_group(key: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    merged = {col: "" for col in PEOPLE_SCHEMA_COLUMNS}
    sources: set[str] = set()
    artifacts: set[str] = set()
    for row in rows:
        for col in PEOPLE_SCHEMA_COLUMNS:
            if col in LIST_VALUE_COLUMNS or col in INTERACTION_MERGE_COLUMNS:
                continue
            merged[col] = choose(merged.get(col, ""), row.get(col, ""))
        for src in (row.get("source_channels") or "").split(","):
            if src.strip():
                sources.add(src.strip())
        for artifact in source_artifact_values(row.get("source_artifacts")):
            artifacts.add(artifact)
    for col in LIST_VALUE_COLUMNS:
        merged[col] = union_list_column(rows, col)
        # Keep the primary value consistent: if primary_email/primary_phone is
        # empty but aliases exist, promote the first alias.
        primary_col = "primary_email" if col == "all_emails" else "primary_phone"
        if not merged.get(primary_col):
            aliases = listish_values(merged[col])
            if aliases:
                merged[primary_col] = aliases[0]
    counts = merge_interaction_counts(*[row.get("interaction_counts", "") for row in rows])
    merged["interaction_counts"] = json.dumps(counts, ensure_ascii=False) if counts else ""
    merged["last_interaction"] = latest_interaction(*[row.get("last_interaction", "") for row in rows])
    merged["source_channels"] = ",".join(sorted(sources))
    merged["source_artifacts"] = compact_source_artifacts(sorted(artifacts))
    merged["merge_key"] = key
    merged["merge_confidence"] = "1.0" if key.startswith("linkedin:") else "0.0"
    merged["merge_sources"] = merged["source_channels"]
    merged["merged_row_count"] = len(rows)
    merged["needs_review"] = "false"
    if not merged.get("id"):
        merged["id"] = f"merged:{sha(key + row_name(merged))}"
    return merged


def build_groups(rows: list[dict[str, str]]) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = {}
    singletons: list[dict[str, str]] = []
    for row in rows:
        key = stable_linkedin_key(row) or stable_source_key(row)
        if key:
            groups.setdefault(key, []).append(row)
        else:
            singletons.append(row)
    return groups, singletons


def similar_pairs(rows: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    review: list[dict[str, Any]] = []
    named = [(i, normalize_name(row_name(r)), r) for i, r in enumerate(rows) if normalize_name(row_name(r))]
    for i in range(len(named)):
        _, left_name, left = named[i]
        for j in range(i + 1, len(named)):
            _, right_name, right = named[j]
            if left.get("merge_key") == right.get("merge_key"):
                continue
            if not left_name or not right_name:
                continue
            ratio = difflib.SequenceMatcher(None, left_name, right_name).ratio()
            exact_parts = set(left_name.split()) == set(right_name.split()) and len(left_name.split()) >= 2
            if ratio >= threshold or exact_parts:
                left["needs_review"] = "true"
                right["needs_review"] = "true"
                review.append({
                    "left_id": left.get("id", ""),
                    "right_id": right.get("id", ""),
                    "left_name": row_name(left),
                    "right_name": row_name(right),
                    "similarity": round(ratio, 3),
                    "left_sources": left.get("source_channels", ""),
                    "right_sources": right.get("source_channels", ""),
                    "reason": "similar_name_no_shared_linkedin",
                })
    return review


def cmd_run(args: argparse.Namespace) -> int:
    inputs = [Path(p) for p in args.input] if args.input else []
    # Self-heal artifacts live in the overrides/ dir SIBLING to the merged output-dir, so they
    # resolve correctly regardless of cwd (a passed path wins; "" disables; None -> derive).
    overrides_dir = Path(args.output_dir).parent / "overrides"

    def _resolve(value: str | None, name: str) -> Path | None:
        if value is None:
            return overrides_dir / name
        return Path(value) if value else None

    override_path = _resolve(args.overrides, "review.csv")
    # Auto-ingest extra people rows produced by the self-heal: retarget re-attachments,
    # consolidation rows (a parent's children's contacts folded onto its kept LinkedIn),
    # and approved synthetic rows (deep-researched people with no real LinkedIn).
    for extra in (_resolve(args.retarget_people, "retarget-people.csv"),
                  _resolve(args.consolidate_people, "consolidate-people.csv"),
                  _resolve(getattr(args, "synthetic_people", None), "synthetic-people.csv")):
        if extra and extra.exists() and extra not in inputs:
            inputs.append(extra)
    all_rows: list[dict[str, str]] = []
    per_file: dict[str, int] = {}
    for path in inputs:
        if not path.exists():
            continue
        rows = load_people_file(path)
        all_rows.extend(rows)
        per_file[str(path)] = len(rows)
    groups, singletons = build_groups(all_rows)
    merged_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    for key, rows in sorted(groups.items()):
        merged = merge_group(key, rows)
        merged_rows.append(merged)
        source_rows.extend(source_fact_rows(merged, rows))
    for row in singletons:
        normalized = normalize_people_row(row)
        normalized.update({
            "merge_key": stable_source_key(normalized),
            "merge_confidence": "0.0",
            "merge_sources": normalized.get("source_channels", ""),
            "merged_row_count": 1,
            "needs_review": "false",
        })
        if not normalized.get("id"):
            normalized["id"] = f"merged:{sha(normalized['merge_key'])}"
        merged_rows.append(normalized)
        source_rows.extend(source_fact_rows(normalized, [normalized]))
    # Re-apply the durable self-heal override BEFORE the LinkedIn keep-filter: a `detach`
    # clears the wrong link so that person drops out here; a `verify` annotates the row.
    overrides = load_overrides(override_path)
    override_stats = apply_overrides(merged_rows, overrides)
    # `exclude` is a hard drop (user doesn't want this person indexed) — remove before the
    # LinkedIn keep-filter so it's counted as an exclusion, not a missing-LinkedIn filter.
    merged_rows = [row for row in merged_rows if not row.pop("__excluded__", False)]
    unfiltered_merged_rows = len(merged_rows)
    filtered_without_linkedin = sum(1 for row in merged_rows if not stable_linkedin_key(row))
    filtered_without_rapidapi_payload = sum(1 for row in merged_rows if not has_rapidapi_profile(row))
    merged_rows = [row for row in merged_rows if keep_people_csv_row(row)]
    kept_merge_keys = {row.get("merge_key", "") for row in merged_rows}
    source_rows = [row for row in source_rows if row.get("merge_key", "") in kept_merge_keys]
    review = similar_pairs(merged_rows, args.name_threshold)
    output_dir = Path(args.output_dir)
    output = output_dir / "people.csv"
    legacy_output = output_dir / "people_harmonic_all.merged.csv"
    review_path = output_dir / "possible_duplicates_review.csv"
    network_contacts_path = output_dir / "network_contacts.csv"
    network_contact_sources_path = output_dir / "network_contact_sources.csv"
    network_companies_path = output_dir / "network_companies.csv"
    manifest = output_dir / "merge_manifest.json"
    write_csv(output, MERGED_COLUMNS, merged_rows)
    shutil.copyfile(output, legacy_output)
    write_csv(review_path, REVIEW_COLUMNS, review)
    write_csv(network_contacts_path, NETWORK_CONTACT_COLUMNS, [network_contact_row(row) for row in merged_rows])
    write_csv(network_contact_sources_path, NETWORK_CONTACT_SOURCE_COLUMNS, source_rows)
    company_rows = network_company_rows(merged_rows)
    write_csv(network_companies_path, NETWORK_COMPANY_COLUMNS, company_rows)
    manifest_payload = {
        "created_at": now_iso(),
        "inputs": per_file,
        "input_rows": len(all_rows),
        "unfiltered_merged_rows": unfiltered_merged_rows,
        "filtered_without_linkedin": filtered_without_linkedin,
        "filtered_without_rapidapi_payload": filtered_without_rapidapi_payload,
        "filtered_people_csv_rows": unfiltered_merged_rows - len(merged_rows),
        "overrides_detached": override_stats["detached"],
        "overrides_verified": override_stats["verified"],
        "overrides_retargeted": override_stats["retargeted"],
        "overrides_excluded": override_stats["excluded"],
        "overrides_spam_dropped": override_stats["spam_dropped"],
        "overrides_worth_dropped": override_stats["worth_dropped"],
        "worth_dropped_person_ids": override_stats["worth_dropped_person_ids"],
        "merged_rows": len(merged_rows),
        "rapidapi_payload_rows": len(merged_rows),
        "linkedin_groups": len(groups),
        "review_pairs": len(review),
        "source_rows": len(source_rows),
        "company_rows": len(company_rows),
        "output": str(output),
        "people_csv": str(output),
        "network_contacts_csv": str(network_contacts_path),
        "network_contact_sources_csv": str(network_contact_sources_path),
        "network_companies_csv": str(network_companies_path),
        "legacy_output": str(legacy_output),
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    emit({"status": "completed", **manifest_payload, "manifest": str(manifest)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge/dedupe local network import people artifacts")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--input", action="append", help="Input people.csv, people_harmonic_all.csv, or messages contacts.csv; repeatable. No filesystem discovery is performed.")
    run.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    run.add_argument("--name-threshold", type=float, default=0.92)
    # Defaults are None -> resolved to <output-dir>/../overrides/<file> at run time; '' disables.
    run.add_argument("--overrides", default=None,
                     help="Self-heal override CSV (detach/verify/retarget per public_identifier); defaults to the overrides/ sibling of --output-dir, '' to disable.")
    run.add_argument("--retarget-people", default=None,
                     help="Enriched re-attach rows from apply-retargets; defaults to the overrides/ sibling of --output-dir, '' to disable.")
    run.add_argument("--consolidate-people", default=None,
                     help="Contact-only rows folding parent children onto the kept LinkedIn; defaults to the overrides/ sibling of --output-dir, '' to disable.")
    run.add_argument("--synthetic-people", default=None,
                     help="Deep-researched synthetic rows (no real LinkedIn) from assemble_synthetic_profile; only approved (auto/yes) rows survive the keep-filter. Defaults to the overrides/ sibling of --output-dir, '' to disable.")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
