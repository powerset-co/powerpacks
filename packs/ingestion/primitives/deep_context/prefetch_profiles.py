#!/usr/bin/env python3
"""Fill the SQLite review queue's local LinkedIn profile cache."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import time
from collections import deque
from dataclasses import dataclass
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
from packs.ingestion.primitives.common.jsonio import now_iso, read_json, write_json
from packs.ingestion.primitives.imports.common import write_manifest
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    ENRICH_MANIFEST,
    FACTS_DIR,
    LINKEDIN_OVERRIDES_CSV,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    ROOT,
    VERDICTS_JSONL,
    emit,
    load_env,
)
from packs.ingestion.primitives.deep_context.db import views
from packs.ingestion.primitives.deep_context.db.projectors import project_manifest
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.prompts.loader import load_prompt
from packs.ingestion.primitives.deep_context.reconcile_linkedin import linkedin_view
from packs.ingestion.primitives.enrich.profile_cache import (
    profile_cache_path,
    read_usable_cached_profile,
)
from packs.ingestion.primitives.enrich.rapidapi_client import rapidapi_key, rapidapi_profile
from packs.ingestion.schemas.people_schema import extract_public_identifier

STAGE = "profile-prefetch"
CANONICAL_DB = ROOT / "deep-context.sqlite"
SYNTHETIC_PEOPLE_CSV = LINKEDIN_OVERRIDES_CSV.parent / "synthetic-people.csv"
DEFAULT_SUMMARY_MODEL = "gpt-5-nano"
DEFAULT_SUMMARY_EFFORT = "minimal"
SUMMARY_MAX_OUTPUT_TOKENS = 400
SUMMARY_FIELD = "simple_summary"
DEFAULT_SUMMARY_CONCURRENCY = 200
RAPIDAPI_RPM_DEFAULT = 300
DEFAULT_FETCH_CONCURRENCY = 40
SUMMARY_SYSTEM = load_prompt("profile_summary_system")
SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {"summary": {"type": "string"}}, "required": ["summary"],
}


class Payload(dict):
    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def to_payload(self) -> dict[str, Any]:
        return dict(self)


def review_queue_links(parents: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen, links = set(), []
    for parent in parents:
        for candidate in parent.get("candidates") or []:
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


@dataclass(frozen=True)
class CachedProfileState:
    exists: bool = False
    usable: bool = False
    summarizable: bool = False
    summary: str = ""

    @property
    def failed(self) -> bool:
        return self.exists and not self.usable


def _pub(link: dict[str, str]) -> str:
    return str(link.get("public_identifier") or "").strip().lower()


def read_profile_state(cache_dir: Path, pub: str) -> CachedProfileState:
    path = profile_cache_path(cache_dir, pub)
    if not path or not path.is_file():
        return CachedProfileState()
    record = read_json(path, None)
    if not isinstance(record, dict):
        return CachedProfileState()
    summary = str(record.get(SUMMARY_FIELD) or "").strip()
    cached = read_usable_cached_profile(path)
    if not cached:
        return CachedProfileState(True, False, False, summary)
    profile = cached.get("normalized_profile")
    substantive = isinstance(profile, dict) and (
        profile.get("headline") or profile.get("experiences") or profile.get("education")
        or str(profile.get("summary") or "").strip()
    )
    success = isinstance(profile, dict) and profile.get("success")
    return CachedProfileState(True, True, bool(success and substantive), summary)


def classify_queue(links: list[dict[str, str]], cache_dir: Path) -> dict[str, list[dict[str, str]]]:
    buckets: dict[str, list[dict[str, str]]] = {
        "fetch": [], "summarize": [], "not_summarizable": [], "no_pub": [],
    }
    for link in links:
        pub, state = _pub(link), read_profile_state(cache_dir, _pub(link))
        if not pub:
            buckets["no_pub"].append(link)
        elif state.exists and not state.usable:
            buckets["fetch"].append(link)
            buckets["not_summarizable"].append(link)
        elif not state.usable:
            buckets["fetch"].append(link)
            buckets["summarize"].append(link)
        elif not state.summary:
            buckets["summarize"].append(link)
    return buckets


def _update_summary(cache_dir: Path, pub: str, summary: str | None) -> None:
    path = profile_cache_path(cache_dir, pub)
    if not path or not path.is_file():
        return
    record = read_json(path, None)
    if not isinstance(record, dict):
        return
    if summary:
        record[SUMMARY_FIELD] = summary
        record["summarized_at"] = now_iso()
    else:
        record.pop(SUMMARY_FIELD, None)
        record.pop("summarized_at", None)
    write_json(path, record)


def cleanup_garbage_summaries(links: list[dict[str, str]], cache_dir: Path) -> list[str]:
    cleaned = []
    for link in links:
        pub = _pub(link)
        state = read_profile_state(cache_dir, pub)
        if pub and state.summary and not state.summarizable:
            _update_summary(cache_dir, pub, None)
            cleaned.append(pub)
    return cleaned


def _summary_prompt(link: dict[str, str], cache_dir: Path) -> str:
    profile = linkedin_view({
        "public_identifier": link["public_identifier"],
        "linkedin_url": link.get("linkedin_url") or "",
    }, cache_dir)
    lines = [f"Name: {profile.get('full_name') or link.get('name') or _pub(link)}"]
    for field, label in (("headline", "Headline"), ("location", "Location")):
        if profile.get(field):
            lines.append(f"{label}: {profile[field]}")
    for field, label in (("experiences", "Work history"), ("education", "Education")):
        if profile.get(field):
            lines.append(f"{label}:")
            lines.extend(f"- {item}" for item in profile[field])
    return "\n".join(lines)


async def _summarize_one(
    client: Any, link: dict[str, str], cache_dir: Path, *, model: str,
    effort: str, semaphore: asyncio.Semaphore, max_retries: int,
) -> dict[str, Any]:
    kwargs = responses_kwargs(model, effort=effort, schema=SUMMARY_SCHEMA,
                              schema_name="profile_summary",
                              max_output_tokens=SUMMARY_MAX_OUTPUT_TOKENS)
    async with semaphore:
        for attempt in range(max_retries + 1):
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
                if is_retryable(exc) and attempt < max_retries:
                    await asyncio.sleep(min(2 ** (attempt + 1), 30))
                    continue
                return {"summary": "", "usage": {},
                        "error": f"{type(exc).__name__}: {exc}"[:200]}
    return {"summary": "", "usage": {}, "error": "no result"}


def summarize(
    misses: list[dict[str, str]], cache_dir: Path, *, model: str,
    effort: str, concurrency: int, timeout: int, max_retries: int,
) -> dict[str, Any]:
    targets = [link for link in misses if read_profile_state(cache_dir, _pub(link)).summarizable]
    results: dict[int, dict[str, Any]] = {}

    async def driver() -> None:
        client = make_async_client(timeout=timeout)
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def one(index: int, link: dict[str, str]) -> tuple[int, dict[str, Any]]:
            return index, await _summarize_one(
                client, link, cache_dir, model=model, effort=effort,
                semaphore=semaphore, max_retries=max_retries,
            )
        try:
            await drain_pool(
                [one(index, link) for index, link in enumerate(targets)],
                lambda item: results.__setitem__(*item),
            )
        finally:
            await client.close()

    if targets:
        asyncio.run(driver())
    counts = {"summarized": 0, "failed": 0, "skipped_empty": 0,
              "attempted": len(targets)}
    tokens = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    for index, link in enumerate(targets):
        result = results.get(index, {"summary": "", "usage": {}, "error": "no result"})
        for key in tokens:
            tokens[key] += int(result.get("usage", {}).get(key, 0))
        if result.get("summary"):
            _update_summary(cache_dir, _pub(link), result["summary"])
            counts["summarized"] += 1
        elif result.get("error"):
            counts["failed"] += 1
        else:
            counts["skipped_empty"] += 1
    return {
        "counts": counts, "tokens": tokens,
        "actual_cost_usd": estimate_cost_usd(
            tokens["input_tokens"], tokens["output_tokens"] + tokens["reasoning_tokens"], model
        ),
    }


def _wait_for_fetch_slot(starts: deque[float], rpm: int) -> None:
    if rpm <= 0:
        return
    while True:
        now = time.monotonic()
        while starts and now - starts[0] >= 60:
            starts.popleft()
        if len(starts) < rpm:
            starts.append(now)
            return
        time.sleep(max(0.0, 60 - (now - starts[0])))


def prefetch(
    misses: list[dict[str, str]], cache_dir: Path, *, limit: int = 0,
    concurrency: int = DEFAULT_FETCH_CONCURRENCY, rpm: int = RAPIDAPI_RPM_DEFAULT,
) -> dict[str, int]:
    targets = misses[:limit] if limit else misses
    counts = {"fetched": 0, "from_cache": 0, "failed": 0, "attempted": len(targets)}
    if not targets:
        return counts
    starts: deque[float] = deque()
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(concurrency, len(targets)))
    ) as pool:
        futures = []
        for link in targets:
            _wait_for_fetch_slot(starts, rpm)
            futures.append(pool.submit(
                rapidapi_profile, _pub(link), link["linkedin_url"], cache_dir=cache_dir,
            ))
        for future in futures:
            result = future.result()
            if (result.get("normalized_profile") or {}).get("success") is True:
                counts["from_cache" if result.get("from_cache") else "fetched"] += 1
            else:
                counts["failed"] += 1
    return counts


def _estimated_cost(count: int, model: str) -> dict[str, float]:
    return {
        "estimated_llm_cost_usd_low": round(
            count * estimate_cost_usd(500, 60, model), 6),
        "estimated_llm_cost_usd_high": round(
            count * estimate_cost_usd(500, 160, model), 6),
    }


class PrefetchProfiles:
    """Fetch and summarize profiles, then finish the shared enrichment receipt."""

    name = "deep_prefetch"

    def __init__(
        self, *, db: Db, verdicts: Path | None = None, review: Path | None = None,
        synthetic_people: Path | None = None, facts_dir: Path | None = None,
        people_csv: Path | None = None, parents_dir: Path | None = None,
        dossier_dir: Path | None = None, profile_cache_dir: Path | None = None,
        fetch: bool = False, no_llm: bool = False,
        model: str = DEFAULT_SUMMARY_MODEL,
        reasoning_effort: str = DEFAULT_SUMMARY_EFFORT, limit: int = 0,
        summary_concurrency: int = 0,
        fetch_concurrency: int = DEFAULT_FETCH_CONCURRENCY,
        rapidapi_rpm: int = RAPIDAPI_RPM_DEFAULT, timeout: int = 120,
        max_retries: int = 4, enrichment_manifest: Path | None = None,
    ) -> None:
        self.db, self.profile_cache_dir = db, Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.fetch, self.no_llm, self.model = fetch, no_llm, model
        self.effort, self.limit = reasoning_effort, limit
        self.summary_concurrency, self.fetch_concurrency = summary_concurrency, fetch_concurrency
        self.rapidapi_rpm, self.timeout, self.max_retries = rapidapi_rpm, timeout, max_retries
        self.enrichment_manifest = Path(enrichment_manifest or ENRICH_MANIFEST)
        del verdicts, review, synthetic_people, facts_dir, people_csv, parents_dir, dossier_dir

    def run(self) -> Payload:
        payload = self.execute()
        write_manifest(STAGE, payload.to_payload(), import_dir=ROOT)
        return payload

    def execute(self) -> Payload:
        started = time.monotonic()
        cache = self.profile_cache_dir
        links = review_queue_links(views.linkedin_review(self.db, "queue"))
        cleaned = cleanup_garbage_summaries(links, cache)
        before = classify_queue(links, cache)
        fetch_misses, summary_misses = before["fetch"], before["summarize"]
        not_summarizable, no_pub = before["not_summarizable"], before["no_pub"]
        concurrency = self.summary_concurrency or env_or_profile_int(
            "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency",
            fallback=DEFAULT_SUMMARY_CONCURRENCY,
        )
        summary_calls = 0 if self.no_llm else len(summary_misses)
        payload = Payload(
            status="", source=STAGE, queue_links=len(links), cache_misses=len(fetch_misses),
            summary_misses=len(summary_misses), not_summarizable=len(not_summarizable),
            already_cached=len(links) - len(fetch_misses) - len(no_pub),
            already_summarized=len(links) - len(summary_misses) - len(not_summarizable) - len(no_pub),
            no_public_identifier=len(no_pub), cleaned_garbage_summaries=len(cleaned),
            cleaned_public_identifiers=sorted(cleaned),
            estimated_rapidapi_calls=len(fetch_misses), estimated_summary_calls=summary_calls,
            missing_public_identifiers=sorted(_pub(link) for link in fetch_misses),
            summary_missing_public_identifiers=sorted(_pub(link) for link in summary_misses),
            not_summarizable_public_identifiers=sorted(_pub(link) for link in not_summarizable),
            model=self.model, reasoning_effort=reasoning_effort(self.effort),
            summary_concurrency=concurrency, fetch_concurrency=max(1, self.fetch_concurrency),
            rapidapi_rpm=self.rapidapi_rpm, profile_cache_dir=str(cache),
            privacy={"message_bodies_read": False, "network_called": bool(self.fetch),
                     "paid_provider_called": bool(self.fetch)},
            **_estimated_cost(summary_calls, self.model),
        )
        if not self.fetch:
            payload["status"] = "dry_run"
            skipped = (f"; {len(not_summarizable)} failed/empty profile(s) not summarizable"
                       if not_summarizable else "")
            cleaned_note = (f"; cleaned {len(cleaned)} stale summary(ies)" if cleaned else "")
            payload["note"] = (
                f"dry run: {len(fetch_misses)} fetch miss(es) would cost ~{len(fetch_misses)} "
                f"RapidAPI call(s); {summary_calls} summary miss(es) would cost "
                f"~${payload.estimated_llm_cost_usd_low}–{payload.estimated_llm_cost_usd_high} "
                f"LLM{skipped}{cleaned_note}; "
                "rerun with --fetch to spend"
            )
        elif fetch_misses and not rapidapi_key():
            payload["status"] = "blocked_no_key"
            payload["privacy"].update(network_called=False, paid_provider_called=False)
            payload["note"] = "RAPIDAPI_LINKEDIN_KEY / RAPIDAPI_KEY not configured; nothing fetched"
        else:
            counts = prefetch(fetch_misses, cache, limit=self.limit,
                              concurrency=max(1, self.fetch_concurrency), rpm=self.rapidapi_rpm)
            counts["already_cached"] = payload.already_cached
            payload["counts"] = counts
            after = classify_queue(links, cache)
            payload["remaining_misses"] = len(after["fetch"])
            pending = after["summarize"]
            if self.limit:
                pending = pending[:max(0, self.limit - counts["attempted"])]
            summary_counts = {
                "summarized": 0, "failed": 0, "attempted": 0,
                "already_summarized": payload.already_summarized,
                "skipped_no_profile": len(after["not_summarizable"]), "pending": len(pending),
            }
            status = "completed_with_failures" if counts["failed"] else "completed"
            if self.no_llm:
                payload["summary"] = {"status": "skipped_no_llm", "counts": summary_counts}
            elif not os.getenv("OPENAI_API_KEY"):
                payload["summary"] = {"status": "blocked_no_key", "counts": summary_counts}
            elif pending:
                result = summarize(pending, cache, model=self.model,
                                   effort=reasoning_effort(self.effort), concurrency=concurrency,
                                   timeout=self.timeout, max_retries=self.max_retries)
                payload["summary"] = {
                    "status": "completed",
                    "counts": {**summary_counts, **result["counts"]},
                    "tokens": result["tokens"],
                    "actual_cost_usd": result["actual_cost_usd"],
                }
                if result["counts"]["failed"]:
                    status = "completed_with_failures"
            else:
                payload["summary"] = {"status": "completed", "counts": summary_counts}
            payload["remaining_summary_misses"] = len(classify_queue(links, cache)["summarize"])
            payload["status"] = status
        payload["duration_seconds"] = round(time.monotonic() - started, 2)
        if self.fetch and payload.status in {"completed", "completed_with_failures"}:
            self._finish_enrichment(payload)
        return payload

    def _finish_enrichment(self, payload: Payload) -> None:
        path = self.enrichment_manifest
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(current, dict):
            return
        failed = payload.status == "completed_with_failures"
        receipt = {**current, "stage": "enrich",
                   "status": "completed_with_errors" if failed else "completed",
                   "phase": "profiles_complete", "prefetch": payload.to_payload()}
        if failed:
            receipt["error"] = "profile prefetch completed with failures"
        else:
            receipt.pop("error", None)
        receipt.pop("updated_at", None)
        receipt.pop("created_at", None)
        write_manifest(path.parent.name, receipt, import_dir=path.parent.parent)
        project_manifest(self.db, path)


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
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--model", default=DEFAULT_SUMMARY_MODEL)
    parser.add_argument("--reasoning-effort", default=DEFAULT_SUMMARY_EFFORT)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=0)
    parser.add_argument("--fetch-concurrency", type=int, default=DEFAULT_FETCH_CONCURRENCY)
    parser.add_argument("--rapidapi-rpm", type=int, default=RAPIDAPI_RPM_DEFAULT)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=4)
    args = parser.parse_args(argv)
    load_env()
    concurrency = args.concurrency or env_or_profile_int(
        "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency",
        fallback=DEFAULT_SUMMARY_CONCURRENCY,
    )
    payload = PrefetchProfiles(
        db=Db(Path(args.db)), verdicts=Path(args.verdicts), review=Path(args.review),
        synthetic_people=Path(args.synthetic_people), facts_dir=Path(args.facts_dir),
        people_csv=Path(args.people_csv), parents_dir=Path(args.parents_dir),
        dossier_dir=Path(args.dossier_dir), profile_cache_dir=Path(args.profile_cache_dir),
        fetch=args.fetch, no_llm=args.no_llm, model=args.model,
        reasoning_effort=args.reasoning_effort, limit=args.limit,
        summary_concurrency=concurrency, fetch_concurrency=args.fetch_concurrency,
        rapidapi_rpm=args.rapidapi_rpm, timeout=args.timeout, max_retries=args.max_retries,
    ).run()
    emit(payload.to_payload())


if __name__ == "__main__":
    main()
