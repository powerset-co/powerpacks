"""Heal stale LinkedIn identities from SQLite before the review UI serves.

Fresh-hydrate judge-skipped links, reuse the shared judge, detach confirmed dead
links, stand an existing synthetic identity, and stamp the fixed review manifest.

Flow: SQLite selection -> RapidAPI hydrate -> shared judge -> SQLite settlement
-> fixed manifest.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB, PROFILE_CACHE_DIR, REVIEW_MANIFEST, emit, load_env,
)
from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.deep_context.db.models import (
    JUDGE_CONFIRM_THRESHOLD,
    JUDGE_DETACH_THRESHOLD,
    RowKind,
)
from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_review
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot, identity_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.identity_evidence import (
    NO_PROFILE_REASON,
    decide_actions,
    judge_batch,
)
from packs.ingestion.primitives.deep_context.identity_reconcile.queue import (
    build_tasks,
    hydrate_projected_profiles,
)
from packs.ingestion.primitives.deep_context.identity_reconcile.results import (
    write_overrides,
)
from packs.ingestion.primitives.enrich.rapidapi_client import PROFILE_CONTENT, PROFILE_EMPTY, PROFILE_ERROR
from packs.ingestion.primitives.imports.common import write_manifest


HEAL_BATCH_CAP: int | None = None
_FETCH_WORKERS = 8
@dataclass(frozen=True)
class HealCandidate:
    parent_id: str
    parent_slug: str
    name: str
    candidate_key: str
    pub: str
    url: str


def _say(line: str) -> None:
    print(f"[heal] {line}", file=sys.stderr, flush=True)


class HealReview:
    """Construct and run the SQLite-backed self-heal policy."""

    def __init__(self, *, db: Db, profile_cache_dir: Path | None = None,
                 review_manifest: Path | None = None,
                 cap: int | None = HEAL_BATCH_CAP) -> None:
        self.db = db
        self.profile_cache_dir = Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.review_manifest = Path(review_manifest or REVIEW_MANIFEST)
        self.cap = None if cap is None else max(1, int(cap))

    def select_candidates(self) -> tuple[list[HealCandidate], int, int]:
        graph = canonical_snapshot(self.db)
        identity = identity_snapshot(self.db)
        parents = {row.parent_id: row for row in graph.parents}
        selected: list[HealCandidate] = []
        skipped_retarget = 0
        for link in identity.links:
            if (
                link.kind in {RowKind.SYNTHETIC.value, RowKind.RESEARCH.value}
                or link.machine_judgment != "needs_review"
                or float(link.machine_confidence or 0) != 0.0
                or link.machine_reason != NO_PROFILE_REASON
                or not link.linkedin_url
                or (link.decision_approved or link.machine_approved or "").lower()
                in {"yes", "no", "auto"}
            ):
                continue
            action = link.decision_action or link.machine_action or ""
            proposed = link.replacement_public_identifier or link.machine_proposed_public_identifier
            if action == "retarget" and proposed and proposed.lower() != link.public_identifier.lower():
                skipped_retarget += 1
                continue
            parent = parents.get(link.parent_id)
            if parent is None:
                continue
            selected.append(HealCandidate(
                parent_id=link.parent_id,
                parent_slug=parent.display_slug or parent.public_identifier,
                name=parent.display_name or link.display_name or parent.public_identifier,
                candidate_key=link.row_key,
                pub=link.public_identifier.lower(),
                url=link.linkedin_url,
            ))
        selected.sort(key=lambda row: (row.parent_slug, row.candidate_key))
        uncapped = len(selected)
        if self.cap is not None:
            selected = selected[: self.cap]
        if len(selected) < uncapped:
            _say(f"cap {self.cap}: healing {len(selected)} of {uncapped}")
        return selected, skipped_retarget, uncapped

    def fetch_states(self, candidates: list[HealCandidate]) -> dict[str, dict[str, Any]]:
        if not candidates:
            return {}
        _say(f"requesting {len(candidates)} fresh LinkedIn profiles")
        results = {row.candidate_key: {"state": PROFILE_ERROR, "fetched": False}
                   for row in candidates}
        targets = [{
            "public_identifier": row.pub, "linkedin_url": row.url,
            "candidate_key": row.candidate_key, "parent_id": row.parent_id,
        } for row in candidates]

        def record(target: dict[str, str], result: dict[str, Any]) -> None:
            results[target["candidate_key"]] = result

        hydrate_projected_profiles(
            self.db, targets, self.profile_cache_dir, max_workers=_FETCH_WORKERS, fresh=True,
            on_result=record,
        )
        return results

    def rejudge(self, candidates: list[HealCandidate]) -> dict[str, Any]:
        summary = {
            "candidates": len(candidates), "parents": len({row.parent_id for row in candidates}),
            "verified": 0, "detached": 0, "pending": 0,
            "restored_pending_retargets": 0, "skipped_no_openai_key": False,
        }
        if not candidates:
            return summary
        load_env()
        if not (os.environ.get("OPENAI_API_KEY") or "").strip():
            summary["skipped_no_openai_key"] = True
            return summary

        by_key = {task["candidate_key"]: task for task in build_tasks(self.db)}
        tasks = [by_key[row.candidate_key] for row in candidates]
        verdicts = judge_batch(
            tasks, use_llm=True, owner_block="", model=DEFAULT_MODEL, effort="high",
            concurrency=_FETCH_WORKERS, timeout=120, max_retries=6,
        )
        for task, result in zip(tasks, verdicts):
            task["verdict"] = result.get("verdict") or {}
        decide_actions(tasks, JUDGE_CONFIRM_THRESHOLD, JUDGE_DETACH_THRESHOLD)
        projected = write_overrides(self.db, tasks)
        summary.update(
            verified=projected["verified"],
            detached=projected["detached"],
            pending=projected["pending"],
        )
        return summary

    def terminate(self, candidates: list[HealCandidate]) -> dict[str, Any]:
        summary = {
            "candidates": len(candidates), "detached": 0, "stood_synthetic": 0,
            "minted_synthetic": 0, "pending_reresearch": 0,
            "skipped_human_decided": 0, "assemble": None,
        }
        if not candidates:
            return summary
        snapshot = identity_snapshot(self.db)
        synthetic_by_parent = {
            link.parent_id: link
            for link in snapshot.links
            if link.kind == RowKind.SYNTHETIC.value
        }
        tasks = []
        for candidate in candidates:
            tasks.append({"candidate_key": candidate.candidate_key, "action": "detach",
                "verdict": {
                    "verdict": "wrong_person", "confidence": 1.0,
                    "reason": "fresh LinkedIn fetch returned no profile content",
                },
            })
            synthetic = synthetic_by_parent.get(candidate.parent_id)
            approved = (synthetic.decision_approved or synthetic.machine_approved or "") if synthetic else ""
            if synthetic and approved == "yes":
                summary["stood_synthetic"] += 1
            elif synthetic and approved not in {"no", "auto"}:
                tasks.append({"candidate_key": synthetic.row_key, "action": "confirm",
                    "verdict": {"verdict": "confirmed", "confidence": 1.0,
                                "reason": "standing synthetic identity for dead attached link"},
                })
            else:
                summary["pending_reresearch"] += 1
        projected = write_overrides(self.db, tasks)
        summary["detached"] = projected["detached"]
        summary["stood_synthetic"] += projected["verified"]
        summary["skipped_human_decided"] = projected["preserved_user_rows"]
        return summary

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        queue_before = int(linkedin_review(self.db, "progress")["pending"])
        candidates, skipped_retarget, uncapped = self.select_candidates()
        states = self.fetch_states(candidates)
        content = [row for row in candidates if states[row.candidate_key]["state"] == PROFILE_CONTENT]
        empty = [row for row in candidates if states[row.candidate_key]["state"] == PROFILE_EMPTY
                 and states[row.candidate_key].get("fetched")]
        empty_unfetched = sum(
            states[row.candidate_key]["state"] == PROFILE_EMPTY
            and not states[row.candidate_key].get("fetched")
            for row in candidates
        )
        rejudge = self.rejudge(content)
        terminated = self.terminate(empty)
        summary = {
            "primitive": "heal_review", "status": "completed",
            "owner_phones_backfilled": False, "legacy_scrub": {},
            "queue_pending_before": queue_before,
            "queue_pending_after": int(linkedin_review(self.db, "progress")["pending"]),
            "candidates": len(candidates), "candidates_uncapped": uncapped,
            "capped": len(candidates) < uncapped, "cap": self.cap,
            "skipped_pending_retarget": skipped_retarget,
            "profiles": {
                "content": len(content), "empty_fetched": len(empty),
                "empty_unfetched": empty_unfetched,
                "error": len(candidates) - len(content) - len(empty) - empty_unfetched,
                "fetched": sum(bool(row.get("fetched")) for row in states.values()),
                "from_cache": sum(bool(row.get("from_cache")) for row in states.values()),
            },
            "rejudge": rejudge, "terminated": terminated,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        write_manifest(
            self.review_manifest.parent.name,
            {"heal": summary},
            import_dir=self.review_manifest.parent.parent,
        )
        tail = " (nothing to do)" if not candidates else ""
        judged = 0 if rejudge["skipped_no_openai_key"] else rejudge["candidates"]
        _say(f"fetched {summary['profiles']['fetched']} · judged {judged} · "
             f"dead-links {terminated['detached']}{tail}")
        return summary

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Heal stale LinkedIn review candidates")
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--cap", type=int, default=HEAL_BATCH_CAP)
    parser.add_argument("--pre-restart", action="store_true")
    args = parser.parse_args(argv)
    emit(HealReview(db=Db(Path(args.db)), cap=args.cap).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
