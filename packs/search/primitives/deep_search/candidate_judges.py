"""Independent qualification and opportunity judgments over original profile evidence."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from packs.search.primitives.lib.cached_prompt import cached_messages
from packs.search.primitives.llm_rerank_candidates.cross_encoder import profile_evidence

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
JUDGE_CONFIG = {"model": "gpt-5.6-terra", "reasoning_effort": "medium",
                "service_tier": "flex", "max_completion_tokens": 2500,
                "response_format": {"type": "json_object"},
                "extra_body": {"prompt_cache_options": {"mode": "explicit"}}}
JUDGE_GUIDANCE = (
    "Apply this stage's rubric; capability ratings, domain ratings, and opportunity caps are not interchangeable. "
    "Identify the central work before comparing evidence. Distinguish demonstrated responsibilities, credible contextual "
    "inference, and missing detail. Evaluate equivalent methods rather than exact terminology. A missing tool name is not "
    "a missing capability when equivalent work is supported; generic profession or employer association alone is not proof "
    "of an essential specialty. Sparse descriptions do not establish inability or a career switch. Consider relevant "
    "historical work and supported continuity. Keep technical qualification separate from opportunity scope. Explain the "
    "decisive evidence in plain English without narrating adjacent ratings; do not invent accomplishments, personal intent, "
    "or company facts.\n")
OPPORTUNITY_GUIDANCE = (
    "Distinguish technical/project leadership from demonstrated people or organizational management. Architecture ownership "
    "and missing current coding detail alone do not establish an organizational-scope reduction. Explain the actual "
    "responsibility being lost before imposing a cap.\n")
_OPPORTUNITY_PROPERTIES = {
    "why": {"type": "string"}, "cap": {"type": "integer", "enum": [2, 3, 5]},
    "current_scope": {"type": "string"}, "target_scope": {"type": "string"},
    "company_context": {"type": "string"},
    "missing_facts": {"type": "array", "items": {"type": "string"}},
}


def candidate_judge_messages(*, dimension: str, jd: str, candidate: Mapping[str, Any],
                             hiring_company: Mapping[str, Any], pond_query: str,
                             target_level: Any = None, comp_band: Any = None,
                             as_of: str | None = None) -> list[dict[str, Any]]:
    shared = {
        "as_of": as_of or date.today().isoformat(), "job_description": jd,
        "pond_query": pond_query, "target_level": target_level, "comp_band": comp_band,
        "hiring_company": {key: value for key, value in hiring_company.items()
                           if key != "pull_note" and value not in (None, "", [])},
    }
    evidence = {
        "candidate": profile_evidence(dict(candidate)),
        "current_company": {key.removeprefix("current_company_"): value
                            for key, value in candidate.items()
                            if key.startswith("current_company_") and value not in (None, "", [])},
    }
    return cached_messages(
        (PROMPTS / f"{dimension}-judge.txt").read_text(),
        json.dumps(shared, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False))


def candidate_judge_request(*, dimension: str, opportunity_review: bool = False,
                            **inputs: Any) -> dict[str, Any]:
    request = {**JUDGE_CONFIG, "messages": candidate_judge_messages(dimension=dimension, **inputs)}
    if dimension == "opportunity" and not opportunity_review:
        request.update(model="gpt-5.6-luna", reasoning_effort="low", response_format={
            "type": "json_schema", "json_schema": {"name": "candidate_judge", "strict": True,
                "schema": {"type": "object", "properties": _OPPORTUNITY_PROPERTIES,
                           "required": list(_OPPORTUNITY_PROPERTIES), "additionalProperties": False}}})
        request["messages"][0]["content"] += "\n\n" + JUDGE_GUIDANCE + "\n\n" + OPPORTUNITY_GUIDANCE
    return request


def parse_candidate_judge(raw: str, dimension: str) -> dict[str, Any]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Judge response must be an object")
    score_key = "score" if dimension == "domain" else "cap"
    score = payload.get(score_key)
    allowed = (1, 2, 3, 4, 5) if dimension == "domain" else (2, 3, 5)
    if not (dimension == "domain" and score is None and "score" in payload):
        if type(score) is not int or score not in allowed:
            raise ValueError(f"{dimension} requires an integer {score_key}")
    text_fields = ("why",) if dimension == "domain" else (
        "why", "current_scope", "target_scope", "company_context")
    list_fields = ("evidence", "concerns") if dimension == "domain" else ("missing_facts",)
    for key in text_fields:
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ValueError(f"{dimension} requires {key}")
    for key in list_fields:
        if not isinstance(payload.get(key), list) or any(not isinstance(item, str) for item in payload[key]):
            raise ValueError(f"{dimension} requires a string list for {key}")
    return {key: payload[key] for key in (score_key, *text_fields, *list_fields)}
