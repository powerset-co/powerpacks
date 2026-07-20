"""Shared durable review.csv storage.

The file mixes LinkedIn identity decisions with network-worth decisions, but the
two producers remain independent:

* LinkedIn reconciliation owns action/approved/link fields.
* Message synthesis owns llm_worth/llm_worth_reason.
* The human alone owns network_worth.

Keeping the tiny CSV contract here prevents either LLM stage from becoming the
other stage's fallback writer.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import now_iso


OVERRIDE_COLUMNS = [
    "public_identifier",
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
]

HUMAN_WORTH_VALUES = {"yes", "no"}
MACHINE_WORTH_VALUES = {"yes", "maybe", "no"}
USER_APPROVED = {"yes", "no"}


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


def has_human_worth(rows: dict[str, dict[str, str]], person_id: str) -> bool:
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
    # Local import avoids making the basic CSV contract depend on dossier parsing.
    from packs.ingestion.primitives.deep_context.candidates import llm_network_worth

    rows = load_override_rows(review_path)
    synced_people = synced_rows = skipped_human = without_worth = cleared_legacy_spam = 0

    for facts_path in sorted(facts_dir.glob("*.jsonl")):
        person_id = facts_path.stem
        worth = llm_network_worth(person_id, facts_dir)
        decision = (worth.get("decision") or "").strip().lower()
        if decision not in MACHINE_WORTH_VALUES:
            without_worth += 1
            continue

        keys = row_keys_for_person(rows, person_id)
        if not include_human_rows and any(
            (rows[key].get("network_worth") or "").strip().lower() in HUMAN_WORTH_VALUES
            for key in keys
        ):
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
