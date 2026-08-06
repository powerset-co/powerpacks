"""Select the canonical enrichment queue and render its fixed provider CSV."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    DEEP_RESEARCH_DIR,
    load_owner,
    owner_background_block,
)
from packs.ingestion.primitives.deep_context.db import views
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.parallel_research.config import (
    PROCESSOR_PRICING_USD,
)
from packs.ingestion.primitives.deep_context.parallel_research.queue import (
    filter_already_done,
)


DEFAULT_PROCESSOR = "core2x"
DR_OUT_DIR = DEEP_RESEARCH_DIR
QUEUE_CSV = DR_OUT_DIR / "research_queue.csv"
QUEUE_FIELDS = [
    "handle",
    "source_parent_slug",
    "source_person_ids",
    "source_candidate_public_identifier",
    "display_name",
    "bio",
    "known_info",
    "primary_email",
    "phone_e164",
    "area_code",
    "source_channel",
    "retarget_hint",
]
@dataclass(frozen=True)
class ResearchSelection:
    """One parsed snapshot of the SQLite queue and its paid-work estimate."""

    fingerprint: dict[str, Any]
    eligible: tuple[dict[str, Any], ...]
    queue: tuple[dict[str, str], ...]
    pending: tuple[dict[str, str], ...]
    reused_completed: int
    duplicate_handles: int
    eligible_candidates: int
    processor: str
    cost_per_person_usd: float
    estimated_usd: float

    @property
    def candidates_skipped_not_added(self) -> int:
        return 0

    def result_base(self, budget: float) -> dict[str, Any]:
        return {
            "source": "reconcile_deep_research",
            "eligible": len(self.eligible),
            "eligible_candidates": self.eligible_candidates,
            "candidates_skipped_not_added": self.candidates_skipped_not_added,
            "would_submit": len(self.pending),
            "reused_completed": self.reused_completed,
            "duplicate_handles": self.duplicate_handles,
            "processor": self.processor,
            "cost_per_person_usd": self.cost_per_person_usd,
            "estimated_usd": self.estimated_usd,
            "budget_usd": budget,
            "selection": self.fingerprint,
            "updated_at": now_iso(),
        }


def build_queue(
    subset: list[dict[str, Any]], facts_dir: Path, raw_dir: Path
) -> list[dict[str, str]]:
    owner = load_owner()
    owner_context = owner_background_block(owner) if owner else ""
    queue: list[dict[str, str]] = []
    for row in subset:
        person_ids = row.get("person_ids") or []
        email = next(
            (
                str(value)
                for value in row.get("match_emails") or []
                if "@" in str(value)
            ),
            "",
        )
        phone = next(
            (str(value) for value in row.get("match_phones") or [] if str(value)),
            "",
        )
        rejected = (row.get("linkedin") or {}).get("linkedin_url", "")
        context = ""
        if rejected:
            context = (
                f"Rejected LinkedIn: {rejected}. "
                f"Reason: {(row.get('verdict') or {}).get('reason', '')}"
            )
        if owner_context:
            context = "\n".join(
                filter(None, (context, f"Mailbox owner: {owner_context}"))
            )
        queue.append(
            {
                "handle": row.get("parent_slug", ""),
                "source_parent_slug": row.get("parent_slug", ""),
                "source_person_ids": json.dumps(person_ids, ensure_ascii=False),
                "source_candidate_public_identifier": row.get("candidate_key", ""),
                "display_name": row.get("name", ""),
                "bio": DossierEvidence.load(
                    person_ids, facts_dir, raw_dir
                ).research_bio(),
                "known_info": context,
                "primary_email": email,
                "phone_e164": phone,
                "area_code": "",
                "source_channel": "email" if email else "phone",
                "retarget_hint": "",
            }
        )
    return queue


def select_research(
    db: Db,
    *,
    facts_dir: Path,
    raw_dir: Path,
    out_dir: Path,
    processor: str,
    confirm_threshold: float,
    include_plausibly_absent: bool,
    include_candidates: bool,
) -> ResearchSelection:
    fingerprint = views.workflow_state(db)["selection"]
    fingerprint = {
        **fingerprint,
        "fingerprint": str(
            fingerprint.get("fingerprint") or fingerprint.get("sha256") or ""
        ),
    }
    eligible = views.linkedin_review(
        db,
        "enrichment",
        include_plausibly_absent=include_plausibly_absent,
        include_candidates=include_candidates,
        confirm_threshold=confirm_threshold,
    )
    queue = build_queue(eligible, facts_dir, raw_dir)
    pending, reused_completed = filter_already_done(queue, out_dir)
    duplicate_handles = max(0, len(queue) - len(pending) - reused_completed)
    cost_per = PROCESSOR_PRICING_USD.get(
        processor, PROCESSOR_PRICING_USD[DEFAULT_PROCESSOR]
    )
    return ResearchSelection(
        fingerprint=fingerprint,
        eligible=tuple(eligible),
        queue=tuple(queue),
        pending=tuple(pending),
        reused_completed=reused_completed,
        duplicate_handles=duplicate_handles,
        eligible_candidates=sum(bool(row.get("candidate_origin")) for row in eligible),
        processor=processor,
        cost_per_person_usd=cost_per,
        estimated_usd=round(len(pending) * cost_per, 2),
    )


def write_queue(path: Path, rows: tuple[dict[str, str], ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=QUEUE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
