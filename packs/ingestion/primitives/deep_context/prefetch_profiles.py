#!/usr/bin/env python3
"""Offline RapidAPI profile prefetch + LLM summary step for the Check-Profile queue.

The review UI is cache-only: it renders whatever the local profile cache holds
and never calls a provider. This stage fills that cache ahead of review — it
scans exactly the population the Check-Profile stage will render (attached /
kept links plus pending retarget proposals), diffs against the profile cache,
and fetches each miss ONCE through the same cache-first RapidAPI primitive
apply_retargets uses (the primitive writes the cache, so reruns are idempotent
and each person costs at most one paid call ever).

After a profile is cached, this stage also generates a ~2-sentence plain-English
"who is this person" summary from the CACHED PROFILE FIELDS ONLY (headline /
title / company / work history / education / location — never message bodies)
and persists it inside the same cache record as ``simple_summary``. That makes
summarization idempotent too: a rerun where every cached profile already carries
a summary makes ZERO LLM calls. The review UI reads ``simple_summary`` from the
cache at render time and shows it in the card "Summary" row in preference to the
stored judge/deep-research reason.

Default is a spend-free dry run reporting BOTH miss counts (profiles not cached,
and cached profiles with no summary) plus a combined cost estimate (RapidAPI
calls + low/high LLM cost). Pass ``--fetch`` to actually fetch-then-summarize
(``--limit N`` to cap, ``--no-llm`` to fetch without summarizing). Output is this
stage's fixed manifest — no ledgers, no run ids.

Run: uv run --project . python -m packs.ingestion.primitives.deep_context.prefetch_profiles
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from packs.indexing.lib.openai_stream import drain_pool
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.indexing.lib.openai_responses import (
    estimate_cost_usd,
    is_retryable,
    make_async_client,
    parse_json_response,
    reasoning_effort,
    responses_kwargs,
    usage_tokens,
)
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    FACTS_DIR,
    LINKEDIN_OVERRIDES_CSV,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    ROOT,
    VERDICTS_JSONL,
    emit,
    load_env,
    now_iso,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import linkedin_view
from packs.ingestion.primitives.deep_context.reconcile_review_web import (
    SYNTHETIC_PEOPLE_CSV,
    _all_review_parents,
    pending_linkedin_candidates,
)
from packs.ingestion.primitives.enrich.enrich_people import (
    profile_cache_path,
    rapidapi_key,
    rapidapi_profile,
    read_json,
    read_usable_cached_profile,
    write_json,
)
from packs.ingestion.primitives.import_contacts_pipeline.common import write_manifest
from packs.ingestion.schemas.people_schema import extract_public_identifier

STAGE = "profile-prefetch"

# Cheapest real model in packs/indexing/lib/llm_config.CHAT_MODEL_PRICES_PER_1K_USD
# (input 0.00005 / output 0.00040 per 1K) — the owner asked for "gpt-5-mini or the
# cheapest"; gpt-5-nano is present and strictly cheaper, so it is the default.
DEFAULT_SUMMARY_MODEL = "gpt-5-nano"
# Reasoning effort for a tiny extractive summary — cheapest useful setting.
DEFAULT_SUMMARY_EFFORT = "minimal"
# The generated summary is a compact string; keep the ceiling tight.
SUMMARY_MAX_OUTPUT_TOKENS = 400
# The field we persist inside the cache record (sibling to normalized_profile).
SUMMARY_FIELD = "simple_summary"
# Summaries are latency-bound per call; fan out hard (owner: 200).
DEFAULT_SUMMARY_CONCURRENCY = 200
# RapidAPI: go as fast as the plan's own limit allows — 300 requests/minute — and
# no more conservative than that. Concurrency is set wide enough to saturate that
# cap given typical per-call latency; the ONLY guard is the RPM budget below, which
# only bites if a big cohort would otherwise exceed 300/min.
RAPIDAPI_RPM_DEFAULT = 300
DEFAULT_FETCH_CONCURRENCY = 40

SUMMARY_SYSTEM = (
    "You write a neutral, factual 2-sentence description of a professional from their "
    "LinkedIn-style profile fields. State who they are: current role and company, then "
    "what they do or a notable part of their background (past roles, education, focus). "
    "Use ONLY the provided fields — never invent employers, titles, or dates. No hype, no "
    "second person, no greeting. If the provided fields are empty or too thin to say "
    "anything concrete about this specific person, return an EMPTY summary string — NEVER "
    "write generic filler like 'is a professional at a company in an unspecified role'. "
    "Return JSON: {\"summary\": \"<=2 sentences, or empty string if nothing concrete\"}."
)

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def review_queue_links(parents: list[dict[str, Any]]) -> list[dict[str, str]]:
    """One (pub, url, name) per real LinkedIn the Check-Profile queue will show.

    Mirrors the review UI's own queue: every pending identity candidate of every
    queued parent, skipping synthetic profiles (no LinkedIn to fetch) and bare
    import-candidate ids (not LinkedIn public identifiers)."""
    seen: set[str] = set()
    links: list[dict[str, str]] = []
    for parent in parents:
        for candidate in pending_linkedin_candidates(parent):
            if candidate.get("synthetic"):
                continue
            url = str(candidate.get("url") or "").strip()
            pub = (str(candidate.get("profile_pub") or "").strip().lower()
                   or extract_public_identifier(url).lower()
                   or str(candidate.get("pub") or "").strip().lower())
            if not pub or pub.startswith("candidate:") or pub in seen:
                continue
            seen.add(pub)
            links.append({
                "public_identifier": pub,
                "linkedin_url": url or f"https://www.linkedin.com/in/{pub}",
                "name": str(parent.get("name") or ""),
            })
    return links


def has_cached_profile(cache_dir: Path, pub: str) -> bool:
    """True iff a usable RapidAPI profile is on disk for this pub (per-person,
    no all-or-none assumption — works whether the cache is empty or partial)."""
    return bool(read_usable_cached_profile(profile_cache_path(cache_dir, pub)))


def _cached_summary(cache_dir: Path, pub: str) -> str:
    """The persisted ``simple_summary`` in this pub's cache record, or ''."""
    path = profile_cache_path(cache_dir, pub)
    if not path or not path.exists():
        return ""
    record = read_json(path, None)
    if not isinstance(record, dict):
        return ""
    return str(record.get(SUMMARY_FIELD) or "").strip()


def profile_is_summarizable(cache_dir: Path, pub: str) -> bool:
    """True only when the cached profile is REAL enough to summarize.

    A RapidAPI fetch can return ``normalized_profile.success = False`` with an
    ``error`` (e.g. a bad research-guess LinkedIn URL: "unrecognized linkedin
    profile payload", "not a valid LinkedIn profile"). Those cache entries have no
    substance, so feeding them to the LLM only produces hallucinated filler.

    Summarizable requires BOTH:
      1. ``normalized_profile.success`` is truthy, AND
      2. at least one substantive field is non-empty — headline, experiences,
         education, or the summary/about text.
    """
    cached = read_usable_cached_profile(profile_cache_path(cache_dir, pub))
    if not cached:
        return False
    normalized = cached.get("normalized_profile")
    if not isinstance(normalized, dict) or not normalized.get("success"):
        return False
    return bool(normalized.get("headline") or normalized.get("experiences")
                or normalized.get("education") or (normalized.get("summary") or "").strip())


def cached_but_failed(cache_dir: Path, pub: str) -> bool:
    """True when a cache FILE exists for this pub but it is a failed/empty fetch —
    i.e. a record on disk that ``read_usable_cached_profile`` rejects because
    ``normalized_profile.success`` is falsey / it carries an ``error`` / it has no
    substance. This is distinct from "uncached" (no file at all): a failed fetch
    already TRIED and produced nothing summarizable, so it is excluded from the
    summary-miss projection (the hallucination guard). It still needs a (re-)fetch,
    so it independently stays a fetch miss.
    """
    path = profile_cache_path(cache_dir, pub)
    if not path or not path.exists():
        return False
    if read_usable_cached_profile(path):
        return False  # a usable profile is not "failed"
    record = read_json(path, None)
    return isinstance(record, dict)  # a file exists but yields no usable profile


def cleanup_garbage_summaries(links: list[dict[str, str]], cache_dir: Path) -> list[str]:
    """Self-heal: strip a persisted ``simple_summary`` from any cache entry whose
    profile is NOT summarizable (failed/empty fetch). This removes generic filler
    a prior run may have written before this guard existed. Returns the cleaned pubs."""
    cleaned: list[str] = []
    for link in links:
        pub = str(link.get("public_identifier") or "").strip().lower()
        if not pub:
            continue
        if _cached_summary(cache_dir, pub) and not profile_is_summarizable(cache_dir, pub):
            _clear_summary(cache_dir, pub)
            cleaned.append(pub)
    return cleaned


def classify_queue(links: list[dict[str, str]], cache_dir: Path) -> dict[str, list[dict[str, str]]]:
    """Per-person checks over the whole review-profile queue.

    A link is a **summary miss** when it has no ``simple_summary`` AND it is not a
    cached-but-failed/empty profile. The one rule reads correctly both before and
    after the fetch:

    - Before the fetch, an UNCACHED person is a summary miss — they carry no
      summary yet and (after a successful fetch) will be summarizable, so their
      LLM cost must be projected in the dry run.
    - After the fetch, a person whose fetch FAILED is now ``cached`` but not
      summarizable, so they drop OUT of the summary-miss set and never reach the
      LLM (the hallucination guard — no "Jackson Ding is a professional at a
      company" filler for a bad-URL fetch).
    - An already-summarized person is never a miss.

    Buckets:

    - ``fetch``: links with no cached RapidAPI profile (need a fetch).
    - ``summarize``: summary misses per the rule above — every link with no
      ``simple_summary`` except cached-but-failed/empty profiles.
    - ``not_summarizable``: cached but failed/empty profiles — surfaced for counts
      and cleanup, never sent to the LLM. At execution time these are the fetches
      we must NOT feed to the LLM.
    - ``no_public_identifier``: queue rows we cannot fetch/summarize (defensive;
      ``review_queue_links`` already drops these, so normally empty).
    """
    fetch: list[dict[str, str]] = []
    summarize_links: list[dict[str, str]] = []
    not_summarizable: list[dict[str, str]] = []
    no_pub: list[dict[str, str]] = []
    for link in links:
        pub = str(link.get("public_identifier") or "").strip().lower()
        if not pub:
            no_pub.append(link)
            continue
        # Fetch decision: skip only when a USABLE profile is on disk. An uncached
        # person AND a cached-but-failed one both need a (re-)fetch.
        if not has_cached_profile(cache_dir, pub):
            fetch.append(link)
        if cached_but_failed(cache_dir, pub):
            # A failed/empty fetch already tried and produced nothing summarizable
            # → excluded from the summary-miss set (never fed to the LLM),
            # regardless of any stale garbage summary it carries.
            not_summarizable.append(link)
        elif not _cached_summary(cache_dir, pub):
            # No summary yet and NOT cached-failed → summary miss. Covers an
            # uncached person (summarizable after a successful fetch) and a real
            # cached profile that simply has not been summarized yet.
            summarize_links.append(link)
    return {"fetch": fetch, "summarize": summarize_links,
            "not_summarizable": not_summarizable, "no_public_identifier": no_pub}


def _summary_prompt(link: dict[str, str], cache_dir: Path) -> str:
    """Profile-fields-only prompt for one person (never touches message bodies)."""
    view = linkedin_view(
        {"public_identifier": link["public_identifier"],
         "linkedin_url": link.get("linkedin_url") or ""},
        cache_dir)
    lines = [f"Name: {view.get('full_name') or link.get('name') or link['public_identifier']}"]
    if view.get("headline"):
        lines.append(f"Headline: {view['headline']}")
    if view.get("location"):
        lines.append(f"Location: {view['location']}")
    if view.get("experiences"):
        lines.append("Work history:")
        lines.extend(f"- {exp}" for exp in view["experiences"])
    if view.get("education"):
        lines.append("Education:")
        lines.extend(f"- {edu}" for edu in view["education"])
    return "\n".join(lines)


def _persist_summary(cache_dir: Path, pub: str, summary: str) -> None:
    """Write ``simple_summary`` into the pub's cache record in place."""
    path = profile_cache_path(cache_dir, pub)
    if not path or not path.exists():
        return
    record = read_json(path, None)
    if not isinstance(record, dict):
        return
    record[SUMMARY_FIELD] = summary
    record["summarized_at"] = now_iso()
    write_json(path, record)


def _clear_summary(cache_dir: Path, pub: str) -> None:
    """Drop a persisted ``simple_summary`` from the pub's cache record in place
    (self-heal garbage written for a failed/empty profile)."""
    path = profile_cache_path(cache_dir, pub)
    if not path or not path.exists():
        return
    record = read_json(path, None)
    if not isinstance(record, dict) or SUMMARY_FIELD not in record:
        return
    record.pop(SUMMARY_FIELD, None)
    record.pop("summarized_at", None)
    write_json(path, record)


async def _summarize_one(client: Any, link: dict[str, str], cache_dir: Path, *,
                         model: str, effort: str, semaphore: asyncio.Semaphore,
                         max_retries: int) -> dict[str, Any]:
    kwargs = responses_kwargs(model, effort=effort, schema=SUMMARY_SCHEMA,
                              schema_name="profile_summary",
                              max_output_tokens=SUMMARY_MAX_OUTPUT_TOKENS)
    async with semaphore:
        attempt = 0
        while True:
            try:
                response = await client.responses.create(
                    model=model,
                    input=[{"role": "system", "content": SUMMARY_SYSTEM},
                           {"role": "user", "content": _summary_prompt(link, cache_dir)}],
                    **kwargs,
                )
                parsed = parse_json_response(response, "profile summary")
                return {"summary": str(parsed.get("summary") or "").strip(),
                        "usage": usage_tokens(response), "error": ""}
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if is_retryable(exc) and attempt <= max_retries:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                return {"summary": "", "usage": {"input_tokens": 0, "output_tokens": 0,
                                                 "reasoning_tokens": 0},
                        "error": f"{type(exc).__name__}: {exc}"[:200]}


def summarize(misses: list[dict[str, str]], cache_dir: Path, *, model: str,
              effort: str, concurrency: int, timeout: int,
              max_retries: int) -> dict[str, Any]:
    """Generate + persist one summary per miss (async fan-out); counts + tokens.

    Run-time guard: only profiles that are summarizable AT THIS MOMENT
    (``profile_is_summarizable`` — a successful fetch with substantive fields)
    reach the LLM. A failed/empty fetch is skipped here even if it slipped into
    the miss list, so we never hallucinate filler for a bad-URL profile.
    """
    summarizable = [link for link in misses
                    if profile_is_summarizable(
                        cache_dir, str(link.get("public_identifier") or "").strip().lower())]
    results: dict[int, dict[str, Any]] = {}

    async def driver() -> None:
        client = make_async_client(timeout=timeout)
        semaphore = asyncio.Semaphore(max(1, concurrency))

        def on_result(item: tuple[int, dict[str, Any]]) -> None:
            results[item[0]] = item[1]

        async def one(i: int, link: dict[str, str]) -> tuple[int, dict[str, Any]]:
            return i, await _summarize_one(client, link, cache_dir, model=model,
                                           effort=effort, semaphore=semaphore,
                                           max_retries=max_retries)
        try:
            await drain_pool([one(i, link) for i, link in enumerate(summarizable)], on_result)
        finally:
            await client.close()

    if summarizable:
        asyncio.run(driver())

    # skipped_empty: the model returned an empty summary (defense-in-depth — it
    # judged the fields too thin to say anything concrete). We write NOTHING; it is
    # not an error/retry failure, so it is tracked separately from ``failed``.
    # ``attempted`` counts only the run-time-summarizable population that actually
    # reached the LLM; the run-level manifest reports ``skipped_no_profile`` for the
    # failed/empty fetches guarded out here.
    counts = {"summarized": 0, "failed": 0, "skipped_empty": 0,
              "attempted": len(summarizable)}
    usage_total = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    for i, link in enumerate(summarizable):
        res = results.get(i, {"summary": "", "usage": {}, "error": "no result"})
        for key in usage_total:
            usage_total[key] += int(res.get("usage", {}).get(key, 0))
        if res.get("summary"):
            _persist_summary(cache_dir, link["public_identifier"], res["summary"])
            counts["summarized"] += 1
        elif res.get("error"):
            counts["failed"] += 1
        else:
            counts["skipped_empty"] += 1
    billed_output = usage_total["output_tokens"] + usage_total["reasoning_tokens"]
    return {"counts": counts, "tokens": usage_total,
            "actual_cost_usd": estimate_cost_usd(
                usage_total["input_tokens"], billed_output, model)}


class _RpmGate:
    """Minimal thread-safe requests-per-minute bound — the ONLY fetch throttle.

    Concurrency is otherwise unthrottled; this just blocks the (N+1)-th start
    until the oldest of the last ``rpm`` starts is a minute old, so a large cohort
    can't blow past the provider's own cap. rpm <= 0 disables it entirely."""

    def __init__(self, rpm: int) -> None:
        self._rpm = rpm
        self._lock = threading.Lock()
        self._starts: deque[float] = deque()

    def acquire(self) -> None:
        if self._rpm <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                while self._starts and now - self._starts[0] >= 60.0:
                    self._starts.popleft()
                if len(self._starts) < self._rpm:
                    self._starts.append(now)
                    return
                wait = 60.0 - (now - self._starts[0])
            time.sleep(max(0.0, wait))


def prefetch(misses: list[dict[str, str]], cache_dir: Path, api_key: str,
             *, limit: int = 0, concurrency: int = DEFAULT_FETCH_CONCURRENCY,
             rpm: int = RAPIDAPI_RPM_DEFAULT) -> dict[str, int]:
    """Fetch each miss once via the cache-first primitive (which writes the
    cache); counts only — the cache files are the durable output. Fan-out is wide
    (``concurrency``); the sole pace guard is the ``rpm`` budget (default 300 =
    the RapidAPI plan cap), which only bites for a cohort large enough to exceed it."""
    targets = misses[:limit] if limit else misses
    counts = {"fetched": 0, "from_cache": 0, "failed": 0, "attempted": len(targets)}
    if not targets:
        return counts
    gate = _RpmGate(rpm)

    def fetch_one(link: dict[str, str]) -> dict[str, Any]:
        gate.acquire()
        return rapidapi_profile(link["public_identifier"], link["linkedin_url"],
                                api_key, cache_dir=cache_dir)

    workers = max(1, min(concurrency, len(targets)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(fetch_one, targets):
            if (result.get("normalized_profile") or {}).get("success") is True:
                counts["from_cache" if result.get("from_cache") else "fetched"] += 1
            else:
                counts["failed"] += 1
    return counts


# Rough per-summary token estimate for the DRY-RUN cost band (prompt + short
# output). Actual runs report measured usage; this only sizes the estimate.
_EST_INPUT_TOKENS = 500
_EST_OUTPUT_TOKENS_LOW = 60
_EST_OUTPUT_TOKENS_HIGH = 160


def _estimated_llm_cost(count: int, model: str) -> dict[str, float]:
    low = sum(estimate_cost_usd(_EST_INPUT_TOKENS, _EST_OUTPUT_TOKENS_LOW, model)
              for _ in range(count))
    high = sum(estimate_cost_usd(_EST_INPUT_TOKENS, _EST_OUTPUT_TOKENS_HIGH, model)
               for _ in range(count))
    return {"estimated_llm_cost_usd_low": round(low, 6),
            "estimated_llm_cost_usd_high": round(high, 6)}


def _summary_concurrency(args: argparse.Namespace) -> int:
    """LLM summary fan-out: explicit --concurrency wins, else env/profile (owner
    default 200). RapidAPI stays on the separate, bounded --fetch-concurrency."""
    if args.concurrency:
        return max(1, args.concurrency)
    return env_or_profile_int("POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency",
                              fallback=DEFAULT_SUMMARY_CONCURRENCY)


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    cache_dir = Path(args.profile_cache_dir)
    parents = _all_review_parents(
        Path(args.verdicts), Path(args.review), Path(args.synthetic_people),
        Path(args.facts_dir), Path(args.people_csv),
        Path(args.parents_dir), Path(args.dossier_dir), cache_dir)
    links = review_queue_links(parents)
    # Self-heal FIRST: strip any garbage simple_summary a prior run wrote for a
    # failed/empty profile, so it never lingers in the UI. Free, local, idempotent.
    cleaned_summaries = cleanup_garbage_summaries(links, cache_dir)
    buckets = classify_queue(links, cache_dir)
    fetch_misses, summ_misses = buckets["fetch"], buckets["summarize"]
    not_summarizable, no_pub = buckets["not_summarizable"], buckets["no_public_identifier"]
    use_llm = not args.no_llm
    # The queue-wide per-person state BEFORE any work this run (owner: works
    # whether the cache is empty or partially populated — no all-or-none assumption).
    already_cached = len(links) - len(fetch_misses) - len(no_pub)
    # already_summarized = links that are already done: they carry a summary and
    # are summarizable. ``summarize`` (summary misses), ``not_summarizable``
    # (cached-failed), and ``no_pub`` are mutually exclusive and together are
    # exactly the not-yet-done set, so the remainder is the already-summarized set.
    # (``fetch`` ⊆ ``summarize`` — an uncached person is always a summary miss —
    # so it is NOT subtracted again here.)
    already_summarized = (len(links) - len(summ_misses)
                          - len(not_summarizable) - len(no_pub))
    # Summarization runs ONLY over REAL cached profiles lacking a summary. Failed/
    # empty profiles are excluded (not summarizable) — we never feed empties to the
    # LLM, so it can't hallucinate generic filler for a bad-URL fetch.
    payload: dict[str, Any] = {
        "queue_links": len(links),
        "cache_misses": len(fetch_misses),
        "summary_misses": len(summ_misses),
        "not_summarizable": len(not_summarizable),
        "already_cached": already_cached,
        "already_summarized": already_summarized,
        "no_public_identifier": len(no_pub),
        "cleaned_garbage_summaries": len(cleaned_summaries),
        "cleaned_public_identifiers": sorted(cleaned_summaries),
        "estimated_rapidapi_calls": len(fetch_misses),
        "estimated_summary_calls": len(summ_misses) if use_llm else 0,
        "missing_public_identifiers": sorted(link["public_identifier"] for link in fetch_misses),
        "summary_missing_public_identifiers": sorted(
            link["public_identifier"] for link in summ_misses),
        "not_summarizable_public_identifiers": sorted(
            link["public_identifier"] for link in not_summarizable),
        "model": args.model,
        "reasoning_effort": reasoning_effort(args.reasoning_effort),
        "summary_concurrency": _summary_concurrency(args),
        "fetch_concurrency": max(1, args.fetch_concurrency),
        "rapidapi_rpm": args.rapidapi_rpm,
        "profile_cache_dir": str(cache_dir),
        "privacy": {"message_bodies_read": False,
                    "network_called": bool(args.fetch),
                    "paid_provider_called": bool(args.fetch)},
    }
    payload.update(_estimated_llm_cost(payload["estimated_summary_calls"], args.model))

    if not args.fetch:
        payload["status"] = "dry_run"
        skipped_note = (f"; {len(not_summarizable)} failed/empty profile(s) not summarizable"
                        if not_summarizable else "")
        cleaned_note = (f"; cleaned {len(cleaned_summaries)} stale summary(ies)"
                        if cleaned_summaries else "")
        payload["note"] = (
            f"dry run: {len(fetch_misses)} fetch miss(es) would cost ~{len(fetch_misses)} "
            f"RapidAPI call(s); {payload['estimated_summary_calls']} summary miss(es) would "
            f"cost ~${payload['estimated_llm_cost_usd_low']}–{payload['estimated_llm_cost_usd_high']} "
            f"LLM{skipped_note}{cleaned_note}; rerun with --fetch to spend")
    elif not rapidapi_key():
        payload["status"] = "blocked_no_key"
        payload["privacy"]["network_called"] = False
        payload["privacy"]["paid_provider_called"] = False
        payload["note"] = "RAPIDAPI_LINKEDIN_KEY / RAPIDAPI_KEY not configured; nothing fetched"
    else:
        counts = prefetch(fetch_misses, cache_dir, rapidapi_key(),
                          limit=args.limit, concurrency=max(1, args.fetch_concurrency),
                          rpm=args.rapidapi_rpm)
        counts["already_cached"] = already_cached
        payload["counts"] = counts
        # Re-classify AFTER the fetch: a failed fetch (bad URL) now sits in the
        # not_summarizable bucket, NOT the summarize bucket — so we never hand it
        # to the LLM. --limit caps the whole run.
        post = classify_queue(links, cache_dir)
        payload["remaining_misses"] = len(post["fetch"])
        status = "completed" if not counts["failed"] else "completed_with_failures"
        pending_summary = post["summarize"]
        if args.limit:
            pending_summary = pending_summary[:max(0, args.limit - counts["attempted"])]
        # Fetch failures show up as newly non-summarizable cached entries; report
        # them so the manifest explains why some fetched people got no summary.
        skipped_no_profile = len(post["not_summarizable"])
        summary_counts = {"summarized": 0, "failed": 0, "attempted": 0,
                          "already_summarized": already_summarized,
                          "skipped_no_profile": skipped_no_profile,
                          "pending": len(pending_summary)}
        if not use_llm:
            payload["summary"] = {"status": "skipped_no_llm", "counts": summary_counts}
        elif not os.getenv("OPENAI_API_KEY"):
            payload["summary"] = {"status": "blocked_no_key", "counts": summary_counts}
            payload["privacy"]["paid_provider_called"] = True  # RapidAPI still ran
        elif pending_summary:
            result = summarize(
                pending_summary, cache_dir, model=args.model,
                effort=reasoning_effort(args.reasoning_effort),
                concurrency=_summary_concurrency(args), timeout=args.timeout,
                max_retries=args.max_retries)
            payload["summary"] = {"status": "completed",
                                  "counts": {**summary_counts, **result["counts"]},
                                  "tokens": result["tokens"],
                                  "actual_cost_usd": result["actual_cost_usd"]}
            if result["counts"]["failed"]:
                status = "completed_with_failures"
        else:
            payload["summary"] = {"status": "completed", "counts": summary_counts}
        payload["remaining_summary_misses"] = len(classify_queue(links, cache_dir)["summarize"])
        payload["status"] = status
    payload["duration_seconds"] = round(time.monotonic() - started, 2)
    manifest = write_manifest(STAGE, payload, import_dir=ROOT)
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verdicts", default=str(VERDICTS_JSONL))
    parser.add_argument("--review", default=str(LINKEDIN_OVERRIDES_CSV))
    parser.add_argument("--synthetic-people", default=str(SYNTHETIC_PEOPLE_CSV))
    parser.add_argument("--facts-dir", default=str(FACTS_DIR))
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    parser.add_argument("--parents-dir", default=str(PARENTS_DIR))
    parser.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    parser.add_argument("--profile-cache-dir", default=str(PROFILE_CACHE_DIR))
    parser.add_argument("--fetch", action="store_true",
                        help="actually fetch cache misses (spends RapidAPI credits) then "
                             "summarize; default is a spend-free dry run")
    parser.add_argument("--no-llm", action="store_true",
                        help="fetch without generating profile summaries (no OpenAI spend)")
    parser.add_argument("--model", default=DEFAULT_SUMMARY_MODEL,
                        help=f"OpenAI model for the summary (default: {DEFAULT_SUMMARY_MODEL}, "
                             "the cheapest in llm_config)")
    parser.add_argument("--reasoning-effort", default=DEFAULT_SUMMARY_EFFORT,
                        help=f"reasoning effort for the summary (default: {DEFAULT_SUMMARY_EFFORT})")
    parser.add_argument("--limit", type=int, default=0,
                        help="cap the number of fetch+summary calls (0 = all misses)")
    parser.add_argument("--concurrency", type=int, default=0,
                        help="parallel LLM summary calls (0 = env POWERPACKS_OPENAI_CONCURRENCY "
                             f"or {DEFAULT_SUMMARY_CONCURRENCY})")
    parser.add_argument("--fetch-concurrency", type=int, default=DEFAULT_FETCH_CONCURRENCY,
                        help=f"parallel RapidAPI fetches (default {DEFAULT_FETCH_CONCURRENCY}; wide "
                             "enough to saturate the RPM budget)")
    parser.add_argument("--rapidapi-rpm", type=int, default=RAPIDAPI_RPM_DEFAULT,
                        help=f"RapidAPI requests-per-minute budget — the sole fetch pace guard "
                             f"(default {RAPIDAPI_RPM_DEFAULT} = the plan cap; 0 disables it)")
    parser.add_argument("--timeout", type=int, default=120, help="per-call OpenAI timeout (s)")
    parser.add_argument("--max-retries", type=int, default=4,
                        help="retries per summary call on transient failures")
    args = parser.parse_args(argv)
    load_env()
    emit(run(args))


if __name__ == "__main__":
    main()
