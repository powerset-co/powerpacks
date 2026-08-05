"""Shared durable review.csv storage.

The file mixes LinkedIn identity decisions with network-worth decisions, but the
two producers remain independent:

* LinkedIn reconciliation owns action/approved/link fields.
* Message synthesis owns child llm_worth/llm_worth_reason.
* Parent construction mirrors the aggregated machine verdict into one parent row.
* The human alone owns that parent row's network_worth.

Keeping the tiny CSV contract here prevents either LLM stage from becoming the
other stage's fallback writer.

Changelog:
  2026-08-05 (sqlite P0): the review value vocabulary becomes StrEnums —
    `ReviewAction`, `ApprovedState`, `MachineWorth`, `HumanWorth`,
    `ReviewSource` — measured from both real stores (arthur 1292 rows, jake
    16891 rows) plus every literal a writer stamps. The legacy sets
    (HUMAN_WORTH_VALUES etc.) are now derived FROM the enums so the vocabulary
    keeps one home; review_db.py generates its SQL CHECK constraints from the
    same enums. Values are unchanged.
  2026-07-30 (style): `llm_network_worth` is imported at module top instead of inside
    `mirror_facts_worth`. The old note claimed the deferral kept "the basic CSV
    contract" independent of dossier parsing, but `candidates` imports only
    `deep_context.common` — there was never a cycle to break. Also documented the
    two row shapes `_undecided_candidate_retarget` accepts (review row vs review-UI
    candidate) at the definition, since that fallback reads as a typo otherwise.
    No behavior change.
  2026-07-27 (declared contract): `ReviewRow` — the pydantic row model generated
    from OVERRIDE_COLUMNS. The synthesize and reconcile nodes both declare
    `review.csv` with disjoint `owns_columns` slices, and the graph checker
    requires one shared row-model OBJECT, so it lives here with the column list.
  2026-07-23 (audit dedup): now_iso import from common.jsonio instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import csv
import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.candidates import llm_network_worth
from packs.ingestion.primitives.pipeline.contract import row_model_for


OVERRIDE_COLUMNS = [
    "public_identifier",
    # Canonical membership for parent-level worth rows. Identity/link rows
    # leave this blank. Membership lets human decisions survive reclustering.
    "worth_person_ids",
    "action",
    "approved",
    "new_linkedin_url",
    "new_public_identifier",
    "linkedin_url",
    "match_emails",
    "match_phones",
    "confidence",
    "reason",
    "person_id",
    "source",
    "updated_at",
    # Legacy/profile-research fields. The old LinkedIn spam screen wrote
    # llm_reject=spam; synthesis clears that value when it mirrors worth so it
    # can no longer act as a second hidden worth decision.
    "llm_reject",
    "llm_reject_confidence",
    "llm_reject_reason",
    # Machine-owned sha256 of the EVIDENCE the retarget identity judge consumed
    # (proposal_fingerprint in reconcile_deep_research). A later pass whose
    # would-be proposal matches this sha reuses the stored verdict — including
    # rejections — instead of re-judging; changed evidence re-judges.
    "llm_judge_fingerprint",
    # Machine-owned worth mirrored from facts/<person_id>.jsonl.
    "llm_worth",
    "llm_worth_reason",
    # Human-owned worth. Machine writers must never change it.
    "network_worth",
    # Human-owned free-text "why" captured with a worth decision (the optional
    # collapsed box on the review card). Machine writers must never change it.
    "user_worth_note",
]

# The declared row shape of review.csv, generated FROM OVERRIDE_COLUMNS so the
# column list keeps one home. Both writer nodes (synthesize, reconcile) must
# reference THIS object — the graph checker treats two different row-model
# objects on one path as a schema mismatch.
ReviewRow = row_model_for("ReviewRow", OVERRIDE_COLUMNS)

# The identity judge's asymmetric bars, in the ONE home every reader shares
# (reconcile_linkedin aliases these for its apply pass; review display and the
# legacy stored-row scrub read them directly).
#
# Confirm (low, keep-biased): a confirmed link auto-verifies here — keeping a
# slightly-wrong link is cheap because the user fixes it in review.
JUDGE_CONFIRM_THRESHOLD = 0.70
# Detach (high): dropping a real person is the costly error. reconcile
# auto-APPLIES a wrong_person verdict at/above this only when a confirmed
# sibling wins the conflict group — but the verdict itself is authoritative
# either way: review surfaces treat an unapplied >= bar detach as detached
# (the human never re-reviews a hard-contradicted profile), while a below-bar
# detach stays a pending human decision.
JUDGE_DETACH_THRESHOLD = 0.85
# Decisive: a group's ONLY bar-clearing confirm at/above this wins outright —
# keep it, detach every other candidate regardless of detach confidence. Two
# bar-clearing confirms is genuine ambiguity (family collisions) and stays
# with the human.
DECISIVE_CONFIRM_THRESHOLD = 0.95

class ReviewAction(StrEnum):
    """Identity outcomes an `action` cell can carry (empty = no proposal)."""

    VERIFY = "verify"
    DETACH = "detach"
    RETARGET = "retarget"
    EXCLUDE = "exclude"


class ApprovedState(StrEnum):
    """Who settled an identity decision. Empty `approved` = still pending.

    `auto` and `source` are NOT redundant: real stores carry approved=auto
    rows with a human source (jake: 95 x deep-context-review) and
    approved=yes rows only from human sources — the approved class records
    WHO the settlement counts as, the source records WHICH writer stamped it.
    """

    AUTO = "auto"  # machine-standing (judge auto-applied / promoted)
    YES = "yes"    # human confirmation
    NO = "no"      # human rejection


class MachineWorth(StrEnum):
    """llm_worth values synthesis mirrors from facts/<id>.jsonl."""

    YES = "yes"
    MAYBE = "maybe"
    NO = "no"


class HumanWorth(StrEnum):
    """network_worth values a human can set (never written by machines)."""

    YES = "yes"
    NO = "no"


class ReviewSource(StrEnum):
    """Every writer that stamps a review.csv `source` cell, one member per
    stamp site. Adding a writer means adding a member HERE — review_db.py
    generates its SQL CHECK from this enum, so an unlisted source fails
    loudly at import instead of silently accreting a new vocabulary."""

    REVIEW = "deep-context-review"                # review_web/decisions.py (human card decision)
    USER_GUIDANCE = "user-guidance"               # review_web/retarget_queue.py (paste/guided retarget)
    RECONCILE = "deep-context-reconcile"          # reconcile_linkedin apply pass
    DEEP_RESEARCH = "deep-research"               # reconcile_deep_research proposals
    SYNTHESIS = "deep-context-synthesis"          # mirror_facts_worth (this module)
    PARENT_WORTH = "deep-context-parent-worth"    # worth_view / decisions.py parent rows
    HEAL = "deep-context-heal"                    # heal_review dead-link detaches
    NAME_MATCH = "deep-context-name-match"        # reconcile_linkedin name-match pass
    SELF_REPORTED = "dossier-self-reported"       # reconcile_linkedin self-reported links
    SIBLING_SETTLE = "legacy-sibling-settle"      # common/legacy.py rule 4
    LEGACY_MIGRATION = "legacy-migration"         # migrate_legacy_resolutions


# Sources that count as a HUMAN having decided (terminal for machine writers).
HUMAN_DECISION_SOURCES = frozenset({ReviewSource.REVIEW.value, ReviewSource.USER_GUIDANCE.value})

# Legacy set spellings, derived from the enums so the vocabulary keeps one home.
HUMAN_WORTH_VALUES = frozenset(member.value for member in HumanWorth)
MACHINE_WORTH_VALUES = frozenset(member.value for member in MachineWorth)
USER_APPROVED = frozenset({ApprovedState.YES.value, ApprovedState.NO.value})
PARENT_WORTH_PREFIX = "parent-worth:"

# The `source` the heal pass stamps on its dead-link detaches (heal_review).
# Lives here — the review.csv contract home — because reconcile_deep_research
# must recognize it too (a heal detach is a re-research INVITATION, not a
# decision) and importing heal_review there would cycle through
# assemble_synthetic_profile.
HEAL_DETACH_SOURCE = ReviewSource.HEAL.value


def parent_worth_key(parent_id: str) -> str:
    """The one review.csv key for a canonical parent's worth decision."""
    value = str(parent_id or "").strip().lower()
    return f"{PARENT_WORTH_PREFIX}{value}" if value else ""


def parent_id_from_worth_key(key: str) -> str:
    value = str(key or "").strip().lower()
    return value.removeprefix(PARENT_WORTH_PREFIX) if value.startswith(PARENT_WORTH_PREFIX) else ""


def is_parent_worth_row(row: dict[str, Any], key: str = "") -> bool:
    return bool(
        parent_id_from_worth_key(key or str(row.get("public_identifier") or ""))
    )


def parse_worth_person_ids(row: dict[str, Any]) -> list[str]:
    raw = str(row.get("worth_person_ids") or "")
    return sorted({value.strip().lower() for value in raw.split("|") if value.strip()})

# The deep-research judge's confirm bar (reconcile_deep_research --confirm-threshold
# defaults to this). Doing double duty is the point: research_reject_fields stamps
# llm_reject=yes for wrong_person AND needs_review AND confirmed-below-bar alike, so a
# rejection's confidence only proves "definitely not a near-confirm" when it is AT or
# ABOVE the same bar (a confirm at/above it never gets llm_reject at all). Keeping one
# constant for both keeps that structural guarantee from drifting. NOTE the guarantee
# only holds for rows STAMPED under this bar: rows stamped under an older, HIGHER bar
# can be confirm-flavored inside [current bar, old bar) — fresh runs stamp clean, and
# changed evidence re-judges via the fingerprint cache.
RESEARCH_CONFIRM_THRESHOLD = 0.80

# review.csv stores llm_reject as free text; these are the truthy spellings.
_REJECT_TRUTHY = {"1", "true", "yes"}


def _undecided_candidate_retarget(row: dict[str, Any]) -> bool:
    """Shared gate of both stand-predicates below: a found-LinkedIn retarget on
    a candidate-origin identity (candidate:*) with no terminal human decision.
    Real-network people (directory uuids etc.) never pass — re-attaching a
    wrong identity on an existing person stays human-gated.

    TWO SHAPES, ONE PREDICATE (deliberate, and the reason for the `person_id` /
    `pub` fallback): `apply_retargets` passes a raw review.csv row, whose person
    key is `person_id`, while the review server's `pending_linkedin_candidates`
    passes a UI candidate dict, whose person key is `pub`. Both name the same
    thing. This is the "parse at the boundary" debt of the review UI's untyped
    candidate dict, not a divergence — when the review-UI model is typed, the
    caller should hand this predicate ONE parsed shape and the fallback goes."""
    if (str(row.get("action") or "").strip().lower() != "retarget"
            or str(row.get("approved") or "").strip().lower() in USER_APPROVED):
        return False
    person_id = str(row.get("person_id") or row.get("pub") or "").strip().lower()
    return person_id.startswith("candidate:")


def judge_accepted_candidate_retarget(row: dict[str, Any]) -> bool:
    """A candidate-origin found-LinkedIn the identity judge ACCEPTED and no
    human has overridden: its verdict STANDS — it neither waits in the review
    queue nor blocks application. The judge ran against the full dossier
    evidence and rejects bad matches via llm_reject*, so re-asking a human to
    confirm every acceptance was decision-theater at enrichment scale (569 of
    642 pending checks on real data). A human yes/no is still terminal."""
    return (_undecided_candidate_retarget(row)
            and str(row.get("llm_reject") or "").strip().lower() not in _REJECT_TRUTHY)


def judge_rejected_candidate_retarget(row: dict[str, Any]) -> bool:
    """The mirror image: a candidate-origin found-LinkedIn the judge REJECTED at
    or above the confirm bar, with no human decision — the rejection STANDS and
    the card leaves the Check-LinkedIn queue (the reject is never applied, so the
    person simply moves on without that profile; new evidence can still re-propose).

    The bar matters: llm_reject=yes conflates wrong_person, needs_review, and
    confirmed-below-bar verdicts, and on real data most sub-bar rejections were
    NEAR-CONFIRMS ("name + location match, no hard conflicts") sitting just
    under the bar. Only at/above RESEARCH_CONFIRM_THRESHOLD is a rejection
    structurally guaranteed not to be a confirm flavor — those read "the sender
    is a different named person" — so only those stand. Sub-bar rejections keep
    the human, and a human yes/no stays terminal either way."""
    if (not _undecided_candidate_retarget(row)
            or str(row.get("llm_reject") or "").strip().lower() not in _REJECT_TRUTHY):
        return False
    try:
        confidence = float(str(row.get("llm_reject_confidence") or "").strip())
    except ValueError:
        return False
    return confidence >= RESEARCH_CONFIRM_THRESHOLD


def load_override_rows(path: Path) -> dict[str, dict[str, str]]:
    """Load existing decisions keyed by the row's public_identifier field."""
    rows: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                key = (row.get("public_identifier") or "").strip().lower()
                if key:
                    rows[key] = row
    return rows


def write_override_rows(path: Path, rows: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OVERRIDE_COLUMNS)
        writer.writeheader()
        for key in sorted(rows):
            writer.writerow({column: rows[key].get(column, "") for column in OVERRIDE_COLUMNS})


def row_keys_for_person(rows: dict[str, dict[str, str]], person_id: str) -> list[str]:
    """Every review row representing one stable dossier person id."""
    pid = (person_id or "").strip().lower()
    if not pid:
        return []
    return [
        key
        for key, row in rows.items()
        if key == pid or (row.get("person_id") or "").strip().lower() == pid
    ]


def parent_ids_by_person(index_json: Path) -> dict[str, str]:
    """Map current child person ids to canonical parent ids."""
    try:
        index = json.loads(index_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    slugs = index.get("slugs") or {}
    out: dict[str, str] = {}
    for parent in (index.get("parents") or {}).values():
        parent_id = str(parent.get("parent_id") or "").strip().lower()
        if not parent_id:
            continue
        for child in parent.get("children") or []:
            person_id = str((slugs.get(child) or {}).get("person_id") or "").strip().lower()
            if person_id:
                out[person_id] = parent_id
    return out


def has_human_worth(
    rows: dict[str, dict[str, str]],
    person_id: str,
    parent_ids: dict[str, str] | None = None,
) -> bool:
    pid = str(person_id or "").strip().lower()
    parent_id = (parent_ids or {}).get(pid, "")
    parent_row = rows.get(parent_worth_key(parent_id)) or {}
    if (parent_row.get("network_worth") or "").strip().lower() in HUMAN_WORTH_VALUES:
        return True
    return any(
        (rows[key].get("network_worth") or "").strip().lower() in HUMAN_WORTH_VALUES
        for key in row_keys_for_person(rows, person_id)
    )


def mirror_facts_worth(
    review_path: Path,
    facts_dir: Path,
    *,
    include_human_rows: bool = False,
) -> dict[str, Any]:
    """Mirror every facts worth verdict into review.csv.

    Normal synthesis leaves rows with a human Yes/No completely untouched.
    ``$deep-context rejudge`` sets ``include_human_rows`` so the refreshed
    machine opinion is visible beside the sticky human decision; the human
    ``network_worth`` cell itself is always preserved.
    """
    rows = load_override_rows(review_path)
    parent_ids = parent_ids_by_person(facts_dir.parent / "index.json")
    synced_people = synced_rows = skipped_human = without_worth = cleared_legacy_spam = 0

    for facts_path in sorted(facts_dir.glob("*.jsonl")):
        person_id = facts_path.stem
        worth = llm_network_worth(person_id, facts_dir)
        decision = (worth.get("decision") or "").strip().lower()
        if decision not in MACHINE_WORTH_VALUES:
            without_worth += 1
            continue

        keys = row_keys_for_person(rows, person_id)
        if not include_human_rows and has_human_worth(rows, person_id, parent_ids):
            skipped_human += 1
            continue

        if not keys:
            key = person_id.lower()
            rows[key] = {column: "" for column in OVERRIDE_COLUMNS}
            rows[key]["public_identifier"] = person_id
            rows[key]["person_id"] = person_id
            keys = [key]

        for key in keys:
            row = rows[key]
            row["llm_worth"] = decision
            row["llm_worth_reason"] = str(worth.get("reason") or "")
            row["person_id"] = row.get("person_id") or person_id
            row["source"] = row.get("source") or "deep-context-synthesis"
            row["updated_at"] = now_iso()
            # Retire only the old spam-screen value. llm_reject=yes/no can still
            # describe a proposed LinkedIn profile and is identity state.
            if (row.get("llm_reject") or "").strip().lower() == "spam":
                row["llm_reject"] = ""
                row["llm_reject_confidence"] = ""
                row["llm_reject_reason"] = ""
                cleared_legacy_spam += 1
            synced_rows += 1
        synced_people += 1

    write_override_rows(review_path, rows)
    return {
        "path": str(review_path),
        "synced_people": synced_people,
        "synced_rows": synced_rows,
        "skipped_human": skipped_human,
        "without_worth": without_worth,
        "cleared_legacy_spam": cleared_legacy_spam,
        "total_rows": len(rows),
    }
