"""Paid-cache selection, bundle loading, and authoritative orphan pruning."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from packs.ingestion.primitives.deep_context.candidates import llm_network_worth
from packs.ingestion.primitives.deep_context.common import (
    load_owner,
    owner_background_block,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.synthesis.prompting import (
    OWNER_PROMPT_SUFFIX,
    SYNTHESIS_VERSION,
    SYSTEM_PROMPT,
    owner_identity_block,
)


@dataclass(frozen=True)
class SynthesisPlan:
    owner: dict[str, Any] | None
    system_prompt: str
    paths: list[Path]


def facts_version(path: Path) -> str:
    try:
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError):
        return ""
    return str(records[-1].get("synthesis_version") or "") if records else ""


def pending_target_paths(
    raw_dir: Path,
    facts_dir: Path,
    *,
    force: bool,
    person_id: str,
    rejudge: bool = False,
    human_worth_person_ids: set[str] | None = None,
) -> list[Path]:
    paths: list[Path] = []
    human_worth = human_worth_person_ids or set()
    for path in sorted(raw_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        pid = path.stem
        if person_id and pid != person_id:
            continue
        if not force and not rejudge:
            if facts_version(facts_dir / f"{pid}.jsonl") != SYNTHESIS_VERSION:
                paths.append(path)
                continue
            if pid in human_worth:
                continue
            if llm_network_worth(pid, facts_dir).get("decision", "") in {"yes", "no"}:
                continue
        paths.append(path)
    return paths


def build_plan(
    db: Db,
    raw_dir: Path,
    facts_dir: Path,
    *,
    no_owner: bool,
    force: bool,
    rejudge: bool,
    person_id: str,
) -> SynthesisPlan:
    owner = load_owner() if not no_owner else None
    system_prompt = SYSTEM_PROMPT + (
        owner_identity_block(owner) + OWNER_PROMPT_SUFFIX + owner_background_block(owner)
        if owner else ""
    )
    snapshot = canonical_snapshot(db)
    human_parents = {row.parent_id for row in snapshot.parents if row.human_worth is not None}
    human_people = {row.person_id for row in snapshot.people if row.parent_id in human_parents}
    return SynthesisPlan(owner, system_prompt, pending_target_paths(
        raw_dir,
        facts_dir,
        force=force,
        rejudge=rejudge,
        person_id=person_id,
        human_worth_person_ids=human_people,
    ))


def load_bundle(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def prune_orphan_facts(raw_dir: Path, facts_dir: Path, *, scoped: bool, dry_run: bool) -> int:
    if scoped or dry_run:
        return 0
    try:
        manifest = json.loads((raw_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if manifest.get("status") != "completed":
        return 0
    current_ids = {
        path.stem for path in raw_dir.glob("*.json") if path.name != "manifest.json"
    }
    removed = 0
    for facts_path in facts_dir.glob("*.jsonl"):
        if facts_path.stem not in current_ids:
            facts_path.unlink()
            removed += 1
    return removed


def chunked(sequence: list[Any], size: int) -> Iterator[list[Any]]:
    for index in range(0, len(sequence), max(1, size)):
        yield sequence[index:index + size]
