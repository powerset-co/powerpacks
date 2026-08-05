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

Every per-person decision — fetch miss, summary miss, and the not-summarizable
hallucination guard — comes from ONE parse of that person's cache record
(``read_profile_state`` -> ``CachedProfileState``) handed to ONE first-rule-wins
``classify_link``; ``classify_queue`` just buckets the queue by its verdict. The
cache is re-read on every classification pass, so the pass after the fetch sees
what the fetch just wrote.

Default is a spend-free dry run reporting BOTH miss counts (profiles not cached,
and cached profiles with no summary) plus a combined cost estimate (RapidAPI
calls + low/high LLM cost). Pass ``--fetch`` to actually fetch-then-summarize
(``--limit N`` to cap, ``--no-llm`` to fetch without summarizing). Output is this
stage's fixed manifest — no ledgers, no run ids.

Run: uv run --project . python -m packs.ingestion.primitives.deep_context.prefetch_profiles

Changelog:
  2026-07-30 (boundary parse): each pub's cache record is parsed ONCE at the
    boundary into the frozen `CachedProfileState` (`read_profile_state`), and the
    per-link decision is the single first-rule-wins `classify_link` returning a
    `QueueVerdict`. `classify_queue` became a trivial loop over that verdict and
    returns the frozen `QueueBuckets` — built once from four local lists, never
    appended to afterwards — instead of a string-keyed dict of lists.
    Replaces the four module-level predicates (`has_cached_profile`,
    `_cached_summary`, `profile_is_summarizable`, `cached_but_failed`) that each
    re-opened the same cache file for the same pub. `_summary_concurrency(args)`
    was inlined into `main()` (CLI = thin argparse over the node). No behavior
    change: same buckets and membership, same manifest fields and values, and the
    classification still re-reads the cache on every call so the post-fetch pass
    still sees the fetch.
  2026-07-27 (declared contract): `PrefetchProfiles` is a `pipeline/contract.py:Node`.
    It DECLARES the review population it scans (verdicts, review.csv, synthetic
    people, facts/parents/dossier templates, merged people.csv) and the shared
    profile cache it upserts, instead of only opening them. `run(args)` became
    `execute()`. EVERY mode still runs through the node because every mode already
    wrote this stage's manifest — the dry run included — so no path bypasses it and
    no spend moved: `--fetch` is still the only door to RapidAPI/OpenAI.
  2026-07-23 (audit dedup): now_iso import from common.jsonio instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
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
    DOSSIER_TEMPLATE,
    FACTS_DIR,
    FACTS_TEMPLATE,
    LINKEDIN_OVERRIDES_CSV,
    PARENT_TEMPLATE,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    PROFILE_CACHE_TEMPLATE,
    ROOT,
    VERDICTS_JSONL,
    emit,
    load_env,
)
from packs.ingestion.primitives.common.jsonio import now_iso, read_json, write_json
from packs.ingestion.primitives.deep_context.reconcile_linkedin import linkedin_view
from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt
from packs.ingestion.primitives.deep_context.review_web.model import (
    SYNTHETIC_PEOPLE_CSV,
    _all_review_parents,
)
from packs.ingestion.primitives.deep_context.review_web.workflow import (
    pending_linkedin_candidates,
)
from packs.ingestion.primitives.enrich.profile_cache import (
    profile_cache_path,
    read_usable_cached_profile,
)
from packs.ingestion.primitives.enrich.rapidapi_client import rapidapi_key, rapidapi_profile
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest
from packs.ingestion.schemas.people_schema import extract_public_identifier
from pydantic import BaseModel

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

SUMMARY_SYSTEM = load_prompt("profile_summary_system")

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


def _link_pub(link: dict[str, str]) -> str:
    """A queue link's cache key: its lowercased public identifier, '' if it has none."""
    return str(link.get("public_identifier") or "").strip().lower()


@dataclass(frozen=True)
class CachedProfileState:
    """One pub's profile-cache record, parsed ONCE at the boundary.

    Every downstream decision (fetch miss, summary miss, hallucination guard,
    stale-summary cleanup) reads these typed fields instead of re-opening the same
    file behind another predicate.

    - ``exists``: a cache FILE is on disk AND it parses to a JSON object. A file
      that is missing or is not an object is indistinguishable from "never
      fetched": both need a fetch, and neither is an attempt that already failed.
    - ``usable``: ``read_usable_cached_profile`` accepts the record — a successful
      fetch, including a legacy raw payload it re-normalizes on the fly. Anything
      else needs a (re-)fetch, per-person: no all-or-none assumption, so this works
      whether the cache is empty or partial.
    - ``summarizable``: the usable profile is REAL enough to summarize —
      ``normalized_profile.success`` is truthy AND at least one substantive field
      is non-empty (headline, experiences, education, or the summary/about text).
      A RapidAPI call can return ``success = False`` with an ``error`` (a bad
      research-guess LinkedIn URL: "unrecognized linkedin profile payload"), and
      those entries have no substance — feeding them to the LLM only produces
      hallucinated filler.
    - ``summary``: the persisted ``simple_summary``, read from the RAW record. The
      dict ``read_usable_cached_profile`` rebuilds for a legacy file does not carry
      it, so it must come from the record on disk.
    """

    exists: bool
    usable: bool
    summarizable: bool
    summary: str

    @property
    def failed(self) -> bool:
        """A record on disk that yields no usable profile: a fetch that already
        TRIED and produced nothing summarizable. It still needs a (re-)fetch, but
        it is excluded from the summary-miss projection (the hallucination guard).
        Distinct from "uncached", which is no record at all."""
        return self.exists and not self.usable


NO_CACHED_PROFILE = CachedProfileState(exists=False, usable=False, summarizable=False, summary="")


def read_profile_state(cache_dir: Path, pub: str) -> CachedProfileState:
    """The one door onto the profile cache for this stage: parse a pub's record at
    the boundary and hand back its typed state. Every missing-path / non-dict guard
    lives here, so nothing downstream re-opens the file or re-checks its shape."""
    path = profile_cache_path(cache_dir, pub)
    if not path or not path.exists():
        return NO_CACHED_PROFILE
    record = read_json(path, None)
    if not isinstance(record, dict):
        return NO_CACHED_PROFILE
    summary = str(record.get(SUMMARY_FIELD) or "").strip()
    cached = read_usable_cached_profile(path)
    if not cached:
        return CachedProfileState(exists=True, usable=False, summarizable=False, summary=summary)
    normalized = cached.get("normalized_profile")
    summarizable = bool(
        isinstance(normalized, dict) and normalized.get("success")
        and (normalized.get("headline") or normalized.get("experiences")
             or normalized.get("education") or (normalized.get("summary") or "").strip()))
    return CachedProfileState(exists=True, usable=True, summarizable=summarizable, summary=summary)


class QueueVerdict(Enum):
    """What one queue link needs — the whole decision space of ``classify_link``."""

    NO_PUBLIC_IDENTIFIER = "no_public_identifier"
    FETCH_THEN_SUMMARIZE = "fetch_then_summarize"
    FETCH_NOT_SUMMARIZABLE = "fetch_not_summarizable"
    SUMMARIZE_ONLY = "summarize_only"
    ALREADY_DONE = "already_done"


def classify_link(pub: str, state: CachedProfileState) -> QueueVerdict:
    """The entire per-person decision, first rule wins.

    It reads correctly BOTH before and after the fetch, which is why `execute()`
    can simply re-run it against a freshly re-read cache:

      no pub          -> nothing to fetch or summarize (defensive: `review_queue_links`
                         already drops these, so normally unreachable)
      failed record   -> re-fetch, and NEVER summarize. After the fetch this is
                         exactly the bad-URL cohort: it is cached but not
                         summarizable, so it drops OUT of the summary-miss set and
                         never reaches the LLM (no "Jordan Bravo is a professional
                         at a company" filler). Stale garbage summaries it may
                         still carry are irrelevant — `cleanup_garbage_summaries`
                         strips those first.
      no usable record-> fetch, AND it is a summary miss: uncached today, and
                         summarizable once the fetch lands, so the dry run must
                         project its LLM cost.
      no summary yet  -> a real cached profile that simply has not been summarized.
      otherwise       -> cached and summarized: an already-summarized person is
                         never a miss.
    """
    if not pub:
        return QueueVerdict.NO_PUBLIC_IDENTIFIER
    if state.failed:
        return QueueVerdict.FETCH_NOT_SUMMARIZABLE
    if not state.usable:
        return QueueVerdict.FETCH_THEN_SUMMARIZE
    if not state.summary:
        return QueueVerdict.SUMMARIZE_ONLY
    return QueueVerdict.ALREADY_DONE


@dataclass(frozen=True)
class QueueBuckets:
    """The classified review-profile queue.

    ``fetch`` and ``summarize`` deliberately OVERLAP — an uncached person is in
    both — which is why `execute()` does not subtract the fetch misses a second
    time when it derives ``already_summarized``.

    - ``fetch``: links with no usable cached RapidAPI profile (uncached, or cached
      but failed) — they need a (re-)fetch.
    - ``summarize``: summary misses — every link with no ``simple_summary`` except
      cached-but-failed/empty profiles.
    - ``not_summarizable``: cached but failed/empty profiles — surfaced for counts
      and cleanup, never sent to the LLM. At execution time these are the fetches
      we must NOT feed to the LLM.
    - ``no_public_identifier``: queue rows we can neither fetch nor summarize
      (defensive; normally empty).

    Frozen and fully populated at construction: `classify_queue` fills four local
    lists and builds this once, so the value is never appended to after the fact.
    """

    fetch: list[dict[str, str]]
    summarize: list[dict[str, str]]
    not_summarizable: list[dict[str, str]]
    no_public_identifier: list[dict[str, str]]


def classify_queue(links: list[dict[str, str]], cache_dir: Path) -> QueueBuckets:
    """Bucket the whole review-profile queue by each link's ``classify_link`` verdict.

    Reads the cache fresh on every call, per link: `execute()` classifies three
    times (before the fetch, after it, and once more at the end) and the post-fetch
    passes MUST see what the fetch just wrote, so nothing is carried across calls.
    """
    fetch: list[dict[str, str]] = []
    summarize: list[dict[str, str]] = []
    not_summarizable: list[dict[str, str]] = []
    no_public_identifier: list[dict[str, str]] = []
    for link in links:
        pub = _link_pub(link)
        verdict = classify_link(pub, read_profile_state(cache_dir, pub))
        if verdict is QueueVerdict.NO_PUBLIC_IDENTIFIER:
            no_public_identifier.append(link)
        elif verdict is QueueVerdict.FETCH_NOT_SUMMARIZABLE:
            fetch.append(link)
            not_summarizable.append(link)
        elif verdict is QueueVerdict.FETCH_THEN_SUMMARIZE:
            fetch.append(link)
            summarize.append(link)
        elif verdict is QueueVerdict.SUMMARIZE_ONLY:
            summarize.append(link)
    return QueueBuckets(
        fetch=fetch,
        summarize=summarize,
        not_summarizable=not_summarizable,
        no_public_identifier=no_public_identifier,
    )


def cleanup_garbage_summaries(links: list[dict[str, str]], cache_dir: Path) -> list[str]:
    """Self-heal: strip a persisted ``simple_summary`` from any cache entry whose
    profile is NOT summarizable (failed/empty fetch). This removes generic filler
    a prior run may have written before this guard existed. Returns the cleaned pubs."""
    cleaned: list[str] = []
    for link in links:
        pub = _link_pub(link)
        if not pub:
            continue
        state = read_profile_state(cache_dir, pub)
        if state.summary and not state.summarizable:
            _clear_summary(cache_dir, pub)
            cleaned.append(pub)
    return cleaned


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

    Run-time guard: the cache state is re-read HERE, after the fetch, and only
    profiles summarizable AT THIS MOMENT (a successful fetch with substantive
    fields) reach the LLM — never a pre-fetch verdict. A failed/empty fetch is
    skipped even if it slipped into the miss list, so we never hallucinate filler
    for a bad-URL profile.
    """
    summarizable = [link for link in misses
                    if read_profile_state(cache_dir, _link_pub(link)).summarizable]
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


def prefetch(misses: list[dict[str, str]], cache_dir: Path,
             *, limit: int = 0, concurrency: int = DEFAULT_FETCH_CONCURRENCY,
             rpm: int = RAPIDAPI_RPM_DEFAULT) -> dict[str, int]:
    """One `get_profile` call per miss (the client writes the cache); counts
    only — the cache files are the durable output. Fan-out is wide
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
                                cache_dir=cache_dir)

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


class PrefetchPrivacy(BaseModel):
    """The `privacy` block — the same three flags the raw dict carried."""
    message_bodies_read: bool = False
    network_called: bool = False
    paid_provider_called: bool = False


class PrefetchSummaryBlock(BaseModel):
    """The `summary` block. Written only on a `--fetch` run; `tokens` and
    `actual_cost_usd` only when the LLM actually ran, exactly as before (the
    None-valued fields are dropped by `to_payload()`)."""
    status: str = ""
    counts: dict[str, int] = {}
    tokens: dict[str, int] | None = None
    actual_cost_usd: float | None = None


class PrefetchProfilesManifest(StageManifest):
    """The stage's typed manifest payload — the raw dict's keys verbatim,
    including the `source` key `write_manifest` used to inject. The branch-only
    keys are `| None` so `to_payload()` drops them on the runs that never set
    them (a dry run has no `counts`/`summary`), which is what the raw dict did by
    simply not assigning them."""
    source: str = STAGE
    queue_links: int = 0
    cache_misses: int = 0
    summary_misses: int = 0
    not_summarizable: int = 0
    already_cached: int = 0
    already_summarized: int = 0
    no_public_identifier: int = 0
    cleaned_garbage_summaries: int = 0
    cleaned_public_identifiers: list[str] = []
    estimated_rapidapi_calls: int = 0
    estimated_summary_calls: int = 0
    missing_public_identifiers: list[str] = []
    summary_missing_public_identifiers: list[str] = []
    not_summarizable_public_identifiers: list[str] = []
    model: str = ""
    reasoning_effort: str = ""
    summary_concurrency: int = 0
    fetch_concurrency: int = 0
    rapidapi_rpm: int = 0
    profile_cache_dir: str = ""
    privacy: PrefetchPrivacy = PrefetchPrivacy()
    # `int | float`, not `float`: `_estimated_llm_cost` sums an EMPTY generator to
    # the int 0 for a zero-miss queue and to a float otherwise, and the dry-run
    # note interpolates these verbatim ("~$0–0" vs "~$0.0–0.0"). A plain `float`
    # would coerce the zero case and silently change the reported text.
    estimated_llm_cost_usd_low: int | float = 0
    estimated_llm_cost_usd_high: int | float = 0
    note: str | None = None
    counts: dict[str, int] | None = None
    remaining_misses: int | None = None
    summary: PrefetchSummaryBlock | None = None
    remaining_summary_misses: int | None = None
    duration_seconds: float = 0.0


class PrefetchProfiles(Node):
    """Fills the shared profile cache (and its summaries) for the Check-Profile
    review queue. Cache-only by default — `fetch=False` is the spend-free preview
    that still runs through this node, because the dry run has always written this
    stage's manifest."""

    name = "deep_prefetch"
    # Everything this stage scans is optional: before review has produced any of
    # it, the queue is simply empty and the run reports zero misses. The cache is
    # BOTH read (the miss diff) and written (the fetch), which is not a cycle —
    # `graph.check_graph` drops self-edges.
    inputs = (
        Artifact(path=str(VERDICTS_JSONL), required=False),
        Artifact(path=str(LINKEDIN_OVERRIDES_CSV), required=False),
        Artifact(path=str(SYNTHETIC_PEOPLE_CSV), required=False),
        Artifact(path=FACTS_TEMPLATE, required=False),
        Artifact(path=str(DEFAULT_PEOPLE_CSV), required=False),
        Artifact(path=PARENT_TEMPLATE, required=False),
        Artifact(path=DOSSIER_TEMPLATE, required=False),
        Artifact(path=PROFILE_CACHE_TEMPLATE, external=True, required=False),
    )
    # The profile cache is deliberately NOT a declared output. It is EXTERNAL
    # data — materialized RapidAPI responses — hydrated opportunistically by
    # several nodes (this one on purpose via --fetch, owner/retargets on a
    # miss). Declaring one in-graph producer would pin a prefetch<->reconcile
    # cycle over what is a cross-run cache, not a pipeline edge; this node's
    # durable record is its manifest.
    outputs = ()
    payload = PrefetchProfilesManifest
    # Where `write_manifest(STAGE, payload, import_dir=ROOT)` has always put it:
    # `<import_dir>/<stage>/manifest.json` (`imports/common.py`). Unmoved.
    manifest = str(ROOT / STAGE / "manifest.json")

    def __init__(
        self,
        *,
        verdicts: Path | None = None,
        review: Path | None = None,
        synthetic_people: Path | None = None,
        facts_dir: Path | None = None,
        people_csv: Path | None = None,
        parents_dir: Path | None = None,
        dossier_dir: Path | None = None,
        profile_cache_dir: Path | None = None,
        fetch: bool = False,
        no_llm: bool = False,
        model: str = DEFAULT_SUMMARY_MODEL,
        reasoning_effort: str = DEFAULT_SUMMARY_EFFORT,
        limit: int = 0,
        summary_concurrency: int = 0,
        fetch_concurrency: int = DEFAULT_FETCH_CONCURRENCY,
        rapidapi_rpm: int = RAPIDAPI_RPM_DEFAULT,
        timeout: int = 120,
        max_retries: int = 4,
    ) -> None:
        self.verdicts = Path(verdicts or VERDICTS_JSONL)
        self.review = Path(review or LINKEDIN_OVERRIDES_CSV)
        self.synthetic_people = Path(synthetic_people or SYNTHETIC_PEOPLE_CSV)
        self.facts_dir = Path(facts_dir or FACTS_DIR)
        self.people_csv = Path(people_csv or DEFAULT_PEOPLE_CSV)
        self.parents_dir = Path(parents_dir or PARENTS_DIR)
        self.dossier_dir = Path(dossier_dir or DOSSIER_DIR)
        self.profile_cache_dir = Path(profile_cache_dir or PROFILE_CACHE_DIR)
        # The ONE spend door: everything paid (RapidAPI fetch, then OpenAI
        # summaries) hangs off this flag, exactly as `--fetch` always has.
        self.fetch = fetch
        self.no_llm = no_llm
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.limit = limit
        # 0 = resolve from env/profile. The CLI passes the already-resolved value
        # (an explicit --concurrency wins there).
        self.summary_concurrency = summary_concurrency
        self.fetch_concurrency = fetch_concurrency
        self.rapidapi_rpm = rapidapi_rpm
        self.timeout = timeout
        self.max_retries = max_retries

    def bindings(self) -> dict[str, str]:
        return {
            str(VERDICTS_JSONL): str(self.verdicts),
            str(LINKEDIN_OVERRIDES_CSV): str(self.review),
            str(SYNTHETIC_PEOPLE_CSV): str(self.synthetic_people),
            FACTS_TEMPLATE: str(self.facts_dir / "{person_id}.jsonl"),
            str(DEFAULT_PEOPLE_CSV): str(self.people_csv),
            PARENT_TEMPLATE: str(self.parents_dir / "{slug}.md"),
            DOSSIER_TEMPLATE: str(self.dossier_dir / "{slug}.md"),
            PROFILE_CACHE_TEMPLATE: str(self.profile_cache_dir / "{public_identifier}.json"),
        }

    def execute(self) -> PrefetchProfilesManifest:
        started = time.monotonic()
        cache_dir = self.profile_cache_dir
        parents = _all_review_parents(
            self.verdicts, self.review, self.synthetic_people,
            self.facts_dir, self.people_csv,
            self.parents_dir, self.dossier_dir, cache_dir)
        links = review_queue_links(parents)
        # Self-heal FIRST: strip any garbage simple_summary a prior run wrote for a
        # failed/empty profile, so it never lingers in the UI. Free, local, idempotent.
        cleaned_summaries = cleanup_garbage_summaries(links, cache_dir)
        buckets = classify_queue(links, cache_dir)
        fetch_misses, summ_misses = buckets.fetch, buckets.summarize
        not_summarizable, no_pub = buckets.not_summarizable, buckets.no_public_identifier
        use_llm = not self.no_llm
        summary_concurrency = self.summary_concurrency or env_or_profile_int(
            "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency",
            fallback=DEFAULT_SUMMARY_CONCURRENCY)
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
        estimated_summary_calls = len(summ_misses) if use_llm else 0
        # Summarization runs ONLY over REAL cached profiles lacking a summary. Failed/
        # empty profiles are excluded (not summarizable) — we never feed empties to the
        # LLM, so it can't hallucinate generic filler for a bad-URL fetch.
        payload = PrefetchProfilesManifest(
            queue_links=len(links),
            cache_misses=len(fetch_misses),
            summary_misses=len(summ_misses),
            not_summarizable=len(not_summarizable),
            already_cached=already_cached,
            already_summarized=already_summarized,
            no_public_identifier=len(no_pub),
            cleaned_garbage_summaries=len(cleaned_summaries),
            cleaned_public_identifiers=sorted(cleaned_summaries),
            estimated_rapidapi_calls=len(fetch_misses),
            estimated_summary_calls=estimated_summary_calls,
            missing_public_identifiers=sorted(link["public_identifier"] for link in fetch_misses),
            summary_missing_public_identifiers=sorted(
                link["public_identifier"] for link in summ_misses),
            not_summarizable_public_identifiers=sorted(
                link["public_identifier"] for link in not_summarizable),
            model=self.model,
            reasoning_effort=reasoning_effort(self.reasoning_effort),
            summary_concurrency=summary_concurrency,
            fetch_concurrency=max(1, self.fetch_concurrency),
            rapidapi_rpm=self.rapidapi_rpm,
            profile_cache_dir=str(cache_dir),
            privacy=PrefetchPrivacy(message_bodies_read=False,
                                    network_called=bool(self.fetch),
                                    paid_provider_called=bool(self.fetch)),
            **_estimated_llm_cost(estimated_summary_calls, self.model),
        )

        if not self.fetch:
            payload.status = "dry_run"
            skipped_note = (f"; {len(not_summarizable)} failed/empty profile(s) not summarizable"
                            if not_summarizable else "")
            cleaned_note = (f"; cleaned {len(cleaned_summaries)} stale summary(ies)"
                            if cleaned_summaries else "")
            payload.note = (
                f"dry run: {len(fetch_misses)} fetch miss(es) would cost ~{len(fetch_misses)} "
                f"RapidAPI call(s); {payload.estimated_summary_calls} summary miss(es) would "
                f"cost ~${payload.estimated_llm_cost_usd_low}–{payload.estimated_llm_cost_usd_high} "
                f"LLM{skipped_note}{cleaned_note}; rerun with --fetch to spend")
        elif not rapidapi_key():
            payload.status = "blocked_no_key"
            payload.privacy.network_called = False
            payload.privacy.paid_provider_called = False
            payload.note = "RAPIDAPI_LINKEDIN_KEY / RAPIDAPI_KEY not configured; nothing fetched"
        else:
            counts = prefetch(fetch_misses, cache_dir,
                              limit=self.limit, concurrency=max(1, self.fetch_concurrency),
                              rpm=self.rapidapi_rpm)
            counts["already_cached"] = already_cached
            payload.counts = counts
            # Re-classify AFTER the fetch: a failed fetch (bad URL) now sits in the
            # not_summarizable bucket, NOT the summarize bucket — so we never hand it
            # to the LLM. --limit caps the whole run.
            post = classify_queue(links, cache_dir)
            payload.remaining_misses = len(post.fetch)
            status = "completed" if not counts["failed"] else "completed_with_failures"
            pending_summary = post.summarize
            if self.limit:
                pending_summary = pending_summary[:max(0, self.limit - counts["attempted"])]
            # Fetch failures show up as newly non-summarizable cached entries; report
            # them so the manifest explains why some fetched people got no summary.
            skipped_no_profile = len(post.not_summarizable)
            summary_counts = {"summarized": 0, "failed": 0, "attempted": 0,
                              "already_summarized": already_summarized,
                              "skipped_no_profile": skipped_no_profile,
                              "pending": len(pending_summary)}
            if not use_llm:
                payload.summary = PrefetchSummaryBlock(status="skipped_no_llm", counts=summary_counts)
            elif not os.getenv("OPENAI_API_KEY"):
                payload.summary = PrefetchSummaryBlock(status="blocked_no_key", counts=summary_counts)
                payload.privacy.paid_provider_called = True  # RapidAPI still ran
            elif pending_summary:
                result = summarize(
                    pending_summary, cache_dir, model=self.model,
                    effort=reasoning_effort(self.reasoning_effort),
                    concurrency=summary_concurrency, timeout=self.timeout,
                    max_retries=self.max_retries)
                payload.summary = PrefetchSummaryBlock(
                    status="completed",
                    counts={**summary_counts, **result["counts"]},
                    tokens=result["tokens"],
                    actual_cost_usd=result["actual_cost_usd"])
                if result["counts"]["failed"]:
                    status = "completed_with_failures"
            else:
                payload.summary = PrefetchSummaryBlock(status="completed", counts=summary_counts)
            payload.remaining_summary_misses = len(classify_queue(links, cache_dir).summarize)
            payload.status = status
        payload.duration_seconds = round(time.monotonic() - started, 2)
        return payload


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
    # LLM summary fan-out: an explicit --concurrency wins, else env/profile (owner
    # default 200). RapidAPI stays on the separate, bounded --fetch-concurrency.
    summary_concurrency = (
        max(1, args.concurrency) if args.concurrency
        else env_or_profile_int("POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency",
                                fallback=DEFAULT_SUMMARY_CONCURRENCY))
    payload = PrefetchProfiles(
        verdicts=Path(args.verdicts),
        review=Path(args.review),
        synthetic_people=Path(args.synthetic_people),
        facts_dir=Path(args.facts_dir),
        people_csv=Path(args.people_csv),
        parents_dir=Path(args.parents_dir),
        dossier_dir=Path(args.dossier_dir),
        profile_cache_dir=Path(args.profile_cache_dir),
        fetch=args.fetch,
        no_llm=args.no_llm,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        limit=args.limit,
        summary_concurrency=summary_concurrency,
        fetch_concurrency=args.fetch_concurrency,
        rapidapi_rpm=args.rapidapi_rpm,
        timeout=args.timeout,
        max_retries=args.max_retries,
    ).run()
    emit(payload.to_payload())


if __name__ == "__main__":
    main()
