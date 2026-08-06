"""Judge and project proposed LinkedIn retargets from completed research."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.ingestion.primitives.common.paths import DEFAULT_PROFILE_CACHE_DIR
from packs.ingestion.primitives.deep_context.common import FACTS_DIR, RAW_DIR
from packs.ingestion.primitives.deep_context.db import views
from packs.ingestion.primitives.deep_context.db.models import JUDGE_CONFIRM_THRESHOLD
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.identity_evidence import (
    judge_research_proposal,
    prefer_cached_profile,
    research_proposal_task,
    research_reject_fields,
)
from packs.ingestion.primitives.deep_context.identity_reconcile.queue import (
    linkedin_view,
)
from packs.ingestion.primitives.deep_context.identity_reconcile.results import (
    upsert_retargets,
)
from packs.ingestion.primitives.deep_context.research_result import ResearchResult
from packs.ingestion.primitives.enrich.rapidapi_client import hydrate_profiles
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    normalize_linkedin_url,
)


DEFAULT_JUDGE_CONCURRENCY = 128


def judge_concurrency() -> int:
    tier = env_or_profile_int(
        "POWERPACKS_OPENAI_CONCURRENCY",
        "openai_concurrency",
        fallback=DEFAULT_JUDGE_CONCURRENCY,
    )
    if (os.getenv("POWERPACKS_OPENAI_CONCURRENCY") or "").strip():
        return tier
    return min(DEFAULT_JUDGE_CONCURRENCY, tier)


def proposal_fingerprint(
    old_pub: str,
    new_url: str,
    dossier: dict[str, Any],
    profile_view: dict[str, Any],
) -> str:
    payload = json.dumps(
        {
            "old_pub": old_pub,
            "new_linkedin_url": new_url,
            "dossier": dossier,
            "profile": profile_view,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def propose_retargets_from_output(
    out_dir: Path,
    subset: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    overrides_csv: Path,
    *,
    db: Db,
    facts_dir: Path | None = None,
    raw_dir: Path | None = None,
    use_llm: bool = False,
    owner_block: str = "",
    model: str = "",
    effort: str = "medium",
    confirm_threshold: float = JUDGE_CONFIRM_THRESHOLD,
    timeout: int = 120,
    max_retries: int = 6,
    heartbeat: Callable[[int, int], None] | None = None,
    profile_cache_dir: Path | None = None,
) -> dict[str, Any]:
    """Judge fixed research outputs and project sticky retarget proposals."""
    facts_dir = facts_dir if facts_dir is not None else FACTS_DIR
    raw_dir = raw_dir if raw_dir is not None else RAW_DIR
    cache_dir = (
        Path(profile_cache_dir)
        if profile_cache_dir is not None
        else DEFAULT_PROFILE_CACHE_DIR
    )
    results = {
        str(row.get("parent_slug") or ""): ResearchResult.load(
            out_dir
            / (row.get("parent_slug") or "")
            / "01_research_parallel.json"
        )
        for row in subset
    }
    proposed = [
        (extract_public_identifier(result.linkedin_url).lower(), result.linkedin_url)
        for result in results.values()
        if result and result.linkedin_url
    ]
    if proposed:
        hydrate_profiles(proposed, cache_dir)
    del overrides_csv
    existing = views.link_decision_state(db)
    proposals: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    cached = grandfathered = 0
    for row in subset:
        handle = row.get("parent_slug", "")
        result = results.get(handle)
        if result is None:
            continue
        new_url = result.linkedin_url
        old_pub = (
            row.get("candidate_key")
            or extract_public_identifier(
                (row.get("linkedin") or {}).get("linkedin_url", "")
            )
        ).lower()
        if not new_url or not old_pub:
            continue
        person_ids = row.get("person_ids") or []
        dossier = DossierEvidence.load(
            person_ids, facts_dir, raw_dir
        ).as_judge_dict()
        profile = prefer_cached_profile(
            result.identity_profile(),
            linkedin_view({"linkedin_url": new_url}, cache_dir),
        )
        fingerprint = proposal_fingerprint(old_pub, new_url, dossier, profile)
        proposal = {
            "old_public_identifier": old_pub,
            "new_linkedin_url": new_url,
            "linkedin_url": (row.get("linkedin") or {}).get("linkedin_url", ""),
            "match_emails": row.get("match_emails") or [],
            "match_phones": row.get("match_phones") or [],
            "person_id": (person_ids or [""])[0],
            "confidence": result.confidence,
            "reason": result.reason,
            "source": "deep-research",
            "judge_fingerprint": fingerprint,
        }
        prior = existing.get(old_pub) or {}
        prior_retarget = (prior.get("action") or "").strip().lower() == "retarget"
        prior_fingerprint = (prior.get("llm_judge_fingerprint") or "").strip()
        if prior_retarget and prior_fingerprint == fingerprint:
            cached += 1
            continue
        if (
            prior_retarget
            and not prior_fingerprint
            and (prior.get("new_linkedin_url") or "").strip()
            == normalize_linkedin_url(new_url)
        ):
            grandfathered += 1
            proposals.append(proposal)
            continue
        pending.append(
            {
                "proposal": proposal,
                "task": research_proposal_task(
                    dossier,
                    profile,
                    name=row.get("name", ""),
                    match_emails=row.get("match_emails") or [],
                    match_phones=row.get("match_phones") or [],
                    confidence=result.confidence,
                    unverified=result.unverified,
                ),
            }
        )

    if pending:
        if heartbeat:
            heartbeat(0, len(pending))

        def judge_one(item: dict[str, Any]) -> dict[str, Any]:
            return judge_research_proposal(
                item["task"],
                use_llm=use_llm,
                owner_block=owner_block,
                model=model or "",
                effort=effort,
                timeout=timeout,
                max_retries=max_retries,
            )

        done = 0
        with ThreadPoolExecutor(
            max_workers=min(judge_concurrency(), len(pending))
        ) as pool:
            futures = {pool.submit(judge_one, item): item for item in pending}
            for future in as_completed(futures):
                item = futures[future]
                item["proposal"].update(
                    research_reject_fields(future.result(), confirm_threshold)
                )
                done += 1
                if heartbeat:
                    heartbeat(done, len(pending))
        proposals.extend(item["proposal"] for item in pending)

    projected = upsert_retargets(db, proposals)
    projected.update(
        {
            "judge_calls": len(pending),
            "cached_verdicts": cached,
            "grandfathered": grandfathered,
        }
    )
    return projected
