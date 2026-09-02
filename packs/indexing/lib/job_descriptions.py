"""Deterministic job-description records and position matches."""

from __future__ import annotations

import hashlib
import html
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from packs.search.tech_skills import extract


_TAG_RE = re.compile(r"<[^>]+>")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ROLE_HEADINGS = re.compile(
    r"^(?:about (?:the|this|our) (?:role|job|opportunity)|the role|role overview|"
    r"what you(?:'|’)ll do|what you will do|responsibilities|your responsibilities|"
    r"requirements|qualifications|minimum qualifications|preferred qualifications|"
    r"what you(?:'|’)ll need|what you will need|who you are|nice to have|bonus points)$",
    re.IGNORECASE,
)
_STOP_HEADINGS = re.compile(
    r"^(?:about (?:us|the company|our company|our team)|benefits|perks|compensation|salary|"
    r"equal opportunity|diversity|how to apply|application process)$",
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


def normalize_domain(value: Any) -> str:
    domain = str(value or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain).split("/", 1)[0].split(":", 1)[0]
    return domain.removeprefix("www.").rstrip(".")


def clean_description(value: Any) -> str:
    text = html.unescape(str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)<(?:br|/p|/li|/div|/h[1-6])\s*/?>", "\n", text)
    text = _TAG_RE.sub(" ", text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _heading(line: str) -> str:
    return re.sub(r"[^a-z0-9'’ ]+", "", line.lower().strip().rstrip(":"))


def focused_description(description: Any) -> str:
    """Keep role, responsibility, and requirement sections when they exist."""

    cleaned = clean_description(description)
    selected: list[str] = []
    include = False
    found = False
    for line in cleaned.splitlines():
        heading = _heading(line)
        if _ROLE_HEADINGS.fullmatch(heading):
            include = True
            found = True
            selected.append(line)
        elif _STOP_HEADINGS.fullmatch(heading):
            include = False
        elif include:
            selected.append(line)
    focused = "\n".join(selected).strip()
    return focused if found and len(focused) >= 200 else cleaned


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
    domain = normalize_domain(row.get("company") or row.get("company_domain"))
    if not job_id or not title or not domain or len(description) < 200:
        return None
    text = retrieval_text(title, description)
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


def match_job_descriptions_to_positions(
    jobs: Iterable[dict[str, Any]],
    positions: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    positions_by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for position in positions:
        domain = normalize_domain(position.get("company_domain"))
        if domain:
            positions_by_domain[domain].append(position)

    matches: list[dict[str, Any]] = []
    for job in jobs:
        posted_epoch = _posted_epoch(job.get("posted_date"))
        observed_open = posted_epoch is None and bool(job.get("is_open"))
        if observed_open:
            posted_epoch = int(datetime.now(timezone.utc).timestamp())
        if posted_epoch is None:
            continue
        for position in positions_by_domain.get(normalize_domain(job.get("company_domain")), []):
            start_epoch = int(position.get("start_date_epoch") or 0)
            end_epoch = int(position.get("end_date_epoch") or 0)
            if not start_epoch:
                continue
            if posted_epoch < start_epoch:
                gap_days = (start_epoch - posted_epoch) // 86_400
            elif end_epoch and posted_epoch > end_epoch:
                gap_days = (posted_epoch - end_epoch) // 86_400
            else:
                gap_days = 0
            if gap_days > MAX_POSTING_POSITION_GAP_DAYS:
                continue
            matched = title_match(job.get("title"), position.get("position_title") or position.get("raw_title"))
            if not matched:
                continue
            score, match_type = matched
            if observed_open:
                match_type += "_observed_open"
            if gap_days:
                score = round(score * 0.65, 4)
            job_id = str(job["id"])
            position_id = str(position.get("position_id") or position.get("id") or "")
            person_id = str(position.get("person_id") or position.get("base_id") or "")
            if not position_id or not person_id:
                continue
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
