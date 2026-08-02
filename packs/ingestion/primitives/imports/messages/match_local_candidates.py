#!/usr/bin/env python3
"""Local name matcher between message contacts and an explicit people catalog.

Fuzzy scoring uses `difflib.SequenceMatcher` (stdlib); the tier thresholds below
are tuned to its ratio, so don't swap the scorer without re-tuning them.

Tiers (highest precedence first):

0. Unique exact phone match (E.164/last-10-digits) → matched, confidence 1.0;
   unique exact email match on an email handle → matched, confidence 1.0.
   These run before name tiers and work for contacts with no name at all.
1. Single exact normalized-name match → matched, confidence 1.0
2. Multiple exact normalized-name matches → suggested, confidence 0.80
3. Single-token first-name-only match → suggested, never matched
4. Same last-name pool with a unique first-name prefix candidate → matched
5. Same last-name pool with multiple prefix candidates → suggested (best score)
6. Fuzzy ratio in same-last-name pool ≥ 0.94 with margin ≥ 0.05 → matched
7. Fuzzy ratio ≥ 0.80 → suggested
8. Otherwise unmatched

Candidates come from the people CSVs the CALLER names — `--local-people` and the
optional `--candidates` — and from nowhere else. There is no default catalog: the
canonical `$import-messages` flow concatenates the already-imported Gmail and
LinkedIn `people.csv` rows into `.powerpacks/messages/_local_people.csv` and
passes that. Omitting both means every contact comes back `unmatched`.

Usage:
    match_local_candidates.py match \
        --contacts .powerpacks/messages/contacts.csv \
        --local-people .powerpacks/messages/_local_people.csv \
        [--candidates PATH] [--review PATH] [--manifest PATH]

A manifest JSON is written next to the contacts CSV with
`stats: {total, matched, suggested, unmatched}`.

Approval gate: identifier matches never expand the user's approved set on
their own. `matched` from tier 0 is only emitted for contacts the user
already approved in the research review (`in_network=true`); contacts the
user reviewed without approving are left untouched by tier 0; contacts that
were never reviewed get at most `suggested`, which requires human approval
before import.

Known gap: the approvals input (`research_review.csv`) has no living
producer, so on a fresh install the tier-0 gate can never pass and every
identifier match demotes to `suggested`. The replacement approval surface
belongs to $deep-context (suggestions review / conservative auto-attach);
until it exists, matched-people attachment effectively requires that legacy
file.

Updates the message-contacts CSV in place with the
`match_status / matched_person_id / matched_name / matched_linkedin_url /
match_confidence / match_method / match_reason` columns.

Declared contract (`ContactsMatch`, node `messages_match_local`):

  reads   contacts.csv (all 19 columns), research_review.csv (external — no
          producer since #315)
  writes  contacts.csv, `annotate` mode, owning ONLY the 7 columns in
          `util.MATCH_ANNOTATION_COLUMNS`; `skip` and discovery's 11 metadata
          columns are not this node's to write. The whole file is rewritten
          because csv cannot update a cell in place — the DECLARATION, not the
          write call, is what says which values are ours.

`--local-people` and `--candidates` are NOT declared, for the same reason: they
are caller-chosen catalogs with no fixed path, so they have no name in a graph
keyed by fixed paths. That is also why `--local-people` no longer defaults to
`merged/people.csv` — a default pointing at the file this node's own downstream
merge produces was the graph's 18-of-23 cycle (merge_people ->
messages_match_local -> messages_import -> merge_people), and it contradicted the
only real caller, which passes `import/{gmail,linkedin}/people.csv` concatenated.

Changelog:
  2026-08-01 (name-index person-id dedupe): the exact/last/first-name indexes
    now skip a candidate whose person id is already in the bucket, matching
    the guard the phone/email indexes always had. The catalog concatenates
    per-source people files, so one resolved person appears once per source
    with the same id; without the guard a single person read as "2 exact-name
    candidates" and a correct match demoted to suggested (80% of exact-name
    demotions on a real store were this false ambiguity). Distinct ids still
    count as real ambiguity.
  2026-07-30 (style pass): `MessageContactRow` is imported from its definition
    home (`discover/messages/models.py`) instead of through `util`'s re-export.
  2026-07-26 (cyclic default removed): `--local-people` has NO default. It was
    `.powerpacks/network-import/merged/people.csv` — the fan-in merge's own output,
    fed by this node's own downstream import — which made the declared graph cyclic
    while the canonical `$import-messages` invocation (Step 3, explicit
    `--local-people`) was already acyclic. The catalog is an explicit caller
    argument now, like `--candidates`, and `--no-local-people` went with the
    default it existed to suppress.
  2026-07-25 (declared contract): `cmd_match(args)` + `set_defaults(func=...)`
    became the `ContactsMatch` Node — construct-and-run, declared inputs/outputs,
    and the run manifest written by the Node template (so it gains `status` and
    the declared-output `fingerprints`). Matching itself is untouched: the tier
    ladder, thresholds, approval gate, and written columns are byte-identical.
  2026-07-23 (audit):
    - match_local_candidates.README.md sidecar folded into this docstring.
    - The research_review.csv producer (the research-review flow) was retired
      in #315, opening the known gap above.
    - Moved from primitives/match_local_candidates/ into
      imports/messages/; the duplicated try/except import
      block became the single repo-root bootstrap stanza.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

# Repo-root bootstrap so packs.* imports work in module AND script mode
# (uv run .../match_local_candidates.py); must be in-file because script mode
# never imports the package __init__.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit, now_iso  # noqa: E402
from packs.ingestion.primitives.common.paths import MESSAGES_OUT_DIR  # noqa: E402
# The declared row shape of `.powerpacks/messages/contacts.csv`, imported from the
# DISCOVERY module that owns the file. `graph.check_graph` compares row models by
# IDENTITY, so every writer of that file must name THIS object — not an equal
# copy, and not a second module that re-exports it.
from packs.ingestion.primitives.discover.messages.models import (  # noqa: E402
    MessageContactRow,
)
from packs.ingestion.primitives.imports.messages.util import (  # noqa: E402
    MATCH_ANNOTATION_COLUMNS,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest  # noqa: E402
from packs.ingestion.schemas.message_contacts import (  # noqa: E402
    CSV_HEADERS,
    REQUIRED_INPUT_HEADERS,
    SCHEMA_DOC,
    SCHEMA_JSON,
)
from packs.shared.csv_io import CsvIO  # noqa: E402


DEFAULT_CONTACTS_CSV = MESSAGES_OUT_DIR / "contacts.csv"
# The matcher's own output, and the gate the messages import checks for.
DEFAULT_MATCH_MANIFEST = MESSAGES_OUT_DIR / "contacts.csv.match.manifest.json"
DEFAULT_REVIEW_CSV = Path(".powerpacks/messages/research_review.csv")


@dataclass
class Candidate:
    id: str
    name: str
    linkedin_url: str | None = None
    phone_number: str | None = None
    public_identifier: str | None = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_name(raw: str | None) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (raw or "").strip().lower())
    return re.sub(r"\s+", " ", s).strip()


def phone_match_key(raw: str | None) -> str:
    """Digits-only phone key; 10+ digit numbers compare by their last 10 so
    +14155550123, 14155550123, and 4155550123 all collide."""
    digits = re.sub(r"\D+", "", raw or "")
    if len(digits) < 7:
        return ""
    return digits[-10:] if len(digits) >= 10 else digits


def email_match_key(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    return value if "@" in value else ""


def parse_listish(raw: str | None) -> list[str]:
    value = (raw or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [part.strip() for part in re.split(r"[;,]", value) if part.strip()]


def first_name_prefix_match(a: str, b: str) -> bool:
    """True when first names look like strong prefix variants."""
    a = (a or "").strip()
    b = (b or "").strip()
    if len(a) < 4 or len(b) < 4:
        return False
    return a.startswith(b) or b.startswith(a)


def load_candidates(path: Path) -> list[Candidate]:
    if not path.exists():
        return []
    out: list[Candidate] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = CsvIO.dict_reader(handle)
        for row in reader:
            cid = (row.get("id") or "").strip()
            name = (row.get("name") or "").strip()
            if not cid or not name:
                continue
            emails_raw = (row.get("emails") or "").strip()
            emails = [e for e in emails_raw.split(";") if e]
            phone = (row.get("phone_number") or "").strip()
            out.append(Candidate(
                id=cid,
                name=name,
                linkedin_url=(row.get("linkedin_url") or "").strip() or None,
                phone_number=phone or None,
                public_identifier=(row.get("public_identifier") or "").strip() or None,
                emails=emails,
                phones=[phone] if phone else [],
            ))
    return out


def load_review_approvals(path: Path) -> dict[str, bool] | None:
    """Map contact identifier keys (phone/email) -> approved (in_network) from
    the research review. None when no review exists (nothing reviewed yet)."""
    if not path.exists():
        return None
    approvals: dict[str, bool] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in CsvIO.dict_reader(handle):
            approved = (row.get("in_network") or "").strip().lower() in {"true", "yes", "1"}
            for raw in [row.get("phone_e164"), row.get("handle")]:
                key = email_match_key(raw) or phone_match_key(raw)
                if key:
                    # Any approved row wins over an unapproved duplicate.
                    approvals[key] = approvals.get(key, False) or approved
    return approvals


def load_people_candidates(path: Path, known_ids: set[str], known_identifiers: set[str]) -> list[Candidate]:
    """Load merged people.csv, skipping entries already in an explicit catalog."""
    if not path.exists():
        return []
    out: list[Candidate] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = CsvIO.dict_reader(handle)
        for row in reader:
            cid = (row.get("id") or "").strip()
            name = (row.get("full_name") or "").strip()
            if not cid or cid in known_ids:
                continue
            public_identifier = (row.get("public_identifier") or "").strip().lower()
            if public_identifier and public_identifier in known_identifiers:
                continue
            phones = parse_listish(row.get("all_phones")) or parse_listish(row.get("primary_phone"))
            emails = [email.lower() for email in (parse_listish(row.get("all_emails")) or parse_listish(row.get("primary_email")))]
            if not name and not phones and not emails:
                continue
            out.append(Candidate(
                id=cid,
                name=name,
                linkedin_url=(row.get("linkedin_url") or "").strip() or None,
                phone_number=phones[0] if phones else None,
                public_identifier=public_identifier or None,
                emails=emails,
                phones=phones,
            ))
    return out


def schema_error(path: Path, fieldnames: list[str] | None) -> str:
    fields = ",".join(fieldnames or []) or "<none>"
    header = ",".join(CSV_HEADERS)
    return (
        f"CSV schema mismatch for {path}. Please convert this file into the Powerpacks messages contacts CSV schema before retrying. "
        f"Required input columns: phone,name. Canonical header: {header}. "
        f"Detected columns: {fields}. Schema docs: {SCHEMA_DOC}. JSON schema: {SCHEMA_JSON}. "
        "Common legacy mappings: phone_e164/phone_number -> phone; display_name/full_name -> name; "
        "total_messages -> message_count; imessage_count/imessage_messages -> imessage_message_count; "
        "whatsapp_count/whatsapp_messages -> whatsapp_message_count; message_source/source_channel -> source."
    )


def validate_input_headers(path: Path, fieldnames: list[str] | None) -> None:
    names = {str(value or "").strip() for value in (fieldnames or [])}
    if not REQUIRED_INPUT_HEADERS.issubset(names):
        raise SystemExit(schema_error(path, fieldnames))


def _set_unmatched(row: dict[str, str], reason: str) -> None:
    row["match_status"] = "unmatched"
    row["matched_person_id"] = ""
    row["matched_name"] = ""
    row["matched_linkedin_url"] = ""
    row["match_confidence"] = ""
    row["match_method"] = "unmatched"
    row["match_reason"] = reason


def _set_match(
    row: dict[str, str],
    *,
    status: str,
    candidate: Candidate,
    confidence: float,
    method: str,
    reason: str,
) -> None:
    row["match_status"] = status
    row["matched_person_id"] = candidate.id
    row["matched_name"] = candidate.name
    row["matched_linkedin_url"] = candidate.linkedin_url or ""
    row["match_confidence"] = f"{confidence:.3f}".rstrip("0").rstrip(".") or "0"
    row["match_method"] = method
    row["match_reason"] = reason


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

def apply_matching(
    rows: list[dict[str, str]],
    candidates: list[Candidate],
    approvals: dict[str, bool] | None = None,
) -> dict[str, int]:
    if not rows:
        return {"total": 0, "matched": 0, "suggested": 0, "unmatched": 0}
    if not candidates:
        for row in rows:
            _set_unmatched(row, "no local candidate catalog available")
        return {"total": len(rows), "matched": 0, "suggested": 0, "unmatched": len(rows)}

    exact_index: dict[str, list[Candidate]] = {}
    last_name_index: dict[str, list[Candidate]] = {}
    first_name_index: dict[str, list[Candidate]] = {}
    phone_index: dict[str, list[Candidate]] = {}
    email_index: dict[str, list[Candidate]] = {}
    for c in candidates:
        for phone in c.phones or ([c.phone_number] if c.phone_number else []):
            key = phone_match_key(phone)
            if key and not any(existing.id == c.id for existing in phone_index.get(key, [])):
                phone_index.setdefault(key, []).append(c)
        for email in c.emails:
            key = email_match_key(email)
            if key and not any(existing.id == c.id for existing in email_index.get(key, [])):
                email_index.setdefault(key, []).append(c)
        norm = normalize_name(c.name)
        if not norm:
            continue
        # Same person-id dedupe as the identifier indexes above: the catalog
        # concatenates per-source people files (gmail + linkedin), so one
        # resolved person appears once per source with the SAME id. A bucket
        # must count DISTINCT people — without this guard, len(bucket) reads a
        # single person as "2 exact-name candidates" and demotes a correct
        # match to suggested. Two genuinely different people (different ids)
        # still occupy two slots and stay ambiguous.
        if not any(existing.id == c.id for existing in exact_index.get(norm, [])):
            exact_index.setdefault(norm, []).append(c)
        parts = norm.split(" ")
        if len(parts) >= 2:
            if not any(existing.id == c.id for existing in last_name_index.get(parts[-1], [])):
                last_name_index.setdefault(parts[-1], []).append(c)
            if not any(existing.id == c.id for existing in first_name_index.get(parts[0], [])):
                first_name_index.setdefault(parts[0], []).append(c)
    for index in (exact_index, last_name_index, first_name_index, phone_index, email_index):
        for bucket in index.values():
            bucket.sort(key=lambda c: c.id)

    matched = suggested = unmatched = 0

    for row in rows:
        # Tier 0: identifier matches run before name tiers and work for
        # contacts with no usable name (the largest unmatched bucket).
        # Approval gate: identifier matches never expand the approved set.
        # approved=True -> matched allowed; approved=False (user reviewed and
        # did not approve) -> tier 0 skips entirely; not reviewed yet -> at
        # most suggested, which requires human approval before import.
        handle = (row.get("phone") or "").strip()
        email_key = email_match_key(handle)
        identifier_key = email_key or phone_match_key(handle)
        approved = approvals.get(identifier_key) if approvals is not None else None
        identifier_pool = [] if (approvals is not None and approved is False) else (
            email_index.get(email_key, []) if email_key else phone_index.get(phone_match_key(handle), [])
        )
        if len(identifier_pool) == 1:
            method = "email_exact" if email_key else "phone_exact"
            if approved:
                matched += 1
                _set_match(row, status="matched", candidate=identifier_pool[0], confidence=1.0,
                           method=method, reason="unique exact identifier match (approved contact)")
            else:
                suggested += 1
                _set_match(row, status="suggested", candidate=identifier_pool[0], confidence=0.95,
                           method=method, reason="unique exact identifier match awaiting approval")
            continue
        if len(identifier_pool) > 1:
            suggested += 1
            method = "email_exact_ambiguous" if email_key else "phone_exact_ambiguous"
            _set_match(row, status="suggested", candidate=identifier_pool[0], confidence=0.90,
                       method=method, reason=f"{len(identifier_pool)} candidates share this identifier")
            continue

        contact_name = (row.get("name") or "").strip()
        norm_contact = normalize_name(contact_name)
        if not norm_contact:
            unmatched += 1
            _set_unmatched(row, "missing contact name")
            continue
        if norm_contact == normalize_name(row.get("phone")):
            unmatched += 1
            _set_unmatched(row, "name is identical to phone")
            continue

        exact = list(exact_index.get(norm_contact, []))
        if len(exact) == 1:
            matched += 1
            _set_match(row, status="matched", candidate=exact[0], confidence=1.0,
                       method="name_exact_linkedin", reason="unique exact name match")
            continue
        if len(exact) > 1:
            suggested += 1
            _set_match(row, status="suggested", candidate=exact[0], confidence=0.80,
                       method="name_exact_ambiguous", reason=f"{len(exact)} exact-name candidates")
            continue

        tokens = norm_contact.split(" ")
        if len(tokens) < 2:
            first_pool = list(first_name_index.get(tokens[0], []))
            if len(first_pool) == 1:
                suggested += 1
                _set_match(
                    row, status="suggested", candidate=first_pool[0], confidence=0.60,
                    method="name_first_only_unique_suggested",
                    reason="single-token first-name-only candidate requires review",
                )
                continue
            if len(first_pool) > 1:
                suggested += 1
                _set_match(
                    row, status="suggested", candidate=first_pool[0], confidence=0.70,
                    method="name_first_only_ambiguous",
                    reason=f"{len(first_pool)} candidates share this first name",
                )
                continue
            unmatched += 1
            _set_unmatched(row, "single-token name with no candidate first-name match")
            continue

        pool = list(last_name_index.get(tokens[-1], []))
        if not pool:
            unmatched += 1
            _set_unmatched(row, "no same-last-name candidates")
            continue

        contact_first = tokens[0]
        prefix_pool = []
        for cand in pool:
            cand_tokens = normalize_name(cand.name).split(" ")
            cand_first = cand_tokens[0] if cand_tokens else ""
            if first_name_prefix_match(contact_first, cand_first):
                prefix_pool.append(cand)

        if len(prefix_pool) == 1:
            cand = prefix_pool[0]
            ratio = SequenceMatcher(None, norm_contact, normalize_name(cand.name)).ratio()
            confidence = round(max(0.95, ratio), 3)
            matched += 1
            _set_match(row, status="matched", candidate=cand, confidence=confidence,
                       method="name_prefix_lastname_linkedin",
                       reason="unique first-name prefix with same last name")
            continue
        if len(prefix_pool) > 1:
            scored = sorted(
                ((SequenceMatcher(None, norm_contact, normalize_name(c.name)).ratio(), c) for c in prefix_pool),
                key=lambda item: item[0], reverse=True,
            )
            best_score, best_candidate = scored[0]
            suggested += 1
            _set_match(row, status="suggested", candidate=best_candidate,
                       confidence=round(float(max(best_score, 0.85)), 3),
                       method="name_prefix_lastname_suggested",
                       reason=f"{len(prefix_pool)} prefix candidates with same last name")
            continue

        scored = sorted(
            ((SequenceMatcher(None, norm_contact, normalize_name(c.name)).ratio(), c) for c in pool),
            key=lambda item: item[0], reverse=True,
        )
        best_score, best_candidate = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        confidence = round(float(best_score), 3)

        if best_score >= 0.94 and (best_score - second_score) >= 0.05:
            matched += 1
            _set_match(row, status="matched", candidate=best_candidate,
                       confidence=confidence, method="name_fuzzy_linkedin",
                       reason="high-confidence fuzzy last-name match")
            continue
        if best_score >= 0.80:
            suggested += 1
            _set_match(row, status="suggested", candidate=best_candidate,
                       confidence=confidence, method="name_fuzzy_suggested",
                       reason="high-confidence fuzzy last-name candidate")
            continue

        unmatched += 1
        _set_unmatched(row, "low-confidence fuzzy candidate")

    # Approval gate, applied to every tier: once the user has reviewed
    # (a research review exists), a match may only carry `matched` status for
    # contacts the user approved. Anything else — including name-tier matches
    # against newly added local candidates — demotes to `suggested` so it goes
    # back through review instead of silently expanding the approved set.
    if approvals is not None:
        for row in rows:
            if row.get("match_status") != "matched":
                continue
            handle = (row.get("phone") or "").strip()
            key = email_match_key(handle) or phone_match_key(handle)
            if not approvals.get(key):
                row["match_status"] = "suggested"
                row["match_reason"] = (row.get("match_reason") or "").rstrip() + " (awaiting approval)"
        matched = sum(1 for row in rows if row.get("match_status") == "matched")
        suggested = sum(1 for row in rows if row.get("match_status") == "suggested")
        unmatched = sum(1 for row in rows if row.get("match_status") == "unmatched")

    return {
        "total": len(rows),
        "matched": matched,
        "suggested": suggested,
        "unmatched": unmatched,
    }


# ---------------------------------------------------------------------------
# CSV IO
# ---------------------------------------------------------------------------

def read_contacts(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SystemExit(f"contacts file not found: {path}")
    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = CsvIO.dict_reader(handle)
        if not reader.fieldnames:
            return []
        validate_input_headers(path, reader.fieldnames)
        for row in reader:
            normalized = {key: (row.get(key) or "") for key in CSV_HEADERS}
            rows.append(normalized)
    return rows


def write_contacts(path: Path, rows: list[dict[str, str]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class MatchManifest(StageManifest):
    """This node's typed run manifest. Same field names and values the raw dict
    carried, in the same order; `status` comes from the StageManifest base and is
    what tells the run template the outputs are worth verifying."""

    primitive: str = "match_local_candidates"
    command: str = "match"
    started_at: str = ""
    elapsed_ms: int = 0
    contacts_path: str = ""
    candidates_path: str = ""
    candidates_loaded: int = 0
    explicit_catalog_candidates: int = 0
    local_people_candidates: int = 0
    local_people_path: str = ""
    review_path: str = ""
    approved_contacts: int = 0
    rows_written: int = 0
    manifest_path: str = ""
    stats: dict[str, int] = {}


class ContactsMatch(Node):
    """Tiers every message contact against the local people catalog and annotates
    `contacts.csv` in place. Owns its fixed paths, the catalog load, the tier
    ladder, and the one run manifest. Construct with explicit paths and call
    `run()` (the Node template: declared inputs -> `execute()` -> declared outputs
    -> manifest)."""

    name = "messages_match_local"
    inputs = (
        # Written by discover/messages. Required: there is nothing to match without it.
        Artifact(path=str(DEFAULT_CONTACTS_CSV), row_model=MessageContactRow),
        # The user's approvals. `external`: its producer (the research-review flow)
        # was retired in #315 and nothing replaced it, so on a fresh install this
        # file never exists and every tier-0 identifier match demotes to
        # `suggested` (the Known gap above).
        Artifact(path=str(DEFAULT_REVIEW_CSV), external=True, required=False),
        # NOT declared: `--local-people` and `--candidates`. Both are explicit,
        # caller-chosen catalogs with no default path, so neither has a name in a
        # graph keyed by fixed paths. `--local-people` USED to default to
        # `merged/people.csv` and was declared — that default was the cycle.
    )
    outputs = (
        Artifact(
            path=str(DEFAULT_CONTACTS_CSV),
            row_model=MessageContactRow,
            writes="annotate",
            owns_columns=MATCH_ANNOTATION_COLUMNS,
        ),
    )
    payload = MatchManifest
    manifest = str(DEFAULT_MATCH_MANIFEST)

    def __init__(
        self,
        *,
        contacts: Path,
        candidates: Path | None = None,
        local_people: Path | None = None,
        review: Path | None = None,
        manifest_path: Path | None = None,
    ) -> None:
        self.contacts_csv = Path(contacts)
        self.candidates_csv = Path(candidates) if candidates else None
        # No default: the catalog is the caller's to name (see the module docstring).
        self.local_people_csv = Path(local_people) if local_people else None
        self.review_csv = Path(review) if review else DEFAULT_REVIEW_CSV
        self.manifest_json = Path(manifest_path) if manifest_path else self.contacts_csv.with_suffix(
            self.contacts_csv.suffix + ".match.manifest.json"
        )

    def bindings(self) -> dict[str, str]:
        """Declared path -> this instance's path, so an explicit --contacts /
        --review / --manifest still validates against the declaration. Keys come
        from the module path constants the declaration itself names, never from
        tuple position or a second default read."""
        return {
            str(DEFAULT_CONTACTS_CSV): str(self.contacts_csv),
            str(DEFAULT_REVIEW_CSV): str(self.review_csv),
            str(DEFAULT_MATCH_MANIFEST): str(self.manifest_json),
        }

    def execute(self) -> MatchManifest:
        """Load both catalogs, run the tier ladder, rewrite the annotated contacts
        (the Node template writes the manifest)."""
        started = time.time()
        rows = read_contacts(self.contacts_csv)
        candidates = load_candidates(self.candidates_csv) if self.candidates_csv else []
        local_candidates: list[Candidate] = []
        if self.local_people_csv:
            known_ids = {c.id for c in candidates}
            known_identifiers = {(c.public_identifier or "").lower() for c in candidates if c.public_identifier}
            local_candidates = load_people_candidates(self.local_people_csv, known_ids, known_identifiers)
        approvals = load_review_approvals(self.review_csv)
        stats = apply_matching(rows, candidates + local_candidates, approvals=approvals)
        written = write_contacts(self.contacts_csv, rows)
        return MatchManifest(
            status="completed",
            started_at=now_iso(),
            elapsed_ms=int((time.time() - started) * 1000),
            contacts_path=str(self.contacts_csv),
            candidates_path=str(self.candidates_csv) if self.candidates_csv else "",
            candidates_loaded=len(candidates) + len(local_candidates),
            explicit_catalog_candidates=len(candidates),
            local_people_candidates=len(local_candidates),
            local_people_path=str(self.local_people_csv) if local_candidates else "",
            review_path=str(self.review_csv) if approvals is not None else "",
            approved_contacts=sum(1 for value in (approvals or {}).values() if value),
            rows_written=written,
            manifest_path=str(self.manifest_json),
            stats=stats,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Match message contacts against local people")
    parser.add_argument("command", choices=["match"])
    parser.add_argument("--contacts", required=True, help="Path to the message-contacts CSV")
    parser.add_argument("--candidates",
                        help="Optional additional candidate CSV; omitted by the canonical local-only flow")
    parser.add_argument("--local-people", help="People CSV to union into the candidate catalog "
                        "(no default; $import-messages passes the concatenated gmail + linkedin import people)")
    parser.add_argument("--review", help="Research review CSV holding the user's in_network approvals "
                        f"(default: {DEFAULT_REVIEW_CSV} when present)")
    parser.add_argument("--manifest", help="Path to write the run manifest JSON")
    return parser


def main() -> int:
    """Exit 0 when the match completed, 1 when a declared input was missing."""
    args = build_parser().parse_args()
    payload = ContactsMatch(
        contacts=Path(args.contacts),
        candidates=Path(args.candidates) if args.candidates else None,
        local_people=Path(args.local_people) if args.local_people else None,
        review=Path(args.review) if args.review else None,
        manifest_path=Path(args.manifest) if args.manifest else None,
    ).run()
    emit(payload.to_payload())
    return 0 if payload.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
