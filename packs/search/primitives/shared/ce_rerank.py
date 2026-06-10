"""Cross-encoder reranking for company semantic search results.

Scores each company against the original query using an LLM pointwise relevance
judgment. Batches multiple companies per API call to minimize round trips.

Usage:
    from ce_rerank import ce_rerank_companies
    scored = await ce_rerank_companies(query, companies, top_n=200)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import openai

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("CE_RERANK_MODEL", "gpt-4.1-nano")
DEFAULT_BATCH_SIZE = 20
DEFAULT_CONCURRENCY = 10
DEFAULT_TOP_N = 500

CE_SYSTEM_PROMPT = """You are a company relevance scorer for a people search engine.

Given a search query describing a type of company, score each company on how well
it matches. Return a JSON object mapping company_id to an integer score 0-10.

Scoring guide:
- 10: Perfect match. Company clearly operates in the described domain.
- 7-9: Strong match. Core business aligns well with the query.
- 4-6: Partial match. Some overlap but not the primary business.
- 1-3: Weak match. Tangentially related at best.
- 0: No match. Company has nothing to do with the query.

Be strict. "Database companies" means companies whose PRIMARY product is a
database, data storage engine, or database infrastructure — not every company
that uses databases. Similarly, "fintech" means financial technology companies,
not every company with a payments page.

Return ONLY a JSON object: {"company_id": score, ...}
No commentary, no markdown."""


def _build_user_prompt(query: str, companies: list[dict[str, Any]]) -> str:
    lines = [f"Query: {query}", "", "Companies:"]
    for c in companies:
        cid = c.get("id", "")
        name = c.get("company_name", "unknown")
        desc = c.get("description") or c.get("entity_sector_text") or ""
        sectors = c.get("sector_types") or []
        # Truncate description to ~200 chars
        if len(desc) > 200:
            desc = desc[:200] + "..."
        sector_str = f" [{', '.join(sectors)}]" if sectors else ""
        lines.append(f"- {cid}: {name}{sector_str} — {desc}" if desc else f"- {cid}: {name}{sector_str}")
    return "\n".join(lines)


async def _score_batch(
    client: openai.AsyncOpenAI,
    query: str,
    companies: list[dict[str, Any]],
    model: str,
) -> dict[str, int]:
    """Score a batch of companies, return {company_id: score}."""
    prompt = _build_user_prompt(query, companies)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
            timeout=30,
        )
        content = resp.choices[0].message.content or "{}"
        scores = json.loads(content)
        # Validate scores are ints 0-10
        return {
            str(k): max(0, min(10, int(v)))
            for k, v in scores.items()
            if isinstance(v, (int, float))
        }
    except Exception as e:
        logger.warning(f"CE batch failed: {e}")
        # On failure, give all companies in this batch a neutral score
        return {str(c.get("id", "")): 5 for c in companies}


async def ce_rerank_companies(
    query: str,
    companies: list[dict[str, Any]],
    *,
    top_n: int = DEFAULT_TOP_N,
    model: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    concurrency: int = DEFAULT_CONCURRENCY,
    api_key: str | None = None,
    api_base: str | None = None,
) -> dict[str, Any]:
    """Score and rerank companies by query relevance using LLM cross-encoder.

    Matches the app's CE behavior:
    - Score all companies
    - Apply mean + 1 stdev threshold (adaptive cutoff)
    - Floor: keep at least max(50, 10% of input)
    - No hard cap — let the data decide

    Args:
        query: The company semantic query (e.g. "database companies")
        companies: List of company dicts with at least 'id' and 'company_name'.
                   Optionally 'description', 'entity_sector_text', 'sector_types'.
        top_n: Fallback max if adaptive threshold keeps more than this.
        model: OpenAI model to use.
        batch_size: Companies per API call.
        concurrency: Max parallel API calls.

    Returns:
        {
            "scored_companies": [...],  # companies above threshold, sorted by CE score desc
            "total_scored": int,
            "kept": int,
            "ce_model": str,
            "elapsed_ms": int,
            "threshold": float,
            "mean_score": float,
            "std_score": float,
            "score_distribution": {"10": N, "9": N, ...}
        }
    """
    if not companies:
        return {"scored_companies": [], "total_scored": 0, "kept": 0, "ce_model": model or DEFAULT_MODEL, "elapsed_ms": 0}

    model = model or DEFAULT_MODEL
    key = api_key or os.environ.get("OPENAI_API_KEY", "")
    base = api_base or os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
    if not base.endswith("/v1"):
        base = base.rstrip("/") + "/v1"

    client = openai.AsyncOpenAI(api_key=key, base_url=base)
    semaphore = asyncio.Semaphore(concurrency)

    # Build batches
    batches = [companies[i:i + batch_size] for i in range(0, len(companies), batch_size)]

    started = time.monotonic()
    all_scores: dict[str, int] = {}

    async def run_batch(batch: list[dict[str, Any]]) -> None:
        async with semaphore:
            scores = await _score_batch(client, query, batch, model)
            all_scores.update(scores)

    await asyncio.gather(*(run_batch(b) for b in batches))
    elapsed_ms = int((time.monotonic() - started) * 1000)

    # Attach scores to companies
    for c in companies:
        c["ce_score"] = all_scores.get(str(c.get("id", "")), 5)

    # Adaptive threshold: mean + 1 stdev (matching app behavior)
    scores_list = [c["ce_score"] for c in companies]
    n = len(scores_list)
    mean_score = sum(scores_list) / n
    variance = sum((s - mean_score) ** 2 for s in scores_list) / n
    std_score = variance ** 0.5
    threshold = mean_score + std_score

    # Filter above threshold
    above = [c for c in companies if c["ce_score"] >= threshold]

    # Floor: keep at least max(50, 10% of input)
    min_results = max(50, len(companies) // 10)
    if len(above) < min_results:
        sorted_all = sorted(companies, key=lambda c: c["ce_score"], reverse=True)
        above = sorted_all[:min_results]
        logger.info(f"CE threshold too strict ({len(above)} < {min_results}), keeping top {min_results}")

    # Sort by CE score descending
    kept = sorted(above, key=lambda c: c["ce_score"], reverse=True)

    # Score distribution
    dist: dict[str, int] = {}
    for c in companies:
        s = str(c.get("ce_score", 0))
        dist[s] = dist.get(s, 0) + 1

    logger.info(
        f"CE rerank: {len(companies)} → {len(kept)} companies "
        f"(threshold={threshold:.1f}, mean={mean_score:.1f}, std={std_score:.1f}, "
        f"model={model}, {len(batches)} batches, {elapsed_ms}ms)"
    )

    return {
        "scored_companies": kept,
        "total_scored": len(companies),
        "kept": len(kept),
        "ce_model": model,
        "elapsed_ms": elapsed_ms,
        "threshold": round(threshold, 2),
        "mean_score": round(mean_score, 2),
        "std_score": round(std_score, 2),
        "score_distribution": dist,
    }
