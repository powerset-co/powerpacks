"""Deterministic input and prompt contract for capability scoring.

Changelog:
  2026-09-25: the Jev spec is the seven profile-level questions and the evidence policy;
    the rating rubric and per-position question template are gone with the request.
"""

from __future__ import annotations

import hashlib
import json

from packs.search.primitives.clean_job_description import clean_job_description as jd_cleaner
from packs.search.primitives.llm_rerank_candidates import terra
from packs.search.primitives.llm_rerank_candidates.jev import model as jev_model
from packs.search.primitives.llm_rerank_candidates.jev.questions import (
    EVIDENCE_POLICY,
    REQUEST_VERSION,
    base_questions,
)

# The judge a JD run uses unless the caller names one; trait reranking without a JD has none.
DEFAULT_JUDGE = "jev"


def _prompt_spec(judge: str, rubric: str | None) -> dict:
    if judge == "terra":
        return {
            "judge": judge,
            "model": terra.MODEL,
            "prompt_version": terra.PROMPT_VERSION,
            "rating_rubric": rubric,
        }
    if judge != "jev":
        raise ValueError("capability judge must be 'terra' or 'jev'")
    return {
        "judge": judge,
        "model": jev_model.MODEL_ID,
        "request_version": REQUEST_VERSION,
        "prompt_version": jev_model.PROMPT_VERSION,
        "question_version": jev_model.QUESTION_VERSION,
        "model_asset_sha256": hashlib.sha256(jev_model.MODEL_ASSET.read_bytes()).hexdigest(),
        "evidence_policy": EVIDENCE_POLICY,
        "questions": base_questions(),
    }


def prompt_spec(*, judge: str, as_of: str) -> dict:
    """Describe the exact scorer prompt and schema for a run artifact."""
    normalized_judge = judge.strip().casefold()
    rubric = terra.system_prompt(as_of) if normalized_judge == "terra" else None
    return _prompt_spec(normalized_judge, rubric)


def request_sha256(*, jd: str, title: str, company_name: str, evaluation_query: str, judge: str) -> str:
    """Hash all normalized inputs and frozen definitions that determine capability scores."""
    if not isinstance(jd, str) or not jd.strip():
        raise ValueError("capability contract requires source JD text")
    if not all(isinstance(value, str) for value in (title, company_name, evaluation_query, judge)):
        raise TypeError("capability contract inputs must be strings")
    cleaner_request = jd_cleaner.build_request(
        jd=jd.strip(),
        title=" ".join(title.split()) or "Not stated",
        company_name=" ".join(company_name.split()) or "Not stated",
    )
    normalized_judge = judge.strip().casefold()
    rubric_template = (terra.PROMPT.read_text(encoding="utf-8").rstrip("\n")
                       if normalized_judge == "terra" else None)
    payload = {
        "cleaner_request": cleaner_request,
        "evaluation_query": evaluation_query.strip(),
        "scorer": _prompt_spec(normalized_judge, rubric_template),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
