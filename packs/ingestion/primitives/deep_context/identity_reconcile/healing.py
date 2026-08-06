"""Selection, hydration, judging, and settlement policy for identity healing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, cast

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.deep_context import identity_evidence, profile_projection
from packs.ingestion.primitives.deep_context.common import load_env
from packs.ingestion.primitives.deep_context.db.identity_views import (
    HealIdentityQueueRow,
    linkedin_review,
)
from packs.ingestion.primitives.deep_context.db.models import (
    JUDGE_CONFIRM_THRESHOLD,
    JUDGE_DETACH_THRESHOLD,
    IdentityOrigin,
    ReviewSource,
    RowKind,
)
from packs.ingestion.primitives.deep_context.db.snapshots import (
    canonical_snapshot,
    identity_snapshot,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier_evidence import (
    DossierEvidence,
    owner_background,
)
from packs.ingestion.primitives.deep_context.identity_reconcile.queue import build_tasks
from packs.ingestion.primitives.deep_context.identity_reconcile import judgment_policy
from packs.ingestion.primitives.deep_context.identity_reconcile.results import write_overrides


def select_candidates(
    db: Db,
    cap: int | None,
    candidate_factory: Callable[..., Any],
    say: Callable[[str], None],
) -> tuple[list[Any], int, int]:
    rows = cast(
        list[HealIdentityQueueRow],
        linkedin_review(
            db,
            "heal",
            no_profile_reason=judgment_policy.NO_PROFILE_REASON,
        ),
    )
    skipped_retarget = sum(row.selection == "pending_retarget" for row in rows)
    selected = []
    for row in rows:
        if row.selection != "candidate":
            continue
        selected.append(candidate_factory(
            parent_id=row.parent_id,
            parent_slug=row.parent_slug,
            name=row.name,
            candidate_key=row.candidate_key,
            pub=row.public_identifier,
            url=row.linkedin_url,
        ))
    uncapped = len(selected)
    if cap is not None:
        selected = selected[:cap]
    if len(selected) < uncapped:
        say(f"cap {cap}: healing {len(selected)} of {uncapped}")
    return selected, skipped_retarget, uncapped


def fetch_states(
    db: Db,
    candidates: list[Any],
    cache_dir: Path,
    *,
    max_workers: int,
    say: Callable[[str], None],
) -> dict[str, dict[str, Any]]:
    if not candidates:
        return {}
    say(f"requesting {len(candidates)} fresh LinkedIn profiles")
    results = {
        row.candidate_key: {
            "state": profile_projection.PROFILE_ERROR,
            "fetched": False,
        }
        for row in candidates
    }
    targets = [{
        "public_identifier": row.pub,
        "linkedin_url": row.url,
        "candidate_key": row.candidate_key,
        "parent_id": row.parent_id,
    } for row in candidates]

    def record(target: dict[str, str], result: dict[str, Any]) -> None:
        results[target["candidate_key"]] = result

    profile_projection.hydrate_profiles(
        targets,
        cache_dir,
        db=db,
        max_workers=max_workers,
        fresh=True,
        on_result=record,
    )
    return results


def rejudge(db: Db, candidates: list[Any], *, concurrency: int) -> dict[str, Any]:
    summary = {
        "candidates": len(candidates),
        "parents": len({row.parent_id for row in candidates}),
        "verified": 0,
        "detached": 0,
        "pending": 0,
        "restored_pending_retargets": 0,
        "skipped_no_openai_key": False,
    }
    if not candidates:
        return summary
    load_env()
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        summary["skipped_no_openai_key"] = True
        return summary
    by_key = {task["candidate_key"]: task for task in build_tasks(db)}
    tasks = [by_key[row.candidate_key] for row in candidates]
    owner_block = owner_background(canonical_snapshot(db))
    verdicts = identity_evidence.judge_batch(
        tasks,
        use_llm=True,
        owner_block=owner_block,
        model=DEFAULT_MODEL,
        effort="high",
        concurrency=concurrency,
        timeout=120,
        max_retries=6,
    )
    for task, result in zip(tasks, verdicts):
        task["verdict"] = result.get("verdict") or {}
        task["judgment_fingerprint"] = str(
            result.get("fingerprint")
            or identity_evidence.task_fingerprint(task, owner_block)
        )
    actions = judgment_policy.decide_actions(
        tasks, JUDGE_CONFIRM_THRESHOLD, JUDGE_DETACH_THRESHOLD
    )
    tasks = [
        {**task, "action": action.action, "via": action.via}
        for task, action in zip(tasks, actions)
    ]
    projected = write_overrides(db, tasks, source=ReviewSource.HEAL)
    summary.update(
        verified=projected["verified"],
        detached=projected["detached"],
        pending=projected["pending"],
    )
    return summary


def terminate(db: Db, candidates: list[Any]) -> dict[str, Any]:
    summary = {
        "candidates": len(candidates),
        "detached": 0,
        "stood_synthetic": 0,
        "minted_synthetic": 0,
        "pending_reresearch": 0,
        "skipped_human_decided": 0,
        "assemble": None,
    }
    if not candidates:
        return summary
    snapshot = canonical_snapshot(db)
    owner_block = owner_background(snapshot)
    synthetic_by_parent = {
        link.parent_id: link
        for link in identity_snapshot(db).links
        if link.kind == RowKind.SYNTHETIC.value
    }
    tasks = []
    for candidate in candidates:
        task = {
            "candidate_key": candidate.candidate_key,
            "action": "detach",
            "verdict": {
                "verdict": "wrong_person",
                "confidence": 1.0,
                "reason": "fresh LinkedIn fetch returned no profile content",
            },
            "evidence": DossierEvidence.from_parent(candidate.parent_id, snapshot),
            "linkedin": {
                "linkedin_url": candidate.url,
                "full_name": candidate.name,
                "has_profile": False,
            },
        }
        task["judgment_fingerprint"] = identity_evidence.task_fingerprint(
            task, owner_block
        )
        tasks.append(task)
        synthetic = synthetic_by_parent.get(candidate.parent_id)
        approved = (
            synthetic.decision_approved or synthetic.machine_approved or ""
        ) if synthetic else ""
        if synthetic and approved == "yes":
            summary["stood_synthetic"] += 1
        elif synthetic and approved not in {"no", "auto"}:
            synthetic_task = {
                "candidate_key": synthetic.row_key,
                "action": "confirm",
                "verdict": {
                    "verdict": "confirmed",
                    "confidence": 1.0,
                    "reason": "standing synthetic identity for dead attached link",
                },
                "evidence": DossierEvidence.from_parent(
                    candidate.parent_id, snapshot
                ),
                "linkedin": {
                    "linkedin_url": synthetic.linkedin_url or "",
                    "full_name": synthetic.display_name or candidate.name,
                    "has_profile": True,
                },
            }
            synthetic_task["judgment_fingerprint"] = (
                identity_evidence.judgment_fingerprint(
                    synthetic_task["evidence"],
                    synthetic_task["linkedin"],
                    IdentityOrigin.ATTACHED,
                    owner_block,
                )
            )
            tasks.append(synthetic_task)
        else:
            summary["pending_reresearch"] += 1
    projected = write_overrides(db, tasks, source=ReviewSource.HEAL)
    summary["detached"] = projected["detached"]
    summary["stood_synthetic"] += projected["verified"]
    summary["skipped_human_decided"] = projected["preserved_user_rows"]
    return summary
