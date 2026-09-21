"""Score original candidate evidence against a cleaned JD with Luna."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from packs.search.primitives.lib.cached_prompt import cached_messages
from packs.search.primitives.llm_rerank_candidates.cross_encoder import profile_evidence
from packs.search.primitives.shared.openai_client import make_async_openai_client

MODEL = "gpt-5.6-luna"
REASONING_EFFORT = "low"
MAX_COMPLETION_TOKENS = 2500
SCORE_TYPE = "ordinal_rating_1_to_5"
PROMPT_VERSION = "luna-capability-concise-20260917"
PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "terra-capability-v5.txt"
BASES = ("direct", "repeated_adjacent", "weak_inference", "mismatch", "exceptional")
OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["evidence", "rating", "basis"],
    "properties": {"evidence": {"type": "string"},
                   "rating": {"type": "integer", "minimum": 1, "maximum": 5},
                   "basis": {"type": "string", "enum": list(BASES)}},
}


def system_prompt(as_of: str) -> str:
    date.fromisoformat(as_of)
    return PROMPT.read_text(encoding="utf-8").replace("{as_of}", as_of)


def _company_description(text: str) -> str:
    endings = list(re.finditer(r"[.!?](?=\s|$)", text))
    excerpt = text[:endings[1].end()] if len(endings) >= 2 else text
    if len(excerpt) <= 800:
        return excerpt
    prefix = re.match(r"(?s)^(.{0,800})(?:\s|$)", excerpt)
    return prefix.group(1).rstrip() if prefix else ""


def _profile_evidence(person: dict) -> dict:
    evidence = profile_evidence(person)
    evidence.pop("title", None)
    evidence.pop("company", None)
    companies = {}
    for position in person.get("positions") or []:
        context = profile_evidence({"positions": [position]})["companies"][0]
        context.pop("investors", None)
        for field in ("funding_date", "last_funding_at", "last_funding_date", "first_funding_at"):
            value = position.get(field) or position.get(f"company_{field}")
            if value not in (None, "", "none", "null"):
                context[field] = value
        if context.get("company_description"):
            context["company_description"] = _company_description(context["company_description"])
        if context:
            companies[json.dumps(context, sort_keys=True, ensure_ascii=False)] = context
    evidence["companies"] = list(companies.values())
    return evidence


def build_request(*, jd: str, profile: dict, as_of: str) -> dict:
    if not isinstance(jd, str) or not jd.strip():
        raise ValueError("Luna requires a cleaned JD")
    passage = json.dumps(_profile_evidence(profile), ensure_ascii=False, separators=(",", ":"))
    return {
        "model": MODEL, "reasoning_effort": REASONING_EFFORT, "service_tier": "flex",
        "max_completion_tokens": MAX_COMPLETION_TOKENS, "store": False,
        "messages": cached_messages(system_prompt(as_of), f"<Job>\n{jd}\n</Job>\n",
                                    f"<Profile>\n{passage}\n</Profile>"),
        "extra_body": {"prompt_cache_options": {"mode": "explicit"}},
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "capability_rating", "strict": True, "schema": OUTPUT_SCHEMA}},
    }


def _validate_answer(answer: Any) -> dict:
    if not isinstance(answer, dict) or set(answer) != {"rating", "evidence", "basis"}:
        raise RuntimeError("Luna returned unexpected result fields; candidate remains unscored")
    if type(answer["rating"]) is not int or not 1 <= answer["rating"] <= 5:
        raise RuntimeError("Luna returned an invalid rating; candidate remains unscored")
    if not isinstance(answer["evidence"], str) or not answer["evidence"].strip():
        raise RuntimeError("Luna returned no explanation; candidate remains unscored")
    if answer["basis"] not in BASES:
        raise RuntimeError("Luna returned an invalid evidence basis; candidate remains unscored")
    return answer


async def score_candidates(*, jd: str, profiles: dict[str, dict], output_dir: Path,
                           as_of: str, api_key: str | None = None, concurrency: int = 32,
                           client: Any | None = None) -> dict:
    """Persist exact successful requests; failures never become negative ratings."""
    if concurrency < 1:
        raise ValueError("Luna concurrency must be positive")
    prompt_hash = hashlib.sha256(system_prompt(as_of).encode()).hexdigest()
    result = {
        "status": "ok" if profiles else "empty", "model": MODEL, "revision": prompt_hash,
        "prompt_version": PROMPT_VERSION, "assessment_date": as_of,
        "score_type": SCORE_TYPE, "scores": [], "artifacts": [],
        "requests": 0, "cached_candidates": 0,
        "usage": {"pairs": 0, "input_tokens": 0, "output_tokens": 0,
                  "cached_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0},
    }
    semaphore = asyncio.Semaphore(concurrency)
    owned_client = None
    requests, candidate_requests = {}, {}
    for person_id, profile in profiles.items():
        request = build_request(jd=jd, profile=profile, as_of=as_of)
        request_hash = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        requests[request_hash] = request
        candidate_requests[person_id] = request_hash

    async def score_one(request_hash: str, request: dict) -> tuple[dict, Path, bool]:
        nonlocal client, owned_client
        cache = output_dir / "terra" / f"{request_hash}.json"
        async with semaphore:
            if cache.exists():
                saved = json.loads(cache.read_text(encoding="utf-8"))
                _validate_answer(saved["answer"])
                return saved, cache, True
            if client is None:
                client = owned_client = make_async_openai_client(api_key=api_key, timeout=600, max_retries=0)
            response = await client.chat.completions.create(**request)
            if not response.choices:
                raise RuntimeError("Luna returned no choices; candidate remains unscored")
            choice = response.choices[0]
            if choice.finish_reason != "stop" or choice.message.refusal or not choice.message.content:
                raise RuntimeError("Luna refused or returned an incomplete response; candidate remains unscored")
            try:
                answer = _validate_answer(json.loads(choice.message.content))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Luna returned malformed JSON; candidate remains unscored") from exc
            saved = {
                "answer": answer, "model": response.model,
                "service_tier": response.service_tier,
                "prompt_version": result["prompt_version"], "prompt_sha256": prompt_hash,
                "assessment_date": as_of, "request_sha256": request_hash,
                "usage": response.usage.model_dump() if response.usage else {},
                "explanation_word_limit_exceeded": len(answer["evidence"].split()) > 50,
            }
            cache.parent.mkdir(parents=True, exist_ok=True)
            with cache.open("x", encoding="utf-8") as handle:
                json.dump(saved, handle, ensure_ascii=False)
            return saved, cache, False

    try:
        rows = await asyncio.gather(*(score_one(key, request) for key, request in requests.items()),
                                    return_exceptions=True)
    finally:
        if owned_client is not None:
            await owned_client.close()
    for row in rows:
        if isinstance(row, BaseException):
            raise row
        saved, cache, cached = row
        usage = saved["usage"]
        result["artifacts"].append(str(cache))
        result["cached_candidates"] += int(cached)
        result["requests"] += int(not cached)
        result["usage"]["pairs"] += 1
        result["usage"]["input_tokens"] += usage.get("prompt_tokens", 0)
        result["usage"]["output_tokens"] += usage.get("completion_tokens", 0)
        details = usage.get("prompt_tokens_details") or {}
        result["usage"]["cached_tokens"] += details.get("cached_tokens", 0) or 0
        result["usage"]["cache_write_tokens"] += details.get("cache_write_tokens", 0) or 0
        result["usage"]["reasoning_tokens"] += (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
    answers = {key: row[0]["answer"] for key, row in zip(requests, rows)}
    result["scores"] = [{"id": person_id, "score": answers[key]["rating"],
                         "evidence": answers[key]["evidence"], "basis": answers[key]["basis"]}
                        for person_id, key in candidate_requests.items()]
    return result
