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
