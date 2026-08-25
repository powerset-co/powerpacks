"""Turning reranked rows into the reviewed candidates a pond reports.

  rerank rows -> _review_rows (score threshold, then the fallback threshold)
              -> _review_candidates (join the profile JSONL and company context)
              -> _annotate_company_fit (one checkpointed fit call per candidate)

Every fit call is checkpointed by input hash under the pond directory, so a
rerun re-reads the checkpoint instead of paying again; a failed call falls back
to the deterministic company-context label.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # direct script execution
    from company_context import (
        apply_company_fit_response, company_fit_messages, fallback_company_fit,
    )
    from precedents import retrieve_fit_precedents
    from harness.artifacts import (
        _price_usage_log, _read_json, _response_usage, _write_json, resolve_artifact_path,
    )
    from harness.summary import _save
except ImportError:  # pragma: no cover - module execution
    from ..company_context import (
        apply_company_fit_response, company_fit_messages, fallback_company_fit,
    )
    from ..precedents import retrieve_fit_precedents
    from .artifacts import (
        _price_usage_log, _read_json, _response_usage, _write_json, resolve_artifact_path,
    )
    from .summary import _save
from openai_client import make_async_openai_client
from packs.indexing.lib.openai_stream import drain_pool

REVIEW_SCORE_THRESHOLD = .70
FALLBACK_REVIEW_SCORE_THRESHOLD = .30
FIT_CONCURRENCY = int(os.environ.get(
    "LLM_RERANK_CONCURRENCY", os.environ.get("SEARCH_V2_RERANK_MAX_CONCURRENT", "400")))


def _evaluation_text(text: str, exclusions: Sequence[str]) -> str:
    if exclusions:
        text += "\n\nRecruiter rerank exclusions: candidates primarily specializing in "
        text += "; ".join(exclusions) + " are not a fit for this search."
    return text


def _profiles(path_text: Any) -> dict[str, dict[str, Any]]:
    path = resolve_artifact_path(path_text)
    if not path.is_file():
        return {}
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return {str(row["person_id"]): row for row in (json.loads(line) for line in handle if line.strip())
                if row.get("person_id")}


def _level(title: Any) -> str:
    text = " ".join(str(title or "").lower().split())
    rules = (
        (r"\b(founder|owner|partner|chief|cto|ceo|cfo|coo)\b", "Founder / C-suite"),
        (r"\b(vp|vice president)\b", "VP"), (r"\b(director|head of)\b", "Director / Head"),
        (r"\bmanager\b", "Manager"), (r"\b(staff|principal)\b", "Staff / Principal"),
        (r"\bsenior\b", "Senior"), (r"\b(junior|associate|analyst|intern)\b", "Early career"),
    )
    return next((label for pattern, label in rules if re.search(pattern, text)), "Unspecified")


def _recent_roles(profile: Mapping[str, Any]) -> list[dict[str, str]]:
    return [{
        "title": str(row.get("title") or row.get("position_title") or "").strip(),
        "company": str(row.get("company_name") or row.get("company") or "").strip(),
    } for row in (profile.get("positions") or [])[:3] if isinstance(row, Mapping)]


def _trait_scores(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _rerank_score(row: Mapping[str, Any]) -> float:
    value = row.get("final_score")
    return float(value if value is not None else row.get("score") or 0)


def _review_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    primary = [row for row in rows if _rerank_score(row) >= REVIEW_SCORE_THRESHOLD]
    return primary or [row for row in rows
                       if _rerank_score(row) >= FALLBACK_REVIEW_SCORE_THRESHOLD]


def _review_candidates(rows: Sequence[Mapping[str, Any]],
                       profiles: Mapping[str, Mapping[str, Any]],
                       company_contexts: Sequence[Mapping[str, Any]] = (),
                       company_refs: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    candidates = []
    for index, row in enumerate(_review_rows(rows)):
        person = str(row.get("person_id") or "")
        profile = profiles.get(person) or {}
        title = row.get("current_titles") or profile.get("current_title")
        context = company_contexts[index] if index < len(company_contexts) else {}
        candidates.append({
            "person": person, "name": row.get("name") or profile.get("name"),
            "title": title,
            "company": row.get("current_companies") or profile.get("current_company"),
            "location": row.get("location") or profile.get("location") or profile.get("city"),
            "linkedin_url": row.get("linkedin_url") or profile.get("linkedin_url"),
            "score": round(float(row.get("final_score") or 0), 4),
            "source_operator": row.get("source_operator"),
            "source_channel": row.get("source_channel"),
            "current_company_headcount": context.get("headcount"),
            "current_company_stage": context.get("stage"),
            "current_company_funding": context.get("funding"),
            "current_company_funding_basis": context.get("funding_basis"),
            "company_timing": ((company_refs[index].get("company_timing")
                                if index < len(company_refs) else None) or "current"),
            "current_position_start_date": (company_refs[index].get("current_position_start_date")
                                            if index < len(company_refs) else None),
            "months_in_seat": (company_refs[index].get("months_in_seat")
                               if index < len(company_refs) else None),
            "recent_roles": _recent_roles(profile),
            "company_card_id": None,
            "trait_scores": _trait_scores(row.get("trait_scores")),
            "reason": " ".join(str(row.get("overall_reasoning") or "").split())[:900],
        })
    return candidates


def _annotate_company_fit(*, candidates: Sequence[Mapping[str, Any]], results: dict[str, Any],
                          run_dir: Path, pond_n: int, plan: Mapping[str, Any],
                          client: Any | None = None) -> list[dict[str, Any]]:
    if not candidates:
        return []
    jd = (run_dir / "jd.txt").read_text(encoding="utf-8")
    hiring_company = results.get("hiring_company_context") or results.get("hiring_company") or {}
    brief = results.get("brief") or {}
    precedents = retrieve_fit_precedents(
        title=str(results.get("title") or ""), brief=results.get("brief") or {},
        candidates=candidates)
    checkpoint_dir = run_dir / "ponds" / f"pond-{pond_n:02d}" / "company-fit"
    os.environ["POWERPACKS_USAGE_LOG"] = str(run_dir / "usage.jsonl")
    os.environ["POWERPACKS_USAGE_STAGE"] = f"search_harness.pond_{pond_n:02d}.company_fit"
    os.environ["OPENAI_SERVICE_TIER"] = "flex"

    async def annotate_all() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        semaphore = asyncio.Semaphore(max(1, min(FIT_CONCURRENCY, len(candidates))))
        api_client = client or make_async_openai_client(os.environ.get("OPENAI_API_KEY"))

        async def annotate_one(index: int, candidate: Mapping[str, Any]
                               ) -> tuple[dict[str, Any], dict[str, Any]]:
            messages = company_fit_messages(
                jd=jd, target_level=plan.get("target_level"), comp_band=plan.get("comp_band"),
                hiring_company=hiring_company, candidate=candidate, brief=brief,
                fit_precedents=precedents)
            input_sha = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
            checkpoint = checkpoint_dir / f"{index:03d}.json"
            record = _read_json(checkpoint) if checkpoint.is_file() else {}
            if record.get("input_sha") == input_sha and record.get("raw"):
                try:
                    return apply_company_fit_response(candidate, str(record["raw"])), {
                        "candidate_index": index, "input_sha": input_sha,
                        "checkpoint": str(checkpoint), "cached": True}
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            async with semaphore:
                response = await api_client.chat.completions.create(
                    model="gpt-5.6-luna", reasoning_effort="medium", service_tier="flex",
                    messages=messages, response_format={"type": "json_object"})
            record = {"input_sha": input_sha, "raw": response.choices[0].message.content or "{}",
                      "usage": _response_usage(response)}
            _write_json(checkpoint, record)
            annotated = apply_company_fit_response(candidate, str(record["raw"]))
            return annotated, {"candidate_index": index, "input_sha": input_sha,
                               "checkpoint": str(checkpoint), "cached": False}

        async def guarded(index: int, candidate: Mapping[str, Any]
                          ) -> tuple[int, dict[str, Any], dict[str, Any]]:
            try:
                annotated, record = await annotate_one(index, candidate)
            except Exception as exc:
                annotated = {**dict(candidate),
                             **fallback_company_fit(candidate, plan.get("target_level"))}
                record = {"candidate_index": index, "error": f"{type(exc).__name__}: {exc}"}
            return index, annotated, record

        output: list[dict[str, Any] | None] = [None] * len(candidates)
        records: list[dict[str, Any] | None] = [None] * len(candidates)

        def handle(value: tuple[int, dict[str, Any], dict[str, Any]]) -> None:
            index, annotated, record = value
            output[index], records[index] = annotated, record

        try:
            await drain_pool([
                guarded(index, candidate) for index, candidate in enumerate(candidates)], handle)
        finally:
            if client is None:
                await api_client.close()
        return ([row for row in output if row is not None],
                [row for row in records if row is not None])

    annotated, checkpoints = asyncio.run(annotate_all())
    raw_record = {"kind": "company_fit", "pond_n": pond_n, "checkpoints": checkpoints}
    raw_responses = results.setdefault("raw_model_responses", [])
    prior = next((index for index, row in enumerate(raw_responses)
                  if row.get("kind") == "company_fit" and row.get("pond_n") == pond_n), None)
    if prior is None:
        raw_responses.append(raw_record)
    else:
        raw_responses[prior] = raw_record
    _price_usage_log(run_dir / "usage.jsonl")
    _save(results, run_dir)
    return annotated
