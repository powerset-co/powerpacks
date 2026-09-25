"""Project research retarget proposals into canonical identity decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context.db.models import WriterSource
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityVerdict,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.settlement import (
    MachineIdentitySettlement,
    settle_machine_identities,
)
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url


@dataclass(frozen=True)
class RetargetProposal:
    """One typed research conclusion ready for canonical identity settlement."""

    candidate_key: str
    new_linkedin_url: str
    reason: str = ""
    source: str = "deep-research"
    judge_fingerprint: str = ""
    new_public_identifier: str = ""
    approved: str = ""
    judge_payload: IdentityVerdict | None = None


def _judgment_payload_json(payload: IdentityVerdict | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(
        payload.as_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def upsert_retargets(
    db: Db,
    proposals: list[RetargetProposal],
) -> int:
    settlements = []
    proposed = 0
    for proposal in proposals:
        candidate_key = proposal.candidate_key.lower()
        new_url = normalize_linkedin_url(proposal.new_linkedin_url)
        if not candidate_key or not new_url:
            continue
        approved = proposal.approved.lower() or None
        payload = proposal.judge_payload
        settlements.append(
            MachineIdentitySettlement(
                key=candidate_key,
                judgment_fingerprint=proposal.judge_fingerprint,
                judgment_payload_json=_judgment_payload_json(payload),
                machine_action="retarget",
                machine_approved=approved,
                machine_confidence=payload.confidence if payload else None,
                machine_reason=payload.reason if payload else proposal.reason,
                machine_judgment=payload.value if payload else None,
                machine_proposed_url=new_url,
                machine_proposed_public_identifier=str(
                    proposal.new_public_identifier or extract_public_identifier(new_url)
                ).lower(),
                paid_profile=True,
                source=proposal.source or WriterSource.DEEP_RESEARCH.value,
            )
        )
        proposed += 1
    projected = settle_machine_identities(db, settlements)
    return min(proposed, len(projected))
