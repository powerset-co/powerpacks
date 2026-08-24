"""Typed boundary for saved deep-search result and pond artifacts."""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

GROUPS = (
    ("send_worthy", "Send-worthy"),
    ("chat_worthy", "Chat-worthy"),
    ("wrong_timing_relationship", "Wrong timing / relationship"),
    ("passed", "Not a fit"),
)


@dataclass(frozen=True)
class TraitScore:
    name: str
    score: float
    confidence: float
    reason: str
    meaning: str = ""


@dataclass(frozen=True)
class PondCandidate:
    person_id: str
    title: str
    company: str
    location: str
    avatar_url: str
    final_score: float
    traits: tuple[TraitScore, ...]


@dataclass(frozen=True)
class CandidatePond:
    run_id: str
    pond_n: int
    query: str
    candidate: PondCandidate


@dataclass(frozen=True)
class Iteration:
    pond_n: int
    query: str
    candidates: tuple[PondCandidate, ...]

    def candidate(self, person_id: str) -> PondCandidate | None:
        return next((row for row in self.candidates if row.person_id == person_id), None)


@dataclass(frozen=True)
class Pond:
    run_id: str
    pond_n: int
    query: str
    diagnosis: str
    move: str
    good_count: int
    result_count: int
    cost_usd: float


@dataclass(frozen=True)
class Candidate:
    person_id: str
    name: str
    linkedin_url: str
    title: str
    company: str
    location: str
    avatar_url: str
    level: str
    timing: str
    pedigree: str
    move: str
    why: str
    found_run: str
    found_pond: int
    found_query: str
    queries: tuple[str, ...]
    ponds: tuple[CandidatePond, ...]

    def in_pond(self, run_id: str, pond_n: int) -> PondCandidate | None:
        return next((row.candidate for row in self.ponds
                     if row.run_id == run_id and row.pond_n == pond_n), None)


@dataclass(frozen=True)
class CandidateGroup:
    key: str
    label: str
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class SearchResult:
    run_id: str
    title: str
    company: str
    created_at: str
    total_cost_usd: float
    ponds: tuple[Pond, ...]
    groups: tuple[CandidateGroup, ...]
    jd_text: str

    @property
    def queries(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(pond.query for pond in self.ponds if pond.query))

    def candidate(self, person_id: str) -> Candidate | None:
        for group in self.groups:
            hit = next((row for row in group.candidates if row.person_id == person_id), None)
            if hit is not None:
                return hit
        return None


@dataclass(frozen=True)
class _RawRun:
    run_id: str
    payload: dict[str, Any]
    iterations: tuple[Iteration, ...]


def _text(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _artifact_path(root: Path, value: Any) -> Path | None:
    raw = _text(value)
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else root.parent.parent / path


def _jsonl_rows(path: Path | None) -> Iterable[dict[str, Any]]:
    if path is None or not path.exists():
        return ()
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return tuple(json.loads(line) for line in handle if line.strip())


def _traits(value: Any) -> tuple[TraitScore, ...]:
    if isinstance(value, str):
        value = json.loads(value) if value.strip() else {}
    if not isinstance(value, dict):
        return ()
    traits: list[TraitScore] = []
    for name, raw in value.items():
        if isinstance(raw, dict):
            score = _number(raw.get("score"))
            confidence = _number(raw.get("confidence"))
            reason = _text(raw.get("reason"))
        else:
            score, confidence, reason = _number(raw), 0.0, ""
        traits.append(TraitScore(_text(name), score, confidence, reason))
    return tuple(traits)


def _pond_candidates(root: Path, iteration: dict[str, Any],
                     wanted: frozenset[str]) -> tuple[PondCandidate, ...]:
    artifacts = ((iteration.get("arm") or {}).get("artifacts") or {})
    result_path = _artifact_path(root, artifacts.get("jsonl"))
    profile_path = _artifact_path(root, artifacts.get("profiles_path"))
    result_rows = {
        _text(row.get("person_id")): row
        for row in _jsonl_rows(result_path)
        if _text(row.get("person_id")) in wanted
    }
    avatars = {
        _text(row.get("person_id")): _text(row.get("profile_picture_url"))
        for row in _jsonl_rows(profile_path)
        if _text(row.get("person_id")) in wanted
    }
    return tuple(
        PondCandidate(
            person_id=person_id,
            title=_text(row.get("current_titles")),
            company=_text(row.get("current_companies")),
            location=_text(row.get("location")),
            avatar_url=avatars.get(person_id, ""),
            final_score=_number(row.get("final_score")),
            traits=_traits(row.get("trait_scores")),
        )
        for person_id, row in result_rows.items()
    )


def _wanted_people(summary_payloads: Iterable[dict[str, Any]],
                   ) -> dict[tuple[str, int], frozenset[str]]:
    wanted: dict[tuple[str, int], set[str]] = {}
    for payload in summary_payloads:
        groups = ((payload.get("summary") or {}).get("groups") or {})
        for rows in groups.values():
            for row in rows or []:
                person_id = _text(row.get("person"))
                for found in row.get("found_by") or []:
                    key = (_text(found.get("run")), int(found.get("pond") or 0))
                    wanted.setdefault(key, set()).add(person_id)
    return {key: frozenset(values) for key, values in wanted.items()}


def _parse_iterations(root: Path, run_id: str, payload: dict[str, Any],
                      wanted: dict[tuple[str, int], frozenset[str]],
                      ) -> tuple[Iteration, ...]:
    iterations: list[Iteration] = []
    candidate_cache: dict[tuple[str, str], tuple[PondCandidate, ...]] = {}
    for raw in payload.get("iterations") or []:
        pond_n = int(raw.get("pond_n") or 0)
        artifacts = ((raw.get("arm") or {}).get("artifacts") or {})
        cache_key = (_text(artifacts.get("jsonl")), _text(artifacts.get("profiles_path")))
        candidates = candidate_cache.get(cache_key)
        if candidates is None:
            candidates = _pond_candidates(root, raw, wanted.get((run_id, pond_n), frozenset()))
            candidate_cache[cache_key] = candidates
        iterations.append(Iteration(
            pond_n=pond_n,
            query=_text(raw.get("query")),
            candidates=candidates,
        ))
    return tuple(iterations)


def _candidate(raw: dict[str, Any], raw_runs: dict[str, _RawRun]) -> Candidate:
    person_id = _text(raw.get("person"))
    found_by = raw.get("found_by") or []
    sources: list[CandidatePond] = []
    queries: list[str] = []
    for found in found_by:
        run_id = _text(found.get("run"))
        pond_n = int(found.get("pond") or 0)
        query = _text(found.get("query"))
        if query:
            queries.append(query)
        run = raw_runs.get(run_id)
        if run is None:
            continue
        for iteration in run.iterations:
            if iteration.pond_n != pond_n:
                continue
            hit = iteration.candidate(person_id)
            if hit is not None:
                sources.append(CandidatePond(run_id, pond_n, query or iteration.query, hit))
    best = max(sources, key=lambda item: item.candidate.final_score, default=None)
    pond_row = best.candidate if best else None
    return Candidate(
        person_id=person_id,
        name=_text(raw.get("name")),
        linkedin_url=_text(raw.get("linkedin_url")),
        title=(pond_row.title if pond_row and pond_row.title else _text(raw.get("title"))),
        company=(pond_row.company if pond_row and pond_row.company else _text(raw.get("company"))),
        location=pond_row.location if pond_row else "",
        avatar_url=pond_row.avatar_url if pond_row else "",
        level=_text(raw.get("level")),
        timing=_text(raw.get("timing")),
        pedigree=_text(raw.get("pedigree_prior")),
        move=_text(raw.get("move_plausibility")),
        why=_text(raw.get("why")),
        found_run=best.run_id if best else (_text(found_by[0].get("run")) if found_by else ""),
        found_pond=best.pond_n if best else (int(found_by[0].get("pond") or 0) if found_by else 0),
        found_query=best.query if best else (_text(found_by[0].get("query")) if found_by else ""),
        queries=tuple(dict.fromkeys(queries)),
        ponds=tuple(sources),
    )


def _search(root: Path, run_id: str, payload: dict[str, Any],
            raw_runs: dict[str, _RawRun]) -> SearchResult:
    summary = payload["summary"]
    good_people: dict[tuple[str, int], set[str]] = {}
    for key in ("send_worthy", "chat_worthy"):
        for candidate in (summary.get("groups") or {}).get(key) or []:
            for found in candidate.get("found_by") or []:
                pond = (_text(found.get("run")), int(found.get("pond") or 0))
                good_people.setdefault(pond, set()).add(_text(candidate.get("person")))
    ponds: list[Pond] = []
    for raw in summary.get("pond_chain") or []:
        source_run = _text(raw.get("run"))
        pond_n = int(raw.get("pond_n") or 0)
        ponds.append(Pond(
            run_id=source_run,
            pond_n=pond_n,
            query=_text(raw.get("query")),
            diagnosis=_text(raw.get("diagnosis")),
            move=_text(raw.get("move")),
            good_count=len(good_people.get((source_run, pond_n), set())),
            result_count=int(raw.get("result_count") or 0),
            cost_usd=_number(raw.get("cost_usd")),
        ))
    raw_groups = summary.get("groups") or {}
    groups = tuple(CandidateGroup(
        key=key,
        label=label,
        candidates=tuple(_candidate(row, raw_runs)
                         for row in raw_groups.get(key) or []),
    ) for key, label in GROUPS)
    jd_path = root / run_id / "jd.txt"
    return SearchResult(
        run_id=run_id,
        title=_text(payload.get("title")) or run_id,
        company=_text(payload.get("company") or payload.get("hiring_company")),
        created_at=_text(payload.get("created_at") or payload.get("updated_at")),
        total_cost_usd=_number(summary.get("total_cost_usd")),
        ponds=tuple(ponds),
        groups=groups,
        jd_text=jd_path.read_text(encoding="utf-8").strip() if jd_path.is_file() else "",
    )


def load_searches(root: Path) -> tuple[SearchResult, ...]:
    """Read all direct child results; only summary-bearing runs become searches."""
    payloads: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*/results.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payloads[path.parent.name] = payload
    summaries = [payload for payload in payloads.values()
                 if isinstance(payload.get("summary"), dict)]
    wanted = _wanted_people(summaries)
    raw_runs = {
        run_id: _RawRun(
            run_id, payload, _parse_iterations(root, run_id, payload, wanted),
        )
        for run_id, payload in payloads.items()
    }
    searches = tuple(
        _search(root, run_id, payload, raw_runs)
        for run_id, payload in payloads.items()
        if isinstance(payload.get("summary"), dict)
    )
    return tuple(sorted(searches, key=lambda search: (search.created_at, search.run_id),
                        reverse=True))
