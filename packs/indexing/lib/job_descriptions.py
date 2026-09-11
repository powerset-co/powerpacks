"""Deterministic job-description records and position matches."""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from packs.search.tech_skills import extract


_TAG_RE = re.compile(r"<[^>]+>")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_EMBEDDED_DESCRIPTION_RE = re.compile(
    r'"(?:descriptionPlainText|descriptionHtml|xcp_requisition_job_description|description)"\s*:\s*("(?:\\.|[^"\\])*")',
    re.IGNORECASE,
)
_SENIORITY_WORDS = {
    "associate", "chief", "entry", "executive", "founding", "head", "intern", "junior",
    "lead", "manager", "principal", "senior", "sr", "staff", "vice", "vp",
}
_TITLE_ALIASES = {
    "developer": "engineer",
    "development": "engineer",
    "engineering": "engineer",
}
MAX_POSTING_POSITION_GAP_DAYS = 3 * 365
MAX_DESCRIPTION_CHARS = 24_000
TITLE_ONLY_MAX_HEADCOUNT = 100
TITLE_ONLY_MATCH_WEIGHT = 0.35
WORK_MATCH_WEIGHT = 0.8
OUTSIDE_EMPLOYMENT_WEIGHT = 0.65


def normalize_domain(value: Any) -> str:
    domain = str(value or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain).split("/", 1)[0].split(":", 1)[0]
    return domain.removeprefix("www.").rstrip(".")


def clean_description(value: Any) -> str:
    text = str(value or "")
    if len(text) > MAX_DESCRIPTION_CHARS:
        embedded = []
        for match in _EMBEDDED_DESCRIPTION_RE.finditer(text):
            try:
                embedded.append(json.loads(match.group(1)))
            except json.JSONDecodeError:
                continue
        if embedded and len(max(embedded, key=len)) >= 200:
            text = max(embedded, key=len)
    text = html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)<(?:br|/p|/li|/div|/h[1-6])\s*/?>", "\n", text)
    text = _TAG_RE.sub(" ", text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def focused_description(
    description: Any, *, removal_quotes: Iterable[str] = (), source_sha256: str | None = None,
) -> str:
    """Apply reviewed model deletions to the full normalized source.

    Without a plan, preserve all normalized content; no semantic regex rules or
    implicit model calls run here. Plans contain exact nonoverlapping quotes and
    the SHA256 of clean_description(description). Validation protects source
    fidelity, not semantic correctness: callers must review model proposals.
    """
    result = clean_description(description)
    if isinstance(removal_quotes, (str, bytes)):
        raise ValueError("Semantic removal quotes must be a collection of strings")
    quotes = list(removal_quotes)
    if quotes or source_sha256 is not None:
        if source_sha256 != hashlib.sha256(result.encode()).hexdigest():
            raise ValueError("Semantic removal plan does not match normalized source")
        spans = []
        for quote in quotes:
            if not isinstance(quote, str) or not quote.strip() or quote not in result or result.find(quote) != result.rfind(quote):
                raise ValueError("Each semantic removal must match exactly one nonempty source quote")
            start = result.index(quote)
            spans.append((start, start + len(quote)))
        spans.sort()
        if any(right[0] < left[1] for left, right in zip(spans, spans[1:])):
            raise ValueError("Semantic removal quotes overlap")
        for start, end in reversed(spans):
            result = result[:start] + result[end:]
        result = re.sub(r"\n{3,}", "\n\n", "\n".join(
            re.sub(r"[ \t]+", " ", line).strip() for line in result.splitlines()
        )).strip()
        if not result:
            raise ValueError("Semantic removal plan removes the entire description")
    return result if len(result) <= MAX_DESCRIPTION_CHARS else ""


def retrieval_text(title: Any, description: Any) -> str:
    return "\n".join(part for part in [str(title or "").strip(), focused_description(description)] if part)


def word_tokens(text: Any) -> list[str]:
    words = _TOKEN_RE.findall(str(text or "").lower())
    return list(dict.fromkeys([*words, *(f"{words[i]} {words[i + 1]}" for i in range(len(words) - 1))]))


def title_terms(title: Any) -> set[str]:
    terms: set[str] = set()
    for word in _TOKEN_RE.findall(str(title or "").lower()):
        if word in _SENIORITY_WORDS or word in {"and", "of", "the"}:
            continue
        terms.add(_TITLE_ALIASES.get(word, word))
    return terms


def title_phrases(title: Any) -> set[tuple[str, str]]:
    words = [
        _TITLE_ALIASES.get(word, word)
        for word in _TOKEN_RE.findall(str(title or "").lower())
        if word not in (_SENIORITY_WORDS - {"manager"}) and word not in {"and", "of", "the"}
    ]
    return set(zip(words, words[1:]))


def title_match(left: Any, right: Any) -> tuple[float, str] | None:
    left_terms = title_terms(left)
    right_terms = title_terms(right)
    if not left_terms or not right_terms:
        return None
    if left_terms == right_terms:
        return 1.0, "title_exact"
    if title_phrases(left) & title_phrases(right):
        return 0.6, "title_phrase"
    shared = left_terms & right_terms
    score = 2 * len(shared) / (len(left_terms) + len(right_terms))
    if score < 0.6 or (len(shared) < 2 and min(len(left_terms), len(right_terms)) > 1):
        return None
    return round(score, 4), "title_overlap"


def _posted_epoch(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def job_description_record(row: dict[str, Any], operator_id: str = "local:user") -> dict[str, Any] | None:
    job_id = str(row.get("listing_id") or row.get("id") or "").strip()
    title = str(row.get("title") or "").strip()
    description = clean_description(row.get("description"))
    focused = focused_description(description)
    domain = normalize_domain(row.get("company") or row.get("company_domain"))
    if not job_id or not title or not domain or len(focused) < 200:
        return None
    text = "\n".join([title, focused])
    return {
        "id": job_id,
        "company_domain": domain,
        "title": title,
        "description": description,
        "retrieval_text": text,
        "word_tokens": word_tokens(text),
        "tech_skills": extract(text),
        "posted_date": str(row.get("posted_date") or ""),
        "url": str(row.get("url") or row.get("apply_url") or ""),
        "ats_provider": str(row.get("ats_provider") or ""),
        "is_open": bool(row.get("is_open", True)),
        "allowed_operator_ids": [operator_id],
    }


def posting_position_gap_days(job: dict[str, Any], position: dict[str, Any]) -> int | None:
    domain = normalize_domain(job.get("company_domain"))
    if not domain or domain != normalize_domain(position.get("company_domain")):
        return None
    posted_epoch = _posted_epoch(job.get("posted_date"))
    if posted_epoch is None and job.get("is_open"):
        posted_epoch = int(datetime.now(timezone.utc).timestamp())
    start_epoch = int(position.get("start_date_epoch") or 0)
    end_epoch = int(position.get("end_date_epoch") or 0)
    if posted_epoch is None or not start_epoch:
        return None
    if posted_epoch < start_epoch:
        gap_days = (start_epoch - posted_epoch) // 86_400
    elif end_epoch and posted_epoch > end_epoch:
        gap_days = (posted_epoch - end_epoch) // 86_400
    else:
        gap_days = 0
    return gap_days if gap_days <= MAX_POSTING_POSITION_GAP_DAYS else None


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def semantic_job_candidates(
    jobs: Iterable[dict[str, Any]],
    position: dict[str, Any],
    position_vector: list[float],
    *,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    norm = math.sqrt(sum(value * value for value in position_vector))
    if not norm:
        return []
    ranked = []
    for job in jobs:
        gap_days = posting_position_gap_days(job, position)
        vector = job.get("vector")
        if gap_days is None or not vector:
            continue
        job_norm = math.sqrt(sum(value * value for value in vector))
        if not job_norm:
            continue
        score = sum(left * right for left, right in zip(position_vector, vector, strict=True)) / (norm * job_norm)
        if gap_days:
            score *= OUTSIDE_EMPLOYMENT_WEIGHT
        ranked.append((score, job))
    ranked.sort(key=lambda pair: (-pair[0], str(pair[1]["id"])))
    unique = {}
    for _, job in ranked:
        unique.setdefault(_normalized_text(job.get("retrieval_text")), job)
    return list(unique.values())[:top_k]


def _supports_work(evidence: dict[str, Any], job: dict[str, Any], position: dict[str, Any]) -> bool:
    position_quote = _normalized_text(evidence.get("position_evidence")).strip("\"'“”‘’")
    job_quote = _normalized_text(evidence.get("jd_evidence")).strip("\"'“”‘’")
    title = _normalized_text(position.get("position_title") or position.get("raw_title"))
    return bool(
        position_quote and job_quote
        and position_quote in _normalized_text(position.get("description"))
        and position_quote not in title
        and job_quote in _normalized_text(job.get("retrieval_text"))
    )


def match_job_descriptions_to_positions(
    jobs: Iterable[dict[str, Any]],
    positions: Iterable[dict[str, Any]],
    *,
    work_matches: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    evidence_by_pair = {
        (str(row["job_description_id"]), str(row["position_id"])): row
        for row in work_matches or []
    }
    positions_by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for position in positions:
        domain = normalize_domain(position.get("company_domain"))
        if domain:
            positions_by_domain[domain].append(position)

    matches: list[dict[str, Any]] = []
    for job in jobs:
        job_id = str(job["id"])
        observed_open = _posted_epoch(job.get("posted_date")) is None and bool(job.get("is_open"))
        for position in positions_by_domain.get(normalize_domain(job.get("company_domain")), []):
            gap_days = posting_position_gap_days(job, position)
            if gap_days is None:
                continue
            position_id = str(position.get("position_id") or position.get("id") or "")
            person_id = str(position.get("person_id") or position.get("base_id") or "")
            if not position_id or not person_id:
                continue
            evidence = evidence_by_pair.get((job_id, position_id))
            if evidence and _supports_work(evidence, job, position):
                score, match_type = WORK_MATCH_WEIGHT, "work_semantic"
            elif 0 < (position.get("company_headcount") or 0) <= TITLE_ONLY_MAX_HEADCOUNT:
                matched = title_match(job.get("title"), position.get("position_title") or position.get("raw_title"))
                if not matched:
                    continue
                title_score, match_type = matched
                score = round(title_score * TITLE_ONLY_MATCH_WEIGHT, 4)
            else:
                continue
            if observed_open:
                match_type += "_observed_open"
            if gap_days:
                score = round(score * OUTSIDE_EMPLOYMENT_WEIGHT, 4)
            matches.append({
                "id": hashlib.sha256(f"{job_id}|{position_id}".encode()).hexdigest()[:24],
                "job_description_id": job_id,
                "position_id": position_id,
                "person_id": person_id,
                "company_domain": normalize_domain(job.get("company_domain")),
                "match_score": score,
                "match_type": match_type,
                "posting_position_gap_days": gap_days,
            })
    return sorted(matches, key=lambda row: (row["job_description_id"], -row["match_score"], row["position_id"]))
