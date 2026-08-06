"""Copy-first parsing and historical baseline for the judge parity proof."""
from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from packs.ingestion.primitives.deep_context.common import owner_background_block
from packs.ingestion.primitives.deep_context.dossier.facts import merge_facts
from packs.ingestion.primitives.deep_context.dossier_evidence import DossierEvidence

HUMAN_APPROVALS = {"yes", "no"}
BINARY_VERDICTS = {"confirmed", "wrong_person"}


@dataclass(frozen=True)
class ReplayCase:
    """One historical row and its cached unified-judge input."""

    identifier: str
    historical: str
    human: str | None
    task: dict[str, Any]


@dataclass(frozen=True)
class InstallEvaluation:
    """Copy-first parsed inputs for one install."""

    label: str
    baseline: dict[str, int]
    replay_cases: tuple[ReplayCase, ...]
    owner_block: str


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Iterable[object] = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        values = ()
    return tuple(
        text
        for item in values
        if (text := str(item or "").strip().lower())
    )


def _identifiers(row: dict[str, Any]) -> set[str]:
    values = {
        str(row.get(field) or "").strip().lower()
        for field in ("public_identifier", "person_id", "candidate_key", "parent_slug")
    }
    values.update(_strings(row.get("person_ids")))
    values.update(_strings(row.get("worth_person_ids")))
    return values - {""}


def _display_identifier(row: dict[str, Any]) -> str:
    for field in ("candidate_key", "public_identifier", "parent_slug", "person_id"):
        if value := str(row.get(field) or "").strip().lower():
            return value
    return next(iter(_strings(row.get("person_ids"))), "unidentified")


def _human_verdict(row: dict[str, Any]) -> str | None:
    action = str(row.get("action") or "").strip().lower()
    approved = str(row.get("approved") or "").strip().lower()
    if approved not in HUMAN_APPROVALS:
        return None
    if approved == "yes":
        return "confirmed" if action == "verify" else (
            "wrong_person" if action in {"detach", "retarget"} else None
        )
    if action == "verify":
        return "wrong_person"
    if action == "detach":
        return "confirmed"
    return None


def _json_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if stripped := line.strip():
                payload = json.loads(stripped)
                if isinstance(payload, dict):
                    rows.append(payload)
    return rows


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _copy_file(source: Path, target: Path, *, required: bool = False) -> bool:
    if not source.is_file():
        if required:
            raise FileNotFoundError(source)
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def _stage_install(source: Path, target: Path) -> tuple[Path, Path, Path, Path]:
    """Copy all inputs before any parser opens them."""
    review = target / "review.csv"
    verdicts = target / "verdicts.jsonl"
    facts = target / "facts"
    owner = target / "owner.json"
    _copy_file(
        source.parent / "network-import" / "overrides" / "review.csv",
        review,
        required=True,
    )
    _copy_file(source / "reconcile" / "verdicts.jsonl", verdicts, required=True)
    _copy_file(source / "owner.json", owner)
    facts.mkdir(parents=True, exist_ok=True)
    historical = _json_rows(verdicts)
    person_ids = {
        person_id
        for row in historical
        for person_id in _strings(row.get("person_ids"))
        if Path(person_id).name == person_id
    }
    for person_id in sorted(person_ids):
        _copy_file(source / "facts" / f"{person_id}.jsonl", facts / f"{person_id}.jsonl")
    return review, verdicts, facts, owner


def _facts_evidence(
    facts_dir: Path,
    person_ids: tuple[str, ...],
    name: str,
) -> DossierEvidence:
    chunks = []
    for person_id in person_ids:
        path = facts_dir / f"{person_id}.jsonl"
        if path.is_file():
            chunks.extend(_json_rows(path))
    return DossierEvidence.from_facts(merge_facts(chunks), name=name)


def _evidence(row: dict[str, Any], facts_dir: Path) -> DossierEvidence:
    dossier = row.get("dossier")
    if isinstance(dossier, dict):
        return DossierEvidence.from_judge_dict(
            dossier,
            name=str(row.get("name") or ""),
        )
    return _facts_evidence(
        facts_dir,
        _strings(row.get("person_ids")),
        str(row.get("name") or ""),
    )


def _baseline(
    human_rows: list[dict[str, str]],
    historical: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[int, str | None]]:
    index: dict[str, set[int]] = {}
    for position, row in enumerate(historical):
        for key in _identifiers(row):
            index.setdefault(key, set()).add(position)
    counts = {
        "human_decided": 0,
        "human_no_comparator": 0,
        "historical_agree": 0,
        "historical_disagree": 0,
        "historical_abstain": 0,
        "historical_missing": 0,
        "historical_ambiguous": 0,
    }
    human_by_historical: dict[int, str | None] = {}
    for row in human_rows:
        action = str(row.get("action") or "").strip()
        approved = str(row.get("approved") or "").strip().lower()
        if not action or approved not in HUMAN_APPROVALS:
            continue
        counts["human_decided"] += 1
        expected = _human_verdict(row)
        if expected is None:
            counts["human_no_comparator"] += 1
        matches = {
            position
            for key in _identifiers(row)
            for position in index.get(key, set())
        }
        if not matches:
            counts["historical_missing"] += 1
            continue
        if len(matches) != 1:
            counts["historical_ambiguous"] += 1
            continue
        position = next(iter(matches))
        human_by_historical[position] = expected
        if expected is None:
            continue
        actual = str(
            (historical[position].get("verdict") or {}).get("verdict") or ""
        ).lower()
        if actual not in BINARY_VERDICTS:
            counts["historical_abstain"] += 1
        elif actual == expected:
            counts["historical_agree"] += 1
        else:
            counts["historical_disagree"] += 1
    return counts, human_by_historical


def load_install(source: Path, target: Path, label: str) -> InstallEvaluation:
    """Stage and parse one install without ever opening its source artifacts."""
    review_path, verdicts_path, facts_dir, owner_path = _stage_install(source, target)
    human_rows = _csv_rows(review_path)
    historical = _json_rows(verdicts_path)
    baseline, human_by_historical = _baseline(human_rows, historical)
    cases = []
    for position, row in enumerate(historical):
        profile = row.get("linkedin")
        if not isinstance(profile, dict) or not profile.get("has_profile"):
            continue
        cases.append(
            ReplayCase(
                identifier=_display_identifier(row),
                historical=str((row.get("verdict") or {}).get("verdict") or "").lower(),
                human=human_by_historical.get(position),
                task={
                    "name": str(row.get("name") or ""),
                    "evidence": _evidence(row, facts_dir),
                    "linkedin": dict(profile),
                    "research_proposal": bool(row.get("research_proposal")),
                    **(
                        {
                            "research_confidence": float(
                                row.get("research_confidence") or 0
                            ),
                            "research_unverified": bool(
                                row.get("research_unverified")
                            ),
                        }
                        if row.get("research_proposal")
                        else {}
                    ),
                },
            )
        )
    owner = _json_object(owner_path)
    return InstallEvaluation(
        label=label,
        baseline=baseline,
        replay_cases=tuple(cases),
        owner_block=owner_background_block(owner) if owner else "",
    )
