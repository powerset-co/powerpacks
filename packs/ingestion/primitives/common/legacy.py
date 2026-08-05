#!/usr/bin/env python3
"""Cope-with-old-installs scrubs — the ONE module allowed to know legacy shapes.

Each stage calls its scrub as the first line of `execute()`; everything after
that call may assume current shapes. No other module may read or write a legacy
artifact — that prohibition is what entitles the stage's boundary parsers to be
strict.

Every entry is dated and carries a removal condition: a legacy scrub is a
countdown, not a fixture. When the condition is met, delete the line. All
scrubs are idempotent and cheap — a no-op on a current install, safe to run
every time.

Changelog:
  2026-08-05: deep-context — `resolve_stored_identity_policy` rule (5): a
    group with no standing identity and no human touch auto-applies its single
    judge-confirmed retarget proposal at/above the detach bar (0.85) — batch
    recovery converges without a human click per obviously-right find.
  2026-08-04: deep-context — `resolve_stored_identity_policy` rule (4): parents
    half-decided by the pre-v1.15.3 /decide (one human answer settled only the
    clicked row) get their remaining pending candidate rows settled the way the
    live sibling fan-out does (detach + synthetic approve gates to no).
  2026-08-04: deep-context — `resolve_stored_identity_policy`: review.csv
    rows written under the pre-decisive judge-apply policy get the 2026-08
    promotions/demotions (decisive confirm wins its group; a punt on an
    already-settled identity detaches) without a re-judge.
  2026-07-31: deep-context — `ensure_owner_phones`: owner.json predating the
    phones field gets the owner's own numbers harvested from chat.db account
    metadata, so the contact-identifier policy can drop them.
  2026-07-28 (created): collected the gmail import's inline legacy unlinks
    (`ledger.json`, `candidates.csv`) into the one quarantine module.
  2026-07-30: deep-context section — pre-2026-07-27 parent-slug artifact
    migration and the retired `message-linkedin:` identity aliases.
  2026-07-30: messages section — pre-interaction-counts people.csv probe and
    the retired `import/messages/` artifacts scrub.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from packs.shared.csv_io import CsvIO
import csv
import json
from typing import Any
from packs.ingestion.schemas.people_schema import (
    generate_person_id,
    legacy_message_linkedin_id,
)


def scrub_gmail_import(import_dir: Path) -> None:
    """Upgrade an old install's gmail import dir in place.

    2026-07-23 ledger era — remove once no install predates powerpacks-v1.0.0.
    2026-07-25 candidates.csv fold-in (#339) — remove once no install predates
    powerpacks-v1.2.1; the candidate pool merges into people.csv now, so the
    file has no writer and a stale copy would shadow the folded rows.
    """
    (import_dir / "ledger.json").unlink(missing_ok=True)
    (import_dir / "candidates.csv").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

# Files and directories in `import/messages/` that older Powerpacks versions
# wrote and nothing reads today. Each entry carries the version that orphaned it
# and the condition for deleting the entry from this list.
#
#   people.input.csv  2026-07-23 — the review-era import input. Its producer went
#                     with the in-import research/review flow retired in #315.
#                     DELETE this entry once no supported install can predate
#                     #315 (i.e. once a fresh-install-only floor is declared).
#   enrichment/       2026-07-23 — the review-era per-run enrichment scratch dir,
#                     retired with the same flow. Same removal condition.
#   candidates.csv    2026-07-26 — the separate research-candidate pool. #339
#                     folded candidates into `people.csv`, leaving this file with
#                     a reader and no writer. DELETE this entry once no supported
#                     install can predate #339.
MESSAGES_RETIRED_IMPORT_ARTIFACTS = (
    "people.input.csv",
    "enrichment",
    "candidates.csv",
)


def scrub_messages_import_dir(import_dir: Path) -> None:
    """Delete the retired `import/messages/` artifacts listed above.

    Called from the messages import's MATERIALIZE path, not from its stage entry
    — the documented exception to this file's stage-entry rule. The no-op gate
    promises that a current run writes nothing; scrubbing at stage entry would
    make a "nothing to do" run mutate the import dir anyway. Materialize is the
    first point at which the stage is already rewriting this directory.
    """
    for name in MESSAGES_RETIRED_IMPORT_ARTIFACTS:
        target = import_dir / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)


def messages_people_csv_predates_interaction_counts(path: Path) -> bool:
    """True when an existing `import/messages/people.csv` was written before the
    interaction-count columns existed (2026-07-23).

    The messages import's fingerprint no-op cannot catch this: the CODE changed,
    not the input data, so the fingerprints still match and the stage would keep
    serving a people.csv missing `interaction_counts`. The import calls this
    first in `execute()` and self-invalidates instead of trusting its manifest.

    DELETE this entry once no supported install can carry a people.csv written
    before that column landed.
    """
    if not path.exists():
        return False
    with path.open(newline="", encoding="utf-8") as handle:
        header = next(CsvIO.reader(handle), [])
    return bool(header) and "interaction_counts" not in header


def parent_slug_migrations(
    old_parents: dict[str, dict[str, Any]],
    new_parents: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Exact old-slug -> new-slug mapping for unchanged canonical parent IDs."""
    old_by_id = {
        str(parent.get("parent_id") or "").strip().lower(): slug
        for slug, parent in old_parents.items()
        if str(parent.get("parent_id") or "").strip()
    }
    new_by_id = {
        str(parent.get("parent_id") or "").strip().lower(): slug
        for slug, parent in new_parents.items()
        if str(parent.get("parent_id") or "").strip()
    }
    return {
        old_by_id[parent_id]: new_slug
        for parent_id, new_slug in new_by_id.items()
        if parent_id in old_by_id and old_by_id[parent_id] != new_slug
    }


def _rewrite_parent_slug_csv(
    path: Path,
    migrations: dict[str, str],
    fields: tuple[str, ...],
) -> int:
    if not path.exists() or not migrations:
        return 0
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if not fieldnames or not any(field in fieldnames for field in fields):
        return 0
    changed = 0
    for row in rows:
        row_changed = False
        for field in fields:
            old = str(row.get(field) or "").strip()
            if old in migrations:
                row[field] = migrations[old]
                row_changed = True
        changed += row_changed
    if not changed:
        return 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)
    return changed


def _rewrite_parent_slug_jsonl(
    path: Path,
    migrations: dict[str, str],
) -> int:
    if not path.exists() or not migrations:
        return 0
    records: list[dict[str, Any]] = []
    changed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        old = str(record.get("parent_slug") or "").strip()
        if old in migrations:
            record["parent_slug"] = migrations[old]
            changed += 1
        records.append(record)
    if not changed:
        return 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return changed


def migrate_parent_slug_artifacts(
    migrations: dict[str, str],
    *,
    deep_research_dir: Path,
    verdicts_jsonl: Path,
    verdicts_csv: Path,
    applied_csv: Path,
    synthetic_people_csv: Path,
) -> dict[str, int]:
    """Rewrite exact parent-slug references without touching paid result bodies.

    Every path is explicit: this module sits under the stages and never reads
    their constants. `build_parents` passes its own resolved locations.
    """
    directories_renamed = directory_conflicts = 0
    for old_slug, new_slug in sorted(migrations.items()):
        old_dir = deep_research_dir / old_slug
        new_dir = deep_research_dir / new_slug
        if not old_dir.exists():
            continue
        if new_dir.exists():
            directory_conflicts += 1
            continue
        old_dir.rename(new_dir)
        directories_renamed += 1

    csv_rows_rewritten = 0
    csv_rows_rewritten += _rewrite_parent_slug_csv(
        deep_research_dir / "research_queue.csv",
        migrations,
        ("handle", "source_parent_slug"),
    )
    csv_rows_rewritten += _rewrite_parent_slug_csv(
        verdicts_csv, migrations, ("parent_slug",)
    )
    csv_rows_rewritten += _rewrite_parent_slug_csv(
        applied_csv, migrations, ("parent_slug",)
    )
    csv_rows_rewritten += _rewrite_parent_slug_csv(
        synthetic_people_csv, migrations, ("source_parent_slug",)
    )
    return {
        "keys": len(migrations),
        "directories_renamed": directories_renamed,
        "directory_conflicts": directory_conflicts,
        "csv_rows_rewritten": csv_rows_rewritten,
        "jsonl_rows_rewritten": _rewrite_parent_slug_jsonl(
            verdicts_jsonl, migrations
        ),
    }


# -----------------------------------------------------------------------------
# Retired message-linkedin identity aliases
#
# Retired before 2026-07-19. The messages import used to mint
# `message-linkedin:<sha16(pub)>` for a LinkedIn-matched contact before its
# durable directory id existed, then a later run silently re-keyed the contact —
# stranding facts under the retired key as a floating twin of the real person.
# BOTH keys are pure functions of the pub (retired: sha16; durable: the
# directory UUIDv5), so any review row naming the pub yields the EXACT
# equivalence. This is a key migration, not a guess.
#
# `worth_view` calls this at load, so its grouping only ever sees one identity
# per human.
#
# REMOVAL CONDITION: delete once no `facts/*.jsonl` file remains under a
# `MESSAGE_LINKEDIN_PREFIX` person id — the live import can no longer mint the
# prefix, so the population only shrinks.
# -----------------------------------------------------------------------------

MESSAGE_LINKEDIN_PREFIX = "message-linkedin:"


def message_linkedin_aliases(rows: list[dict[str, str]]) -> dict[str, str]:
    """Retired message-linkedin pid (lower) -> the same human's durable person_id.

    Entries for pubs with no stranded facts are inert.
    """
    aliases: dict[str, str] = {}
    for row in rows:
        pub = str(row.get("public_identifier") or "").strip().lower()
        # real LinkedIn pubs only — review keys can also be person-id-shaped
        # (candidate:phone:..., synth-...) and those never minted a legacy id
        if not pub or ":" in pub or pub.startswith("synth-"):
            continue
        aliases[legacy_message_linkedin_id(pub)] = generate_person_id(pub)
    return aliases


# -----------------------------------------------------------------------------
# owner.json without a "phones" field
#
# Predates contact-info-identifiers-v2 (2026-07-31). Without the owner's own
# numbers, the contact-identifier policy cannot drop them, and group-chat
# channel metadata can attribute the owner's own iMessage number to a contact's
# Contact row. Harvest once from chat.db account metadata and stamp the key
# (possibly empty); build_owner writes it on any later rebuild.
#
# REMOVAL CONDITION: delete once no install predates powerpacks v1.6.0.
# -----------------------------------------------------------------------------


def ensure_owner_phones(owner_json: Path) -> bool:
    """Fill a missing OR empty "phones" key on owner.json from chat.db account
    metadata. An EMPTY key re-harvests (cheap, ~ms) so an install synced later
    still self-heals; a populated key is never touched. Returns True when the
    file was rewritten."""
    from packs.ingestion.primitives.deep_context.build_owner import harvest_owner_phones
    if not owner_json.exists():
        return False
    try:
        owner = json.loads(owner_json.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(owner, dict) or owner.get("phones"):
        return False
    phones = harvest_owner_phones()
    if not phones and "phones" in owner:
        return False  # nothing found and the shape is already current
    owner["phones"] = phones
    owner_json.write_text(json.dumps(owner, indent=2) + "\n", encoding="utf-8")
    return True


# -----------------------------------------------------------------------------
# deep-context: 2026-08 identity-resolution policy over STORED review.csv rows.
# The judge-apply pass gained two deterministic rules (PR #413): a DECISIVE
# confirm (>= 0.95, the group's only bar-clearing confirm) wins its conflict
# group outright, and a pending punt on a person who already carries an APPLIED
# identity is superseded. Both are pure functions of fields the rows already
# store (action/approved/confidence), so old stores get the same outcomes here
# at review entry — no re-judge, no spend.
# REMOVAL CONDITION: delete once no supported install predates the release
# carrying PR #413.
#
# Rule (4), 2026-08-04: HALF-DECIDED parents. Before v1.15.3, a /decide settled
# only the clicked row, so a parent with several candidate rows (LinkedIn links
# plus folded synthetic options) kept its other rows pending and re-entered the
# review queue already answered. v1.15.3's /decide fans a human answer out to
# every pending sibling; this rule applies the same fan-out once to rows a
# human decided on OLD code. Pure function of stored fields (action/approved/
# source) — no re-judge, no spend.
# REMOVAL CONDITION for rule (4): delete once no supported install carries
# review rows decided before powerpacks v1.15.3 (post-1.15.3 /decide can no
# longer create the shape, so the population only shrinks).
# -----------------------------------------------------------------------------


def resolve_stored_identity_policy(review_csv: Path, index_json: Path,
                                   people_csv: Path | None = None,
                                   synthetic_csv: Path | None = None) -> dict[str, int]:
    """Promote/demote pending identity rows to the current apply policy.

    Runs four deterministic rules, in order: (1) a ground-truth LinkedIn
    CONNECTION row auto-verifies — the user is literally connected, identity
    is not a question (a restart-review reset used to blank these to a bare
    pending row); (2) a decisive pending confirm promotes and its pending
    siblings drop; (3) a sub-decisive pending punt on a person who already
    carries an applied identity detaches. Connections run first so a freshly
    applied connection supersedes its doppelganger punts in the same pass.
    (4) a parent group holding a HUMAN identity decision (approved yes/no with
    a user-grade source — never a machine `auto`) settles its remaining pending
    candidate rows exactly like the live /decide sibling fan-out: real rows
    detach (approved=yes), pending synthetic options gate to `no` in
    synthetic-people.csv, so a legacy half-decided parent stops re-entering
    the queue. (5) a group with NO standing identity, no decisive verify, and
    no human touch auto-applies its SINGLE judge-confirmed retarget proposal
    at/above the detach bar (0.85 — re-attaching identity deserves the same
    caution as detaching; the 0.70 import-time bar only KEEPS an existing
    link).

    Rules (1)-(3) never touch: user decisions (approved yes/no), retarget rows
    (accepted ones stand, rejected ones must resurface for review), exclude
    rows, or parent-worth rows. Rule (4) additionally settles pending
    verify/detach/retarget rows — but ONLY inside a group the human already
    answered; human yes/no and machine `auto` rows are never touched, and a
    group with no human decision is never touched. Idempotent:
    promoted/demoted rows carry approved=auto, settled rows carry approved=yes
    or a yes/no synthetic gate, and all are skipped on the next pass. Returns
    {"connections": n, "promoted": n, "demoted": n, "siblings_settled": n,
    "retargets_promoted": n}.
    """
    from packs.ingestion.primitives.common.jsonio import now_iso
    from packs.ingestion.primitives.deep_context.review_store import (
        DECISIVE_CONFIRM_THRESHOLD,
        JUDGE_CONFIRM_THRESHOLD,
        JUDGE_DETACH_THRESHOLD,
        is_parent_worth_row,
        load_override_rows,
        parent_ids_by_person,
        write_override_rows,
    )
    from packs.ingestion.primitives.deep_context.review_web.model import (
        load_connection_keys,
    )

    if not review_csv.exists():
        return {"connections": 0, "promoted": 0, "demoted": 0,
                "siblings_settled": 0, "retargets_promoted": 0}
    rows = load_override_rows(review_csv)
    parent_of = parent_ids_by_person(index_json)

    connections = 0
    connection_keys = load_connection_keys(people_csv) if people_csv else set()
    if connection_keys:
        for key, row in rows.items():
            if is_parent_worth_row(row, key):
                continue
            if (row.get("approved") or "").strip().lower() in {"yes", "no", "auto"}:
                continue
            if (row.get("action") or "").strip().lower() not in {"", "verify"}:
                continue
            # Ground truth attaches to the CONNECTION'S OWN pub — never to
            # other profiles that happen to hang on the same person (a
            # doppelganger row must stay subject to the rules below).
            if key in connection_keys and (row.get("linkedin_url") or "").strip():
                row["action"], row["approved"] = "verify", "auto"
                row["updated_at"] = now_iso()
                connections += 1

    def confidence(row: dict[str, str]) -> float:
        try:
            return float(row.get("confidence") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # Group identity rows (verify/detach/retarget) by the person they belong
    # to — the current deep-context parent when the index knows one, else the
    # row's own person_id. Retargets joined the grouping for rule (5); rules
    # (2)/(3) still operate on their original verify/detach subsets.
    groups: dict[str, list[str]] = {}
    for key, row in rows.items():
        if is_parent_worth_row(row, key):
            continue
        if (row.get("action") or "").strip().lower() not in {"verify", "detach", "retarget"}:
            continue
        person_id = (row.get("person_id") or "").strip().lower()
        group = parent_of.get(person_id) or person_id or key
        groups.setdefault(group, []).append(key)

    promoted = demoted = retargets_promoted = 0
    for keys in groups.values():
        group_rows = [rows[k] for k in keys]
        applied = [r for r in group_rows
                   if (r.get("approved") or "").strip().lower() in {"yes", "auto"}
                   and (r.get("action") or "").strip().lower() == "verify"]
        pending = [r for r in group_rows if not (r.get("approved") or "").strip()]
        pending_verify = [r for r in pending
                          if (r.get("action") or "").strip().lower() == "verify"]
        decisive = [r for r in pending_verify
                    if confidence(r) >= DECISIVE_CONFIRM_THRESHOLD]
        bar_clearing = [r for r in pending_verify
                        if confidence(r) >= JUDGE_CONFIRM_THRESHOLD]
        if not applied and len(decisive) == 1 and len(bar_clearing) == 1:
            # Decisive winner: keep it, drop every other pending identity row.
            winner = decisive[0]
            winner["approved"] = "auto"
            winner["updated_at"] = now_iso()
            promoted += 1
            for row in pending:
                # A pending retarget PROPOSAL is not demoted by a decisive
                # verify (pre-rule-(5) behavior preserved: it stays a visible
                # option beside the winner).
                if row is winner or (row.get("action") or "").strip().lower() == "retarget":
                    continue
                row["action"], row["approved"] = "detach", "auto"
                row["updated_at"] = now_iso()
                demoted += 1
        elif applied:
            # Superseded punts: an applied identity already answers the
            # question; a sub-decisive pending verify re-asks it blind.
            for row in pending_verify:
                if confidence(row) >= DECISIVE_CONFIRM_THRESHOLD:
                    continue  # a decisive rival is a REAL conflict — keep human
                row["action"], row["approved"] = "detach", "auto"
                row["updated_at"] = now_iso()
                demoted += 1
        else:
            # (5) Batch-recovery promotion, 2026-08-05: with NO standing
            # identity and no decisive verify in the group, a judge-confirmed
            # found-LinkedIn (llm_reject clear) auto-applies at/above the
            # DETACH bar. The asymmetry with the import-time confirm bar
            # (0.70) is deliberate: that bar KEEPS an already-attached link,
            # which is cheap to keep — RE-ATTACHING a replacement identity
            # deserves the same caution as removing one (0.85). Below the
            # bar, and for judge-rejected proposals, the human keeps the
            # decision. Human-touched groups are skipped (rule (4) settles
            # those), and only a SINGLE bar-clearing proposal promotes — two
            # would be a genuine conflict for the human. Idempotent:
            # promoted rows carry approved=auto and drop out of `pending`.
            human_touched = any(
                (r.get("approved") or "").strip().lower() in {"yes", "no"}
                for r in group_rows)
            promotable = [
                r for r in pending
                if (r.get("action") or "").strip().lower() == "retarget"
                and (r.get("new_linkedin_url") or "").strip()
                and (r.get("llm_reject") or "").strip().lower() not in {"1", "true", "yes"}
                and confidence(r) >= JUDGE_DETACH_THRESHOLD]
            if not human_touched and len(promotable) == 1:
                promotable[0]["approved"] = "auto"
                promotable[0]["updated_at"] = now_iso()
                retargets_promoted += 1
    # Rule (4): settle legacy half-decided parent groups the way the live
    # /decide sibling fan-out does. Runs LAST so it only sweeps what rules
    # (1)-(3) left pending — a v1.15.3+ install has already applied those to
    # this store, and re-ordering would silently change their outcomes.
    from packs.ingestion.primitives.deep_context.candidates import (
        candidate_identifier_key,
        current_parent_by_person_id,
        is_candidate_id,
        parent_by_candidate_identifier,
    )
    from packs.ingestion.primitives.deep_context.review_web.decisions import (
        apply_synthetic_decision,
    )
    from packs.ingestion.primitives.deep_context.review_web.model import (
        _synthetic_source_ids,
    )

    # A human identity decision is approved yes/no from a user-grade writer:
    # the review UI's /decide (`deep-context-review`) or a guided-retarget
    # submit (`user-guidance`). Old machine appliers wrote approved=yes with
    # source `deep-research` — those must NOT trigger a settle (the same
    # human-vs-machine line the shipped settle guards draw at `auto`).
    user_sources = {"deep-context-review", "user-guidance"}
    # Candidate rows only: verify/detach (judged links) and retarget (proposed
    # links). Worth-mirror rows (action='') and exclude rows are neither
    # triggers nor settle targets.
    identity_actions = {"verify", "detach", "retarget"}
    slug_of_person = current_parent_by_person_id(index_json)

    human_groups: set[str] = set()
    pending_by_group: dict[str, list[dict[str, str]]] = {}
    for key, row in rows.items():
        if is_parent_worth_row(row, key):
            continue
        if (row.get("action") or "").strip().lower() not in identity_actions:
            continue
        person_id = (row.get("person_id") or "").strip().lower()
        group = slug_of_person.get(person_id) or person_id or key
        approved = (row.get("approved") or "").strip().lower()
        if approved in {"yes", "no"}:
            if (row.get("source") or "").strip().lower() in user_sources:
                human_groups.add(group)
        elif approved != "auto":
            pending_by_group.setdefault(group, []).append(row)

    siblings_settled = 0
    for group in human_groups:
        for row in pending_by_group.get(group, []):
            # Same write as the live fan-out's sibling withdrawal (a
            # link-level No, never a person reject), with a scrub-owned
            # source so the repair stays auditable.
            row.update({"action": "detach", "approved": "yes",
                        "new_linkedin_url": "", "new_public_identifier": "",
                        "source": "legacy-sibling-settle",
                        "updated_at": now_iso()})
            siblings_settled += 1

    if synthetic_csv is not None and synthetic_csv.exists() and human_groups:
        # A folded synthetic option's gate lives in synthetic-people.csv. Its
        # owning group mirrors the review UI's fold: the row's source person
        # ids via the index's child->parent membership, and — for candidate:*
        # ids — the real parent that already owns the candidate's identifier.
        ident_owner = parent_by_candidate_identifier(index_json)
        with synthetic_csv.open(newline="", encoding="utf-8") as fh:
            synth_rows = list(csv.DictReader(fh))
        for synth in synth_rows:
            pub = (synth.get("public_identifier") or "").strip().lower()
            if not pub.startswith("synth-"):
                continue
            if (synth.get("approved") or "").strip().lower() in {"yes", "no"}:
                continue  # the user already gated it — their word stands
            source_ids = (_synthetic_source_ids(synth.get("source_person_ids") or "")
                          or [str(synth.get("id") or "") or pub])
            synth_groups: set[str] = set()
            for pid in source_ids:
                pid = pid.strip().lower()
                slug = slug_of_person.get(pid)
                if not slug and is_candidate_id(pid):
                    slug = ident_owner.get(candidate_identifier_key(pid))
                synth_groups.add(slug or pid)
            if synth_groups & human_groups:
                apply_synthetic_decision(synthetic_csv, pub, "detach")
                siblings_settled += 1

    if connections or promoted or demoted or siblings_settled or retargets_promoted:
        write_override_rows(review_csv, rows)
    return {"connections": connections, "promoted": promoted,
            "demoted": demoted, "siblings_settled": siblings_settled,
            "retargets_promoted": retargets_promoted}
