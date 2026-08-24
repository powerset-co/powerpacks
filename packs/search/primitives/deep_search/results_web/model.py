"""Typed boundary for saved deep-search result and pond artifacts."""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

GROUPS = (
    ("send_worthy", "Send-worthy"),
    ("chat_worthy", "Chat-worthy"),
    ("wrong_timing_relationship", "Wrong timing / relationship"),
    ("passed", "Passed"),
)
SCHEMA_VERSION = "search-harness.v1"


@dataclass(frozen=True)
class TraitScore:
    name: str
    score: float
    confidence: float
    reason: str
    meaning: str = ""


@dataclass(frozen=True)
class EvaluationTrait:
    name: str
    meaning: str


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
class Iteration:
    pond_n: int
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
    pond_score: float
    jd_score: float
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
    pond_traits: tuple[TraitScore, ...]
    jd_traits: tuple[TraitScore, ...]


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

    @property
    def queries(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(pond.query for pond in self.ponds if pond.query))

    def candidate(self, person_id: str) -> Candidate | None:
        for group in self.groups:
            hit = next((row for row in group.candidates if row.person_id == person_id), None)
            if hit is not None:
                return hit
        return None

    @property
    def has_jd_ranking(self) -> bool:
        return any(candidate.jd_traits for group in self.groups for candidate in group.candidates)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float:
    return float(value)


def _artifact_path(root: Path, value: str) -> Path:
    return root.parent.parent / value


def _jsonl_rows(path: Path) -> Iterable[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return tuple(json.loads(line) for line in handle if line.strip())


def _traits(value: str) -> tuple[TraitScore, ...]:
    return tuple(
        TraitScore(_text(name), _number(raw["score"]), _number(raw["confidence"]),
                   _text(raw["reason"]))
        for name, raw in json.loads(value).items()
    )


def _evaluation_traits(path: Path) -> tuple[EvaluationTrait, ...]:
    return tuple(
        EvaluationTrait(_text(row["value"]), _text(row["meaning"]))
        for row in json.loads(path.read_text(encoding="utf-8"))
    )


def _ordered_jd_traits(traits: tuple[TraitScore, ...],
                       evaluation: tuple[EvaluationTrait, ...]) -> tuple[TraitScore, ...]:
    contract = {trait.name: (trait.meaning, index)
                for index, trait in enumerate(evaluation)}
    meaning_order = {"core": 0, "nice-to-have": 1}
    return tuple(
        replace(trait, meaning=contract[trait.name][0])
        for trait in sorted(traits, key=lambda trait: (
            meaning_order[contract[trait.name][0]], contract[trait.name][1],
        ))
    )


def _pond_candidates(root: Path, iteration: dict[str, Any],
                     wanted: frozenset[str]) -> tuple[PondCandidate, ...]:
    artifacts = iteration["arm"]["artifacts"]
    result_path = _artifact_path(root, artifacts["jsonl"])
    profile_path = _artifact_path(root, artifacts["profiles_path"])
    result_rows = {
        _text(row["person_id"]): row
        for row in _jsonl_rows(result_path)
        if _text(row["person_id"]) in wanted
    }
    avatars = {
        _text(row["person_id"]): _text(row["profile_picture_url"])
        for row in _jsonl_rows(profile_path)
        if _text(row["person_id"]) in wanted
    }
    return tuple(
        PondCandidate(
            person_id=person_id,
            title=_text(row["current_titles"]),
            company=_text(row["current_companies"]),
            location=_text(row["location"]),
            avatar_url=avatars[person_id],
            final_score=_number(row["final_score"]),
            traits=_traits(row["trait_scores"]),
        )
        for person_id, row in result_rows.items()
    )


def _wanted_people(summary_payloads: Iterable[dict[str, Any]],
                   ) -> dict[tuple[str, int], frozenset[str]]:
    wanted: dict[tuple[str, int], set[str]] = {}
    for payload in summary_payloads:
        groups = payload["summary"]["groups"]
        for rows in groups.values():
            for row in rows:
                person_id = _text(row["person"])
                for found in row["found_by"]:
                    key = (_text(found["run"]), int(found["pond"]))
                    wanted.setdefault(key, set()).add(person_id)
    return {key: frozenset(values) for key, values in wanted.items()}


def _parse_iterations(root: Path, run_id: str, payload: dict[str, Any],
                      wanted: dict[tuple[str, int], frozenset[str]],
                      ) -> tuple[Iteration, ...]:
    iterations: list[Iteration] = []
    candidate_cache: dict[tuple[str, str], tuple[PondCandidate, ...]] = {}
    for raw in payload["iterations"]:
        pond_n = int(raw["pond_n"])
        artifacts = raw["arm"]["artifacts"]
        cache_key = (_text(artifacts["jsonl"]), _text(artifacts["profiles_path"]))
        candidates = candidate_cache.get(cache_key)
        if candidates is None:
            candidates = _pond_candidates(root, raw, wanted.get((run_id, pond_n), frozenset()))
            candidate_cache[cache_key] = candidates
        iterations.append(Iteration(
            pond_n=pond_n,
            candidates=candidates,
        ))
    return tuple(iterations)


def _candidate(raw: dict[str, Any], raw_runs: dict[str, tuple[Iteration, ...]],
               evaluation: tuple[EvaluationTrait, ...], *, jd_ranking: bool) -> Candidate:
    person_id = _text(raw["person"])
    found_by = raw["found_by"]
    sources: list[tuple[PondCandidate, str, int, str]] = []
    queries: list[str] = []
    for found in found_by:
        run_id = _text(found["run"])
        pond_n = int(found["pond"])
        query = _text(found["query"])
        if query:
            queries.append(query)
        for iteration in raw_runs[run_id]:
            if iteration.pond_n != pond_n:
                continue
            hit = iteration.candidate(person_id)
            if hit is not None:
                sources.append((hit, run_id, pond_n, query))
    best = max(sources, key=lambda item: item[0].final_score)
    pond_row = best[0]
    jd_traits = (_ordered_jd_traits(_traits(raw["jd_trait_scores"]), evaluation)
                 if jd_ranking else ())
    return Candidate(
        person_id=person_id,
        name=_text(raw["name"]),
        linkedin_url=_text(raw["linkedin_url"]),
        pond_score=pond_row.final_score,
        jd_score=_number(raw["anchored_score"]),
        title=pond_row.title,
        company=pond_row.company,
        location=pond_row.location,
        avatar_url=pond_row.avatar_url,
        level=_text(raw["level"]),
        timing=_text(raw["timing"]),
        pedigree=_text(raw["pedigree_prior"]),
        move=_text(raw["move_plausibility"]),
        why=_text(raw["why"]),
        found_run=best[1],
        found_pond=best[2],
        found_query=best[3],
        queries=tuple(dict.fromkeys(queries)),
        pond_traits=pond_row.traits,
        jd_traits=jd_traits,
    )


def _search(root: Path, run_id: str, payload: dict[str, Any],
            raw_runs: dict[str, tuple[Iteration, ...]]) -> SearchResult:
    summary = payload["summary"]
    good_people: dict[tuple[str, int], set[str]] = {}
    for key in ("send_worthy", "chat_worthy"):
        for candidate in summary["groups"][key]:
            for found in candidate["found_by"]:
                pond = (_text(found["run"]), int(found["pond"]))
                good_people.setdefault(pond, set()).add(_text(candidate["person"]))
    ponds: list[Pond] = []
    for raw in summary["pond_chain"]:
        source_run = _text(raw["run"])
        pond_n = int(raw["pond_n"])
        ponds.append(Pond(
            run_id=source_run,
            pond_n=pond_n,
            query=_text(raw["query"]),
            diagnosis=_text(raw["diagnosis"]),
            move=_text(raw["move"]),
            good_count=len(good_people.get((source_run, pond_n), set())),
            result_count=int(raw["result_count"]),
            cost_usd=_number(raw["cost_usd"]),
        ))
    raw_groups = summary["groups"]
    has_jd_scores = any("jd_trait_scores" in row for rows in raw_groups.values() for row in rows)
    evaluation = (_evaluation_traits(root / run_id / "evaluation-traits.json")
                  if has_jd_scores else ())
    groups = tuple(CandidateGroup(
        key=key,
        label=label,
        candidates=tuple(_candidate(row, raw_runs, evaluation, jd_ranking=has_jd_scores)
                         for row in raw_groups[key]),
    ) for key, label in GROUPS)
    return SearchResult(
        run_id=run_id,
        title=_text(payload["title"]),
        company=_text(payload["company"]),
        created_at=_text(payload["created_at"]),
        total_cost_usd=_number(summary["total_cost_usd"]),
        ponds=tuple(ponds),
        groups=groups,
    )


def load_searches(root: Path) -> tuple[SearchResult, ...]:
    """Read all direct child results; only summary-bearing runs become searches."""
    payloads: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*/results.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "iterations" not in payload:
            continue
        if payload["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"{path}: expected schema_version {SCHEMA_VERSION}")
        payloads[path.parent.name] = payload
    summaries = [payload for payload in payloads.values() if "summary" in payload]
    wanted = _wanted_people(summaries)
    raw_runs = {
        run_id: _parse_iterations(root, run_id, payload, wanted)
        for run_id, payload in payloads.items()
    }
    searches = tuple(
        _search(root, run_id, payload, raw_runs)
        for run_id, payload in payloads.items()
        if "summary" in payload
    )
    return tuple(sorted(searches, key=lambda search: (search.created_at, search.run_id),
                        reverse=True))
