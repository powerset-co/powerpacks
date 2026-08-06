"""Pure, in-process recruiting stages over the typed search contracts."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

import jsonschema

from packs.search.primitives.deep_search.plan_critic import deterministic_checks
from packs.search.primitives.deep_search.build_eval_inputs import plan_from_obj
from packs.search.primitives.deep_search.recruiter_policy import (
    resolve_recruiter_preferences,
    validate_resolved_recruiter_preferences,
)
from packs.search.primitives.evaluate_profile_candidates.evaluate_profile_candidates import (
    STATUS_VALUE,
    profile_is_current_founder_c_suite,
)
from packs.search.reflect.snapshots import canonical_hash

from .frontier import CandidateRecord
from .models import SearchSpec

PROBE_FAMILIES = (
    "title_function",
    "demonstrated_work_systems",
    "company_archetype",
    "adjacent_role_vocabulary",
    "differentiated_core_bonus",
)


class TransientJudgeError(RuntimeError):
    """A candidate judge failure which may be retried exactly once."""


class JudgeBudgetExceeded(RuntimeError):
    """A paid judge attempt was blocked before the provider call."""


def normalize_jd(value: str) -> str:
    text = re.sub(r"\r\n?", "\n", value).strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) < 80:
        raise ValueError("recruiting source is too thin; provide the complete role brief or JD")
    return text


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def build_review_plan(
    spec: SearchSpec,
    extracted: Mapping[str, Any],
    *,
    created_at: str,
    source_url: str | None = None,
) -> dict[str, Any]:
    """Normalize model extraction through the established strict recruiter-plan contract."""
    corpus = spec.corpus.to_dict()
    plan = plan_from_obj(
        dict(extracted),
        set_name="local" if spec.backend.value == "local" else "powerset",
        set_id=str(corpus.get("set_id") or "local"),
        source_url=source_url,
        created_at=created_at,
        user_preferences=dict(spec.recruiting.user_preferences),
    )
    filters = {
        key: list(getattr(spec.person_filters, key))
        for key in ("cities", "states", "countries", "metro_areas")
        if getattr(spec.person_filters, key)
    }
    if filters:
        from packs.search.primitives.deep_search.location_scope import (
            canonical_location_label,
            canonicalize_location_filters,
        )

        if ("cities" in filters or "states" in filters) and "countries" not in filters:
            plan_countries = plan["search_scope"]["filters"].get("countries") or []
            if len(plan_countries) != 1:
                raise ValueError("user city/state location filters require one reviewed country qualifier")
            filters["countries"] = list(plan_countries)
        filters = canonicalize_location_filters(filters)

        plan["search_scope"] = {
            "location": canonical_location_label(filters),
            "filters": filters,
            "source": "user",
        }
    return plan


def review_binding(
    spec: SearchSpec, plan: Mapping[str, Any], jd: str, corpus_snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    policy = plan["recruiter_policy"]
    stable_corpus = {key: value for key, value in corpus_snapshot.items() if key != "observed_at"}
    return {
        "schema_version": "recruiting.review-binding.v1",
        "plan_sha256": canonical_hash(plan),
        "jd_sha256": canonical_hash(jd),
        "source_sha256": canonical_hash(spec.recruiting.source),
        "corpus_sha256": canonical_hash(stable_corpus),
        "corpus": stable_corpus,
        "review_pool_person_ids": list(spec.recruiting.review_pool_person_ids),
        "review_pool_person_ids_sha256": canonical_hash(list(spec.recruiting.review_pool_person_ids)),
        "policy_id": policy["policy_id"],
        "policy_version": policy["policy_version"],
    }


def validate_review_plan(spec: SearchSpec, plan: Mapping[str, Any]) -> tuple[str, ...]:
    schema = Path(__file__).resolve().parents[1] / "schemas" / "search-network-jd-plan.schema.json"
    jsonschema.validate(dict(plan), json.loads(schema.read_text(encoding="utf-8")))
    validate_resolved_recruiter_preferences(plan["recruiter_policy"])
    issues = deterministic_checks(dict(plan), backend=spec.backend.value)
    return tuple(issues)


def generate_initial_probes(spec: SearchSpec, plan: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    titles = ", ".join(spec.role.titles or spec.role.role_ids) or plan["normalized_archetype"]
    core = ", ".join(
        item["trait"] for item in plan["traits"]["must_have"] if item.get("tier") == "core"
    )
    company = ", ".join(spec.company_filters.company_names or spec.company_filters.sector_types)
    bonus = ", ".join(item["trait"] for item in plan["traits"].get("nice_to_have", ()))
    queries = (
        f"{titles}; current and recent role function",
        f"demonstrated systems and shipped work: {core or titles}",
        f"company archetype and operating environment: {company or core or titles}",
        f"adjacent role vocabulary and transferable ownership around {titles}",
        f"differentiated core {core or titles}; bonus {bonus or 'adjacent high-signal experience'}",
    )
    return tuple(
        {"probe_id": f"initial-{index + 1:02d}", "probe_family": family, "query": query}
        for index, (family, query) in enumerate(zip(PROBE_FAMILIES, queries))
    )


def triage_candidate(candidate: CandidateRecord, threshold: float) -> CandidateRecord:
    profile = candidate.hydrated_profile or {}
    evidence = " ".join(
        str(value or "")
        for value in (
            profile.get("current_title"),
            profile.get("summary"),
            candidate.structured.get("position_title"),
        )
    ).strip()
    score = max(candidate.retrieval_score, candidate.deterministic_score)
    if score >= max(threshold, 0.5):
        disposition = "keep"
    elif score < threshold and not evidence:
        disposition = "drop"
    else:
        disposition = "maybe"
    return replace(candidate, triage={"disposition": disposition, "score": score, "evidence_present": bool(evidence)})


def judge_candidate(
    candidate: CandidateRecord,
    plan: Mapping[str, Any],
    adapter: Callable[[CandidateRecord, Mapping[str, Any]], Mapping[str, Any]],
    max_attempts: int = 2,
    before_attempt: Callable[[], bool] | None = None,
) -> tuple[CandidateRecord, int]:
    calls = 0
    for attempt in range(max_attempts):
        if before_attempt is not None and not before_attempt():
            raise JudgeBudgetExceeded("recruiting judge spend budget reached")
        calls += 1
        try:
            result = dict(adapter(candidate, plan))
            result.setdefault("status", "judged")
            result["attempts"] = attempt + 1
            return replace(candidate, judge=result), calls
        except TransientJudgeError as exc:
            if attempt == max_attempts - 1:
                return replace(
                    candidate,
                    judge={"status": "error", "error": str(exc), "attempts": max_attempts, "reviewable": True},
                ), calls
        except JudgeBudgetExceeded:
            raise
        except Exception as exc:  # persistent/non-transient failures are never rejection
            return replace(
                candidate,
                judge={"status": "error", "error": str(exc), "attempts": 1, "reviewable": True},
            ), calls
    raise AssertionError("unreachable")


def _core_group_met(judge: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    by_trait = {
        _norm(item.get("trait")): STATUS_VALUE.get(str(item.get("status") or "unknown"), 0.0)
        for item in judge.get("must_have") or []
        if isinstance(item, Mapping) and item.get("trait")
    }
    groups = plan.get("core_groups") or []
    return bool(groups) and any(
        group.get("all_of")
        and all(by_trait.get(_norm(trait), 0.0) >= STATUS_VALUE["experienced"] for trait in group["all_of"])
        for group in groups
    )


def apply_deterministic_gates(
    candidate: CandidateRecord,
    plan: Mapping[str, Any],
    *,
    score_floor: float,
    sendable_score: float,
) -> CandidateRecord:
    judge = candidate.judge or {}
    policy = plan["recruiter_policy"]["preferences"]
    profile = candidate.hydrated_profile or {}
    title = str(profile.get("current_title") or candidate.structured.get("position_title") or "").lower()
    founder_exec = profile_is_current_founder_c_suite(dict(profile)) or any(
        token in title for token in ("founder", "chief ", "ceo", "cto", "cfo", "coo")
    )
    founder_policy = policy["current_founder_c_suite_for_non_exec_ic"]
    founder_policy_applies = plan.get("target_level") in {"senior_ic", "staff_ic", "lead", None}
    hireable = not founder_exec or not founder_policy_applies or founder_policy == "eligible"
    location = candidate.hard_filter_evidence.get("disposition") == "accepted"
    score = float(judge.get("score") if judge.get("score") is not None else judge.get("jd_score") or 0)
    known_seniority = judge.get("seniority_fit") in {"ideal", "acceptable", "in_band"}
    valid_unknown_seniority = (
        judge.get("seniority_fit") == "unknown"
        and judge.get("_seniority_assessment_valid") is True
    )
    seniority = known_seniority or valid_unknown_seniority
    core = _core_group_met(judge, plan)
    judged = judge.get("status") == "judged"
    categorical = judge.get("verdict") not in {None, "out"}
    shortlist = judged and categorical and location and seniority and core and hireable and score >= score_floor
    sendable = shortlist and known_seniority and score >= sendable_score
    gates = {
        "location": location,
        "core_groups": core,
        "seniority_track": seniority,
        "founder_c_suite_hireable": hireable,
        "categorical_not_out": categorical,
        "score_floor": score >= score_floor,
        "shortlist": shortlist,
        "sendable": sendable,
    }
    return replace(candidate, deterministic_gates=gates, deterministic_score=score)


def select_exemplars(candidates: tuple[CandidateRecord, ...], limit: int) -> tuple[CandidateRecord, ...]:
    strong = [row for row in candidates if row.deterministic_gates.get("shortlist")]
    strong.sort(key=lambda row: (-row.deterministic_score, row.person_id))
    if len(strong) < 10:
        return ()
    selected: list[CandidateRecord] = []
    seen_clusters: set[tuple[str, str]] = set()
    for row in strong:
        profile = row.hydrated_profile or {}
        cluster = (
            str(profile.get("current_company") or row.structured.get("company_id") or "").lower(),
            str(profile.get("current_title") or row.structured.get("position_title") or "").lower(),
        )
        if cluster not in seen_clusters:
            seen_clusters.add(cluster)
            selected.append(row)
    selected.extend(row for row in strong if row not in selected)
    return tuple(selected[: min(20, max(10, limit))])


def expansion_probes(
    exemplars: tuple[CandidateRecord, ...],
    plan: Mapping[str, Any],
    limit: int,
) -> tuple[dict[str, str], ...]:
    probes = []
    for index, row in enumerate(exemplars[: min(6, limit)]):
        profile = row.hydrated_profile or {}
        title = profile.get("current_title") or row.structured.get("position_title") or plan["normalized_archetype"]
        company = profile.get("current_company") or row.structured.get("company_id") or "similar operating environment"
        evidence = [
            item.get("trait")
            for item in ((row.judge or {}).get("must_have") or [])
            if isinstance(item, Mapping) and STATUS_VALUE.get(str(item.get("status") or "unknown"), 0) >= STATUS_VALUE["experienced"]
        ]
        probes.append(
            {
                "probe_id": f"expansion-{index + 1:02d}",
                "probe_family": "exemplar_expansion",
                "query": (
                    f"{plan['normalized_archetype']} with proven background like {title} at {company}; "
                    f"demonstrated core evidence: {', '.join(evidence) or 'reviewed role capabilities'}"
                ),
            }
        )
    return tuple(probes)
