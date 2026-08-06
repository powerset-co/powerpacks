"""Reviewed, bounded recruiting composition over one concrete runner."""

from __future__ import annotations

import json
import csv
import asyncio
import os
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .artifacts import REVIEW_EVIDENCE_NAME, ReviewEvidenceSnapshot
from .filters import hard_filter_validation_artifact, unsupported_hard_filters, validation_findings
from .frontier import CandidateFrontier, CandidateRecord, StageResult, lane_yield_counts
from .models import RankMode, SearchBounds, SearchPlan, SearchSpec
from .recruiting_stages import (
    apply_deterministic_gates,
    build_review_plan,
    canonical_hash,
    expansion_probes,
    generate_initial_probes,
    judge_candidate,
    normalize_jd,
    JudgeBudgetExceeded,
    review_binding,
    select_exemplars,
    triage_candidate,
    validate_review_plan,
    TransientJudgeError,
)
from .stage_membership import STAGE_MEMBERSHIP_NAME, build_stage_membership
from ..reflect.snapshots import validate_snapshot

EMPTY = CandidateFrontier((), 0, 0, None, False)
JudgeAdapter = Callable[[CandidateRecord, Mapping[str, Any]], Mapping[str, Any]]
PlanAdapter = Callable[[str, SearchSpec], Mapping[str, Any]]
CriticAdapter = Callable[[str, Mapping[str, Any], SearchSpec], Mapping[str, Any]]

PLAN_CALL_MAX_TOKENS = (64_000, 16_000, 0)
CRITIC_CALL_MAX_TOKENS = (64_000, 16_000, 0)
JUDGE_CALL_MAX_TOKENS = (128_000, 16_000, 16_000)
PLAN_MAX_COMPLETION_TOKENS = PLAN_CALL_MAX_TOKENS[1] + PLAN_CALL_MAX_TOKENS[2]
CRITIC_MAX_COMPLETION_TOKENS = CRITIC_CALL_MAX_TOKENS[1] + CRITIC_CALL_MAX_TOKENS[2]
JUDGE_MAX_COMPLETION_TOKENS = JUDGE_CALL_MAX_TOKENS[1] + JUDGE_CALL_MAX_TOKENS[2]
SHORTLIST_CSV_FIELDS = (
    "Rank",
    "Name",
    "LinkedIn URL",
    "Current Title",
    "Current Company",
    "Location",
    "Verdict",
    "Score",
    "Seniority Fit",
    "Rationale",
    "Interactions",
    "Source/Channels",
)


class _SpendReservations:
    """Reserve worst-case provider cost and release it only after captured usage."""

    def __init__(self, root: Path | None, bounds: SearchBounds):
        self.root = root
        self.bounds = bounds
        self.reserved_usd = 0.0

    def _usage_rows(self) -> list[dict[str, Any]]:
        if self.root is None:
            raise JudgeBudgetExceeded("paid recruiting calls require a private artifact directory")
        usage_log = self.root / "usage.jsonl"
        if not usage_log.exists():
            return []
        from packs.search.reflect.cost_report import load_rows

        try:
            return load_rows(usage_log)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise JudgeBudgetExceeded(f"recruiting usage capture is unreadable: {exc}") from exc

    def reserve(
        self,
        stage: str,
        model: str | None,
        maximum_tokens: tuple[int, int, int],
    ) -> tuple[float, int, str]:
        from packs.search.reflect.cost_report import DEFAULT_PRICES_PATH, build_report, load_prices
        from packs.search.primitives.lib.usage_pricing import row_cost_usd

        if not model:
            raise JudgeBudgetExceeded(f"{stage} requires a priced model")
        prices = load_prices(DEFAULT_PRICES_PATH)
        rows = self._usage_rows()
        totals = build_report(rows, prices)["totals"]
        if not totals.get("fully_priced"):
            raise JudgeBudgetExceeded("recruiting spend cannot be authorized with unpriced usage")
        prompt, completion, reasoning = maximum_tokens
        estimate = row_cost_usd(
            {
                "model": model,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "reasoning_tokens": reasoning,
            },
            prices,
        )
        if estimate is None:
            raise JudgeBudgetExceeded(f"recruiting spend cannot price model {model}")
        current = float(totals.get("cost_usd") or 0)
        if (
            self.bounds.spend_limit_usd is not None
            and current + self.reserved_usd + estimate > self.bounds.spend_limit_usd
        ):
            raise JudgeBudgetExceeded(
                f"{stage} spend budget cannot authorize the maximum provider call"
            )
        self.reserved_usd += estimate
        return estimate, len(rows), stage

    def reconcile(self, reservation: tuple[float, int, str]) -> None:
        estimate, prior_count, stage = reservation
        rows = self._usage_rows()
        if len(rows) != prior_count + 1 or rows[-1].get("stage") != stage:
            raise JudgeBudgetExceeded(
                f"{stage} usage was not captured; maximum cost remains reserved"
            )
        from packs.search.reflect.cost_report import DEFAULT_PRICES_PATH, load_prices
        from packs.search.primitives.lib.usage_pricing import row_cost_usd

        actual = row_cost_usd(rows[-1], load_prices(DEFAULT_PRICES_PATH))
        if actual is None or actual > estimate:
            raise JudgeBudgetExceeded(
                f"{stage} usage could not be reconciled within its reservation"
            )
        self.reserved_usd -= estimate

    def call(
        self,
        stage: str,
        model: str | None,
        maximum_tokens: tuple[int, int, int],
        adapter: Callable[..., Any],
        *args: Any,
    ) -> Any:
        reservation = self.reserve(stage, model, maximum_tokens)
        with _usage_stage(stage), _required_usage_capture():
            result = adapter(*args)
        self.reconcile(reservation)
        return result


def shortlist_csv_row(rank: int, candidate: CandidateRecord) -> dict[str, Any]:
    """Return the safe shareable recruiting projection; omit IDs and private evidence."""
    profile = candidate.hydrated_profile or {}
    structured = candidate.structured
    judge = candidate.judge or {}
    location = (
        profile.get("location")
        or profile.get("city")
        or structured.get("location")
        or structured.get("city")
        or ""
    )
    channels = profile.get("source_channels") or ()
    interactions = profile.get("total_interactions")
    source_channels = [*((channels,) if isinstance(channels, str) else channels)]
    if not source_channels:
        source_channels = [candidate.backend, *candidate.source_lanes]
    return {
        "Rank": rank,
        "Name": profile.get("name") or profile.get("full_name") or structured.get("name") or "",
        "LinkedIn URL": (
            profile.get("linkedin_url")
            or profile.get("public_profile_url")
            or structured.get("linkedin_url")
            or structured.get("public_profile_url")
            or ""
        ),
        "Current Title": profile.get("current_title") or structured.get("position_title") or "",
        "Current Company": profile.get("current_company") or structured.get("company_name") or "",
        "Location": location,
        "Verdict": judge.get("verdict") or "",
        "Score": judge.get("score") if judge.get("score") is not None else judge.get("jd_score") or "",
        "Seniority Fit": judge.get("seniority_fit") or "",
        "Rationale": judge.get("rationale") or "",
        "Interactions": interactions if interactions is not None else "",
        "Source/Channels": "|".join(str(value) for value in source_channels if value),
    }


def _ensure_prompt_limit(
    model: str,
    messages: list[dict[str, str]],
    maximum_prompt_tokens: int,
    stage: str,
) -> None:
    from packs.search.primitives.lib.token_accounting import count_chat_prompt_tokens

    estimated = count_chat_prompt_tokens(model, messages)
    if estimated > maximum_prompt_tokens:
        raise ValueError(
            f"{stage} input exceeds the {maximum_prompt_tokens}-token prompt ceiling "
            f"({estimated} estimated); preserve the full evidence and provide a smaller input"
        )


def _capabilities(value: Any) -> dict[str, Any]:
    result = asdict(value)
    result["backend"] = value.backend.value
    return {key: list(item) if isinstance(item, tuple) else item for key, item in result.items()}


def _write(root: Path | None, name: str, value: Any, *, jsonl: bool = False) -> str | None:
    if root is None:
        return None
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if jsonl:
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in value), encoding="utf-8")
    else:
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


def _source(spec: SearchSpec, fetcher: Callable[[str], Any] | None) -> tuple[str, dict[str, Any]]:
    raw = spec.recruiting.source
    if raw.startswith(("http://", "https://")):
        from packs.search.primitives.deep_search.fetch_jd import extract, fetch, fetch_ashby

        ashby = fetch_ashby(raw) if fetcher is None else None
        if ashby is not None:
            text, title = ashby
            return normalize_jd(text), {"requested_url": raw, "source_url": raw, "source_title": title, "via": "ashby_posting_api"}
        fetched = fetch(raw) if fetcher is None else fetcher(raw)
        if isinstance(fetched, tuple):
            raw_html, final_url = fetched
        else:
            raw_html, final_url = str(fetched), raw
        text, title = extract(str(raw_html))
        return normalize_jd(text), {"requested_url": raw, "source_url": str(final_url), "source_title": title, "via": "html"}
    return normalize_jd(raw), {"requested_url": None, "source_url": None, "source_title": None, "via": "inline"}


def _private_root(root: str | Path | None, allowed_root: str | Path | None) -> Path | None:
    if root is None:
        return None
    if allowed_root is None:
        raise ValueError("allowed_artifact_root is required when artifact_root is set")
    resolved = Path(root).resolve()
    allowed = Path(allowed_root).resolve()
    if resolved != allowed and allowed not in resolved.parents:
        raise ValueError("recruiting artifacts must be written under the explicitly allowed private root")
    return resolved


def _completed_empty(
    *,
    root: Path | None,
    spec: SearchSpec,
    paths: dict[str, str],
    binding: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    report: Mapping[str, Any],
    stage: str,
    counts: Mapping[str, int],
) -> StageResult:
    membership = build_stage_membership(
        sourced=[], hydrated=[], triaged=[], ranked=(), shortlist_person_ids=set(),
        status="completed_empty", epochs=0, bounds=spec.bounds,
    )
    audit = hard_filter_validation_artifact(
        (), spec, corpus_snapshot_hash=str(binding["corpus_sha256"])
    )
    for key, name, value in (
        (STAGE_MEMBERSHIP_NAME, STAGE_MEMBERSHIP_NAME, membership.to_dict()),
        ("candidate-frontier.json", "candidate-frontier.json", EMPTY.to_dict()),
        ("hard_filter_validation_json", "hard-filter-validation.json", audit),
    ):
        path = _write(root, name, value)
        if path:
            paths[key] = path
    return StageResult(
        stage, "completed_empty", EMPTY, counts=dict(counts), capability_report=dict(report),
        artifact_paths=paths, hard_filter_validation=audit, corpus_observation=dict(snapshot),
    )


def _production_plan_adapter(jd: str, spec: SearchSpec) -> Mapping[str, Any]:
    config = spec.recruiting
    if not config.plan_model or not config.plan_approved:
        raise ValueError("recruiting plan extraction requires explicit plan_model and plan_approved=true")
    from packs.search.primitives.deep_search.build_eval_inputs import build_plan_messages
    from packs.search.primitives.shared.openai_client import make_openai_client

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("OPENAI_API_KEY not set")
    messages = build_plan_messages(jd)
    _ensure_prompt_limit(config.plan_model, messages, PLAN_CALL_MAX_TOKENS[0], "recruiting plan")
    response = make_openai_client(key).chat.completions.create(
        model=config.plan_model,
        messages=messages,
        response_format={"type": "json_object"},
        max_completion_tokens=PLAN_MAX_COMPLETION_TOKENS,
    )
    return json.loads(response.choices[0].message.content or "{}")


def _production_critic_adapter(jd: str, plan: Mapping[str, Any], spec: SearchSpec) -> Mapping[str, Any]:
    config = spec.recruiting
    if not config.plan_model or not config.plan_approved:
        raise ValueError("recruiting plan critic requires explicit plan_model and plan_approved=true")
    from packs.search.primitives.deep_search.plan_critic import SYSTEM, supports_custom_temperature
    from packs.search.primitives.shared.openai_client import make_openai_client

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("OPENAI_API_KEY not set")
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"JOB DESCRIPTION:\n{jd}\n\nPLAN:\n{json.dumps(plan)}"},
    ]
    _ensure_prompt_limit(
        config.plan_model, messages, CRITIC_CALL_MAX_TOKENS[0], "recruiting critic"
    )
    request = {
        "model": config.plan_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_completion_tokens": CRITIC_MAX_COMPLETION_TOKENS,
    }
    if supports_custom_temperature(config.plan_model):
        request["temperature"] = 0.0
    response = make_openai_client(key).chat.completions.create(**request)
    return json.loads(response.choices[0].message.content or "{}")


def _production_judge_adapter(spec: SearchSpec) -> JudgeAdapter:
    config = spec.recruiting
    if not config.judge_implementation or not config.judge_approved:
        raise ValueError("production recruiting judge requires an explicit approved judge implementation")
    if not config.judge_model:
        raise ValueError("production recruiting judge requires judge_model")
    if config.judge_implementation == "profile_evaluator":
        from packs.search.primitives.evaluate_profile_candidates import evaluate_profile_candidates as evaluator
        from packs.search.primitives.shared.openai_client import make_async_openai_client

        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError("OPENAI_API_KEY not set")
        def judge(candidate: CandidateRecord, plan: Mapping[str, Any]) -> Mapping[str, Any]:
            async def evaluate() -> Mapping[str, Any]:
                client = make_async_openai_client(key)
                try:
                    return await evaluator.evaluate_one(
                        client,
                        asyncio.Semaphore(1),
                        config.judge_model,
                        "medium",
                        dict(plan),
                        candidate.to_dict(),
                        dict(candidate.hydrated_profile or {}),
                        120,
                        0,
                        max_completion_tokens=JUDGE_MAX_COMPLETION_TOKENS,
                        max_prompt_tokens=JUDGE_CALL_MAX_TOKENS[0],
                    )
                finally:
                    await client.close()

            result = asyncio.run(evaluate())
            if result.get("error"):
                message = str(result["error"])
                if any(token in message.lower() for token in ("timeout", "429", "temporar", "unavailable")):
                    raise TransientJudgeError(message)
                raise RuntimeError(message)
            return {**result, "score": result.get("jd_score"), "implementation": "profile_evaluator"}

        return judge
    from packs.search.primitives.deep_search import codex_judge
    from packs.search.primitives.evaluate_profile_candidates import evaluate_profile_candidates as evaluator

    def codex(candidate: CandidateRecord, plan: Mapping[str, Any]) -> Mapping[str, Any]:
        profile = dict(candidate.hydrated_profile or {})
        prompt = evaluator.SYSTEM_PROMPT + "\n\n" + evaluator.build_user_prompt(dict(plan), profile)
        _ensure_prompt_limit(
            config.judge_model,
            [{"role": "user", "content": prompt}],
            JUDGE_CALL_MAX_TOKENS[0],
            "recruiting judge",
        )
        parsed, error = codex_judge.judge_one(prompt, config.judge_model, "medium", 120)
        if error:
            lowered = str(error).lower()
            if any(
                token in lowered
                for token in (
                    "timeout",
                    "timed out",
                    "429",
                    "rate limit",
                    "rate-limit",
                    "temporar",
                    "unavailable",
                )
            ):
                raise TransientJudgeError(error)
            raise RuntimeError(error)
        result = evaluator.normalize_evaluation(parsed, dict(plan), profile)
        return {**result, "score": result["jd_score"], "implementation": "codex"}

    return codex


@contextmanager
def _usage_stage(stage: str):
    prior = os.environ.get("POWERPACKS_USAGE_STAGE")
    os.environ["POWERPACKS_USAGE_STAGE"] = stage
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("POWERPACKS_USAGE_STAGE", None)
        else:
            os.environ["POWERPACKS_USAGE_STAGE"] = prior


@contextmanager
def _required_usage_capture():
    prior = os.environ.get("POWERPACKS_USAGE_REQUIRED")
    os.environ["POWERPACKS_USAGE_REQUIRED"] = "1"
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("POWERPACKS_USAGE_REQUIRED", None)
        else:
            os.environ["POWERPACKS_USAGE_REQUIRED"] = prior


def _probe_spec(spec: SearchSpec, query: str) -> SearchSpec:
    return replace(
        spec,
        raw_request=query,
        role=replace(spec.role, bm25_queries=(query,)),
        bounds=replace(
            spec.bounds,
            retrieval_limit=spec.bounds.per_probe_limit,
            semantic_rank_limit=min(spec.bounds.semantic_rank_limit, spec.bounds.per_probe_limit),
        ),
    )


def _run_probes(
    spec: SearchSpec,
    runner: Any,
    capabilities: Any,
    sources: Any,
    filters: Any,
    probes: tuple[dict[str, str], ...],
) -> tuple[list[CandidateRecord], list[dict[str, str]]]:
    contributions: dict[tuple[str, str], tuple[CandidateRecord, ...]] = {}
    failures: list[dict[str, str]] = []

    def one(probe: dict[str, str]) -> tuple[CandidateRecord, ...]:
        probe_spec = _probe_spec(spec, probe["query"])
        plan = SearchPlan(probe_spec, capabilities, sources, ("retrieve",))
        rows = tuple(runner.retrieve_people(plan, filters, probe["probe_id"], probe["probe_family"]))
        by_lane: dict[str, list[CandidateRecord]] = {}
        for row in rows:
            lane = row.source_lanes[0] if row.source_lanes else "unknown"
            by_lane.setdefault(lane, []).append(row)
        lanes = {
            lane: sorted(values, key=lambda item: (-item.retrieval_score, item.person_id))
            for lane, values in by_lane.items()
        }
        selected: list[str] = []
        depth = 0
        while len(selected) < spec.bounds.per_probe_limit:
            added = False
            for lane in sorted(lanes):
                if depth >= len(lanes[lane]):
                    continue
                added = True
                person_id = lanes[lane][depth].person_id
                if person_id not in selected:
                    selected.append(person_id)
                    if len(selected) == spec.bounds.per_probe_limit:
                        break
            if not added:
                break
            depth += 1
        selected_ids = set(selected)
        return CandidateFrontier.merge(
            [row for row in rows if row.person_id in selected_ids]
        ).candidates

    with ThreadPoolExecutor(max_workers=spec.bounds.max_concurrent_probes) as pool:
        pending = {pool.submit(one, probe): probe for probe in probes}
        for future in as_completed(pending):
            probe = pending[future]
            try:
                contributions[(probe["probe_family"], probe["probe_id"])] = future.result()
            except Exception as exc:
                failures.append({**probe, "error": str(exc)})
    records = [row for key in sorted(contributions) for row in contributions[key]]
    failures.sort(key=lambda row: (row["probe_family"], row["probe_id"]))
    return records, failures


def _validate_hydrated(
    spec: SearchSpec, sources: Any, frontier: CandidateFrontier
) -> tuple[tuple[CandidateRecord, ...], tuple[CandidateRecord, ...]]:
    accepted, reviewed = [], []
    for row in frontier.candidates:
        if row.hydration_disposition != "hydrated":
            reviewed.append(row)
            continue
        findings = validation_findings(
            row.hydrated_profile, spec, sources, row.source_lanes, row.structured
        )
        disposition = "accepted" if not findings["violations"] and not findings["unknowns"] else "quarantined"
        validated = replace(
            row,
            hard_filter_evidence={
                **row.hard_filter_evidence,
                **findings,
                "validated": disposition == "accepted",
                "disposition": disposition,
            },
        )
        reviewed.append(validated)
        if disposition == "accepted":
            accepted.append(validated)
    return tuple(accepted), tuple(reviewed)


def _fuse(records: list[CandidateRecord], limit: int | None) -> CandidateFrontier:
    merged = CandidateFrontier.merge(records)
    ordered = sorted(
        merged.candidates,
        key=lambda row: (
            -row.retrieval_score,
            -len(row.found_by),
            tuple(sorted(row.source_lanes)),
            row.person_id,
        ),
    )
    truncated = bool(limit and len(ordered) > limit)
    if limit:
        ordered = ordered[:limit]
    return CandidateFrontier(tuple(ordered), len(records), len(ordered), limit, truncated)


def _ranked(candidates: tuple[CandidateRecord, ...]) -> tuple[CandidateRecord, ...]:
    return tuple(
        sorted(
            candidates,
            key=lambda row: (
                -float((row.judge or {}).get("score") or row.deterministic_score),
                -len(row.found_by),
                -row.retrieval_score,
                row.person_id,
            ),
        )
    )


def _within_spend_budget(
    root: Path | None,
    bounds: SearchBounds,
    *,
    model: str | None = None,
    maximum_tokens: tuple[int, int, int] = (0, 0, 0),
) -> bool:
    if bounds.spend_limit_usd is None:
        return True
    if root is None:
        return False
    usage_log = root / "usage.jsonl"
    from packs.search.reflect.cost_report import DEFAULT_PRICES_PATH, build_report, load_prices, load_rows
    from packs.search.primitives.lib.usage_pricing import row_cost_usd

    prices = load_prices(DEFAULT_PRICES_PATH)
    rows = load_rows(usage_log) if usage_log.exists() else []
    totals = build_report(rows, prices)["totals"]
    if not totals.get("fully_priced"):
        return False
    estimate = 0.0
    if model is not None:
        prompt, completion, reasoning = maximum_tokens
        priced = row_cost_usd(
            {
                "model": model,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "reasoning_tokens": reasoning,
            },
            prices,
        )
        if priced is None:
            return False
        estimate = priced
    return float(totals.get("cost_usd") or 0) + estimate <= bounds.spend_limit_usd


def _run_recruiting(
    spec: SearchSpec,
    runner: Any,
    *,
    artifact_root: str | Path | None = None,
    fetcher: Callable[[str], Any] | None = None,
    plan_adapter: PlanAdapter | None = None,
    critic_adapter: CriticAdapter | None = None,
    judge_adapter: JudgeAdapter | None = None,
    corpus_snapshot: Mapping[str, Any] | None = None,
) -> StageResult:
    root = Path(artifact_root).resolve() if artifact_root is not None else None
    spend = _SpendReservations(root, spec.bounds)
    started = time.monotonic()
    try:
        jd, source_metadata = _source(spec, fetcher)
    except Exception as exc:
        return StageResult("review", "needs_input", EMPTY, errors=(str(exc),))
    capabilities = runner.capabilities(spec)
    report = _capabilities(capabilities)
    unsupported = unsupported_hard_filters(spec, capabilities.supported_hard_filters)
    if unsupported:
        return StageResult("capabilities", "unsupported_capability", EMPTY, errors=(", ".join(unsupported),))
    try:
        snapshot = dict(corpus_snapshot or runner.snapshot_corpus(
            str(spec.corpus.to_dict().get("set_id") or "local"),
            spec.recruiting.review_pool_person_ids,
            spec=spec,
        ))
    except Exception as exc:
        return StageResult("review", "needs_input", EMPTY, capability_report=report, errors=(str(exc),))
    if snapshot.get("verification_status") != "verified_comparable":
        return StageResult(
            "review",
            "needs_input",
            EMPTY,
            capability_report=report,
            errors=("recruiting Review requires a verified comparable corpus snapshot",),
            corpus_observation=snapshot,
        )
    evidence_hashes = snapshot.get("evidence_hashes")
    if not isinstance(evidence_hashes, dict) or set(evidence_hashes) != set(
        spec.recruiting.review_pool_person_ids
    ):
        return StageResult(
            "review",
            "failed_binding",
            EMPTY,
            capability_report=report,
            errors=("review corpus evidence does not exactly match requested review-pool person IDs",),
            corpus_observation=snapshot,
        )
    snapshot_errors = validate_snapshot(snapshot, spec.recruiting.review_pool_person_ids)
    if snapshot_errors:
        return StageResult(
            "review",
            "failed_binding",
            EMPTY,
            capability_report=report,
            errors=tuple(snapshot_errors),
            corpus_observation=snapshot,
        )
    supplied = spec.corpus.to_dict()
    if spec.backend.value == "local":
        derived = {
            "content_hash": snapshot.get("scoped_records_hash"),
            "schema_hash": canonical_hash(snapshot.get("namespace_schema_hashes") or {}),
            "membership_hash": snapshot.get("membership_hash"),
        }
        for name, value in derived.items():
            if supplied.get(name) is not None and supplied[name] != value:
                return StageResult(
                    "review", "needs_input", EMPTY,
                    errors=(f"supplied local {name} does not match the runner snapshot",),
                    corpus_observation=snapshot,
                )
    review_plan_path = root / "review/plan.json" if root is not None else None
    binding_path = root / "review/binding.json" if root is not None else None
    if spec.recruiting.reviewed_plan_hash:
        if review_plan_path is None or not review_plan_path.exists() or binding_path is None or not binding_path.exists():
            return StageResult("review", "failed_binding", EMPTY, errors=("review artifacts are missing",))
        plan = json.loads(review_plan_path.read_text(encoding="utf-8"))
        prior_binding = json.loads(binding_path.read_text(encoding="utf-8"))
    else:
        adapter = plan_adapter or _production_plan_adapter
        try:
            extracted = (
                adapter(jd, spec)
                if plan_adapter is not None
                else spend.call(
                    "recruiting_plan",
                    spec.recruiting.plan_model,
                    PLAN_CALL_MAX_TOKENS,
                    adapter,
                    jd,
                    spec,
                )
            )
            plan = build_review_plan(
                spec,
                extracted,
                created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                source_url=source_metadata.get("source_url"),
            )
        except Exception as exc:
            _write(root, "review/source.json", {**source_metadata, "normalized_jd": jd, "sha256": canonical_hash(jd)})
            return StageResult("review", "needs_input", EMPTY, capability_report=report, errors=(str(exc),))
        prior_binding = None
    critic = validate_review_plan(spec, plan)
    binding = review_binding(spec, plan, jd, snapshot)
    if prior_binding is not None and prior_binding != binding:
        return StageResult(
            "review", "failed_binding", EMPTY, capability_report=report,
            errors=("reviewed plan/source/corpus/policy binding drifted; start a new run",),
        )
    critic_document: dict[str, Any] = {"deterministic_issues": list(critic)}
    if not spec.recruiting.reviewed_plan_hash:
        adapter = critic_adapter or _production_critic_adapter
        try:
            critic_document.update(
                adapter(jd, plan, spec)
                if critic_adapter is not None
                else spend.call(
                    "recruiting_critic",
                    spec.recruiting.plan_model,
                    CRITIC_CALL_MAX_TOKENS,
                    adapter,
                    jd,
                    plan,
                    spec,
                )
            )
        except Exception as exc:
            return StageResult("review", "needs_input", EMPTY, capability_report=report, errors=(str(exc),))
    review_evidence = ReviewEvidenceSnapshot.from_hashes(evidence_hashes)
    paths = {}
    for key, name, value in (
        ("review_plan_json", "review/plan.json", plan),
        ("review_critic_json", "review/critic.json", critic_document),
        ("review_binding_json", "review/binding.json", binding),
        ("review_policy_json", "review/policy.json", plan["recruiter_policy"]),
        ("review_source_json", "review/source.json", {**source_metadata, "normalized_jd": jd, "sha256": binding["jd_sha256"]}),
        ("review_corpus_json", "review/corpus.json", snapshot),
        ("review_evidence_json", REVIEW_EVIDENCE_NAME, review_evidence.to_dict()),
    ):
        path = _write(root, name, value)
        if path:
            paths[key] = path
    if not spec.recruiting.reviewed_plan_hash:
        timings_path = _write(
            root,
            "timings.json",
            {
                "review_seconds": round(time.monotonic() - started, 6),
                "total_seconds": round(time.monotonic() - started, 6),
            },
        )
        if timings_path:
            paths["timings_json"] = timings_path
        return StageResult(
            "review",
            "awaiting_review",
            EMPTY,
            counts={"review_count": 1},
            capability_report=report,
            warnings=critic,
            artifact_paths=paths,
            corpus_observation=snapshot,
        )
    if spec.recruiting.reviewed_plan_hash != binding["plan_sha256"]:
        return StageResult(
            "review",
            "failed_binding",
            EMPTY,
            capability_report=report,
            errors=("reviewed plan/source/corpus/policy binding drifted; start a new run",),
            artifact_paths=paths,
        )
    try:
        adapter = judge_adapter or _production_judge_adapter(spec)
    except ValueError as exc:
        return StageResult("judge", "needs_input", EMPTY, errors=(str(exc),), artifact_paths=paths)

    if (
        judge_adapter is None
        and spec.recruiting.judge_implementation == "profile_evaluator"
    ):
        production_adapter = adapter

        def adapter(candidate: CandidateRecord, reviewed_plan: Mapping[str, Any]) -> Mapping[str, Any]:
            return spend.call(
                "recruiting_judge",
                spec.recruiting.judge_model,
                JUDGE_CALL_MAX_TOKENS,
                production_adapter,
                candidate,
                reviewed_plan,
            )

    source_started = time.monotonic()
    sources = runner.resolve_sources(spec)
    if sources.unresolved_required_inputs:
        return StageResult("resolve_sources", "needs_input", EMPTY, errors=tuple(sources.unresolved_required_inputs))
    filters = runner.apply_hard_filters(spec, sources)
    if filters.eligible_count == 0:
        return _completed_empty(
            root=root, spec=spec, paths=paths, binding=binding, snapshot=snapshot,
            report=report, stage="hard_filter", counts={"eligible_pool": 0, "total_sourced": 0},
        )
    resolved_at = time.monotonic()

    probes = generate_initial_probes(spec, plan)
    records, failures = _run_probes(spec, runner, capabilities, sources, filters, probes)
    initial_source_capped = (
        len({row.person_id for row in records}) > spec.bounds.sourced_candidate_limit
    )
    _write(root, "stages/initial-probes.json", probes)
    _write(root, "stages/probe-failures.json", failures)
    if not records and len(failures) == len(probes):
        return StageResult("source", "failed_source", EMPTY, errors=tuple(row["error"] for row in failures))
    frontier = _fuse(records, spec.bounds.sourced_candidate_limit)
    sourced_records = list(frontier.candidates)
    sourced_person_ids = {row.person_id for row in frontier.candidates}
    if not frontier.candidates:
        return _completed_empty(
            root=root, spec=spec, paths=paths, binding=binding, snapshot=snapshot,
            report=report, stage="source",
            counts={"eligible_pool": filters.eligible_count, "initial_probes": len(probes),
                    "probe_failures": len(failures), "total_sourced": 0},
        )
    frontier = runner.hydrate(frontier)
    survivors, reviewed_initial = _validate_hydrated(spec, sources, frontier)
    audit_rows: list[CandidateRecord] = list(reviewed_initial)
    frontier = _fuse(list(survivors), None)
    hydrated_at = time.monotonic()

    unjudged = list(frontier.candidates)
    triage_skipped = len(unjudged) <= spec.bounds.judge_candidate_limit
    if not triage_skipped:
        unjudged = [triage_candidate(row, spec.bounds.triage_score_threshold) for row in unjudged]
        unjudged = [row for row in unjudged if row.triage["disposition"] in {"keep", "maybe"}]
    unjudged.sort(
        key=lambda row: (
            0 if (row.triage or {}).get("disposition") == "keep" else 1,
            -float((row.triage or {}).get("score") or row.retrieval_score),
            -len(row.found_by),
            row.person_id,
        )
    )
    judge_queue = unjudged[: spec.bounds.judge_candidate_limit]
    triage_records = list(unjudged)
    judged: list[CandidateRecord] = []
    model_calls = 0
    budget_capped = initial_source_capped
    judge_started = time.monotonic()
    for row in judge_queue:
        if model_calls >= spec.bounds.judge_call_limit:
            break
        try:
            result, calls = judge_candidate(
                row,
                plan,
                adapter,
                max_attempts=min(2, spec.bounds.judge_call_limit - model_calls),
            )
        except JudgeBudgetExceeded:
            budget_capped = True
            break
        model_calls += calls
        judged.append(
            apply_deterministic_gates(
                result,
                plan,
                score_floor=spec.bounds.score_floor,
                sendable_score=spec.bounds.sendable_score,
            )
        )
    if len(judged) < len(unjudged):
        budget_capped = True
    judged_candidate_count = len(judged)
    judged_ids = {row.person_id for row in judged}
    reviewable = judged + [row for row in unjudged if row.person_id not in judged_ids]
    frontier = _fuse(reviewable, None)
    judged_at = time.monotonic()

    expansion_started = time.monotonic()
    exemplars = select_exemplars(frontier.candidates, spec.bounds.exemplar_limit)
    terminal = "completed_capped" if budget_capped else "completed_no_anchors"
    total_sourced = len(sourced_person_ids)
    epochs = 0
    if exemplars and not budget_capped:
        terminal = "completed_converged"
        for epoch in range(spec.bounds.epoch_limit):
            epochs = epoch + 1
            if total_sourced >= spec.bounds.sourced_candidate_limit:
                terminal = "completed_capped"
                break
            expansion = expansion_probes(exemplars, plan, spec.bounds.expansion_thread_limit)
            expanded, expansion_failures = _run_probes(
                spec, runner, capabilities, sources, filters, expansion
            )
            failures.extend(expansion_failures)
            if expansion_failures and len(expansion_failures) == len(expansion):
                terminal = "failed_source"
                break
            remaining_budget = spec.bounds.sourced_candidate_limit - total_sourced
            net_new_frontier = _fuse(
                [row for row in expanded if row.person_id not in sourced_person_ids],
                remaining_budget,
            )
            source_capped_this_epoch = net_new_frontier.truncated
            net_new_raw = list(net_new_frontier.candidates)
            if not net_new_raw:
                terminal = "completed_converged"
                break
            total_sourced += len(net_new_raw)
            sourced_person_ids.update(row.person_id for row in net_new_raw)
            sourced_records.extend(net_new_raw)
            hydrated = runner.hydrate(_fuse(net_new_raw, None))
            net_new, reviewed_expansion = _validate_hydrated(spec, sources, hydrated)
            audit_rows.extend(reviewed_expansion)
            if not net_new:
                terminal = "completed_converged"
                break
            triage_records.extend(net_new)
            new_judged = []
            for row in net_new:
                if (
                    model_calls >= spec.bounds.judge_call_limit
                    or judged_candidate_count >= spec.bounds.judge_candidate_limit
                ):
                    terminal = "completed_capped"
                    break
                try:
                    result, calls = judge_candidate(
                        row,
                        plan,
                        adapter,
                        max_attempts=min(2, spec.bounds.judge_call_limit - model_calls),
                    )
                except JudgeBudgetExceeded:
                    terminal = "completed_capped"
                    break
                model_calls += calls
                judged_candidate_count += 1
                new_judged.append(
                    apply_deterministic_gates(
                        result,
                        plan,
                        score_floor=spec.bounds.score_floor,
                        sendable_score=spec.bounds.sendable_score,
                    )
                )
            judged_new_ids = {row.person_id for row in new_judged}
            retained_new = [
                *new_judged,
                *(row for row in net_new if row.person_id not in judged_new_ids),
            ]
            frontier = _fuse([*frontier.candidates, *retained_new], None)
            if source_capped_this_epoch:
                terminal = "completed_capped"
            if terminal == "completed_capped":
                break
            exemplars = select_exemplars(frontier.candidates, spec.bounds.exemplar_limit)
        else:
            terminal = "completed_capped"
    if not frontier.candidates:
        terminal = "completed_empty"
    expanded_at = time.monotonic()

    frontier = CandidateFrontier(
        _ranked(frontier.candidates),
        frontier.input_count,
        frontier.output_count,
        frontier.limit,
        frontier.truncated,
    )
    persistence_started = time.monotonic()
    usage_log = root / "usage.jsonl" if root is not None else None
    usage = {"calls": 0, "cost_usd": None, "fully_priced": False}
    if usage_log is not None and usage_log.exists():
        from packs.search.reflect.cost_report import build_report, load_prices, load_rows, DEFAULT_PRICES_PATH

        usage = build_report(load_rows(usage_log), load_prices(DEFAULT_PRICES_PATH))["totals"]
    usage["provider_model_calls"] = model_calls if judge_adapter is None else 0
    usage["injected_adapter_calls"] = model_calls if judge_adapter is not None else 0
    priced_spend = usage.get("cost_usd") if usage.get("fully_priced") else None
    if spec.bounds.spend_limit_usd is not None and priced_spend is not None and priced_spend >= spec.bounds.spend_limit_usd:
        terminal = "completed_capped"
    presented = frontier.candidates[: spec.bounds.frontier_limit]
    presentation_truncated = len(frontier.candidates) > len(presented)
    shortlist = [row.to_dict() for row in presented if row.deterministic_gates.get("shortlist")]
    sendable = [row.to_dict() for row in presented if row.deterministic_gates.get("sendable")]
    bench = [row.to_dict() for row in presented if not row.deterministic_gates.get("shortlist")]
    unjudged_rows = [row.to_dict() for row in frontier.candidates if not row.judge or row.judge.get("status") != "judged"]
    stage_membership = build_stage_membership(
        sourced=sourced_records,
        hydrated=audit_rows,
        triaged=triage_records,
        ranked=frontier.candidates,
        shortlist_person_ids={row["person_id"] for row in shortlist},
        status=terminal,
        epochs=epochs,
        bounds=spec.bounds,
    )
    for name, value, jsonl in (
        (STAGE_MEMBERSHIP_NAME, stage_membership.to_dict(), False),
        ("candidate-frontier.json", frontier.to_dict(), False),
        ("candidate-frontier.jsonl", [row.to_dict() for row in frontier.candidates], True),
        ("stages/probe-failures.json", failures, False),
        ("judge/unjudged.json", unjudged_rows, False),
        ("shortlist_ranked.json", shortlist, False),
        ("sendable_ranked.json", sendable, False),
        ("bench_ranked.json", bench, False),
        ("usage.json", usage, False),
        ("convergence.json", {"status": terminal, "epochs": epochs, "total_sourced": total_sourced}, False),
        ("bounds.json", {**asdict(spec.bounds), "frontier_truncated": presentation_truncated}, False),
    ):
        path = _write(root, name, value, jsonl=jsonl)
        if path:
            paths[name] = path
    if root is not None:
        csv_path = root / "shortlist.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=SHORTLIST_CSV_FIELDS,
                lineterminator="\n",
            )
            writer.writeheader()
            for rank, row in enumerate(
                (row for row in presented if row.deterministic_gates.get("shortlist")), start=1
            ):
                writer.writerow(shortlist_csv_row(rank, row))
        paths["shortlist_csv"] = str(csv_path)
    audit = hard_filter_validation_artifact(
        tuple(audit_rows), spec, corpus_snapshot_hash=binding["corpus_sha256"]
    )
    _write(root, "hard-filter-validation.json", audit)
    timings = {
        "review_seconds": round(source_started - started, 6),
        "resolve_filter_seconds": round(resolved_at - source_started, 6),
        "source_hydrate_seconds": round(hydrated_at - resolved_at, 6),
        "judge_seconds": round(judged_at - judge_started, 6),
        "expansion_seconds": round(expanded_at - expansion_started, 6),
        "persistence_seconds": round(time.monotonic() - persistence_started, 6),
        "total_seconds": round(time.monotonic() - started, 6),
    }
    _write(root, "timings.json", timings)
    return StageResult(
        "recruiting",
        terminal,
        frontier,
        counts={
            "eligible_pool": filters.eligible_count,
            "initial_probes": len(probes),
            "probe_failures": len(failures),
            "triage_skipped": int(triage_skipped),
            "judge_calls": model_calls,
            "unjudged": len(unjudged_rows),
            "exemplars": len(exemplars),
            "total_sourced": total_sourced,
            **lane_yield_counts(sourced_records),
        },
        capability_report=report,
        resolved_sources=sources.records,
        artifact_paths=paths,
        hard_filter_validation=audit,
        corpus_observation=snapshot,
    )


def run_recruiting(
    spec: SearchSpec,
    runner: Any,
    *,
    artifact_root: str | Path | None = None,
    allowed_artifact_root: str | Path | None = None,
    fetcher: Callable[[str], Any] | None = None,
    plan_adapter: PlanAdapter | None = None,
    critic_adapter: CriticAdapter | None = None,
    judge_adapter: JudgeAdapter | None = None,
    corpus_snapshot: Mapping[str, Any] | None = None,
) -> StageResult:
    root = _private_root(artifact_root, allowed_artifact_root)
    prior = os.environ.get("POWERPACKS_USAGE_LOG")
    if root is not None:
        os.environ["POWERPACKS_USAGE_LOG"] = str(root / "usage.jsonl")
    try:
        return _run_recruiting(
            spec,
            runner,
            artifact_root=root,
            fetcher=fetcher,
            plan_adapter=plan_adapter,
            critic_adapter=critic_adapter,
            judge_adapter=judge_adapter,
            corpus_snapshot=corpus_snapshot,
        )
    finally:
        if prior is None:
            os.environ.pop("POWERPACKS_USAGE_LOG", None)
        else:
            os.environ["POWERPACKS_USAGE_LOG"] = prior
