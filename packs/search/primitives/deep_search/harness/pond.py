"""One pond, start to finish, against the ordinary search pipeline.

  compile-pond     -> prepare the payload, apply the plan scope and the pattern pass
  review-payload   -> accept the reviewed payload and record the human edit delta
  run-pond         -> run the pipeline, review the rows, annotate fit, append the iteration
  reannotate-saved -> refresh company context and fit labels from saved rows only

The pipeline itself is a subprocess because it is a separate primitive with its
own artifact contract; everything else here is in-process.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # direct script execution
    from company_context import (
        current_company_ref, pull_note, resolve_company_contexts, resolve_hiring_company_ref,
    )
    from harness.annotate import (
        REVIEW_SCORE_THRESHOLD, _annotate_company_fit, _evaluation_text, _profiles,
        _rerank_score, _review_candidates, _review_rows,
    )
    from harness.artifacts import (
        ROOT, _last_json, _price_usage_log, _read_json, _write_json, resolve_artifact_path,
    )
    from harness.payload_patterns import _llm_pattern_defaults
    from harness.plan_review import apply_shared_plan_scope, validate_standard_traits
    from harness.pond_stats import (
        _edit_delta, _input_snapshot, _pond_costs, _pool_stats, _result_delta,
    )
    from harness.retrieval import (
        DEFAULT_LOCAL_DB, _approved_retrieval, _backend_args, _decision_backend,
    )
    from harness.summary import _save
except ImportError:  # pragma: no cover - module execution
    from ..company_context import (
        current_company_ref, pull_note, resolve_company_contexts, resolve_hiring_company_ref,
    )
    from .annotate import (
        REVIEW_SCORE_THRESHOLD, _annotate_company_fit, _evaluation_text, _profiles,
        _rerank_score, _review_candidates, _review_rows,
    )
    from .artifacts import (
        ROOT, _last_json, _price_usage_log, _read_json, _write_json, resolve_artifact_path,
    )
    from .payload_patterns import _llm_pattern_defaults
    from .plan_review import apply_shared_plan_scope, validate_standard_traits
    from .pond_stats import (
        _edit_delta, _input_snapshot, _pond_costs, _pool_stats, _result_delta,
    )
    from .retrieval import (
        DEFAULT_LOCAL_DB, _approved_retrieval, _backend_args, _decision_backend,
    )
    from .summary import _save
from search_common import load_env_file

PIPELINE = ROOT / "packs/search/primitives/search_network_pipeline/search_network_pipeline.py"
MAX_PONDS = 4
RETRIEVAL_LIMIT = 1000
LOCATION_FIELDS = ("cities", "states", "countries", "metro_areas", "macro_regions")


def _run_command(command: list[str], *, run_dir: Path, log: Path,
                 stage: str, timeout: int = 7200) -> dict[str, Any]:
    env = os.environ.copy()
    env["POWERPACKS_USAGE_LOG"] = str(run_dir / "usage.jsonl")
    env["POWERPACKS_USAGE_STAGE"] = stage
    env["OPENAI_SERVICE_TIER"] = "flex"
    completed = subprocess.run(command, cwd=ROOT, env=env, text=True,
                               capture_output=True, timeout=timeout)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8")
    _price_usage_log(run_dir / "usage.jsonl")
    if completed.returncode:
        raise RuntimeError(f"search pipeline failed ({completed.returncode}): "
                           f"{(completed.stderr or completed.stdout)[-1600:]}")
    return _last_json(completed.stdout)


def _merge_rapidapi_stats(results: dict[str, Any], stats: Mapping[str, Any]) -> None:
    total = dict(results.get("rapidapi") or {})
    for field in ("cache_hits", "cache_misses", "live_lookups", "unresolved"):
        total[field] = int(total.get(field) or 0) + int(stats.get(field) or 0)
    incoming_cost = stats.get("cost_usd")
    prior_unknown = "cost_usd" in total and total["cost_usd"] is None
    total["cost_usd"] = (None if prior_unknown or incoming_cost is None else
                         round(float(total.get("cost_usd") or 0) + float(incoming_cost), 6))
    total["unit_cost_usd"] = float(stats.get("unit_cost_usd") or
                                   total.get("unit_cost_usd") or 0)
    total["billing_basis"] = stats.get("billing_basis") or total.get("billing_basis")
    results["rapidapi"] = total


def _ensure_hiring_company_context(results: dict[str, Any], plan: Mapping[str, Any]) -> None:
    if results.get("hiring_company_context") is not None:
        return
    hiring_company = dict(plan.get("hiring_company") or results.get("hiring_company") or {})
    results["hiring_company"] = hiring_company
    results["company"] = str(hiring_company.get("name") or results.get("company") or "")
    contexts, stats = resolve_company_contexts([
        resolve_hiring_company_ref(hiring_company, plan.get("source_url"))
    ])
    context = contexts[0]
    if context:
        context["pull_note"] = pull_note(context)
        if not results.get("company"):
            results["company"] = context.get("name") or ""
    results["hiring_company_context"] = context
    _merge_rapidapi_stats(results, stats)


def compile_pond(*, run_dir: Path, env_file: str, backend: str | None = None,
                 db: str = DEFAULT_LOCAL_DB,
                 client: Any | None = None) -> Path:
    results = _read_json(run_dir / "results.json")
    if results.get("status") != "ready_to_compile" or not results.get("pending_query"):
        raise ValueError("search has no query ready to compile")
    pond_n = max((int(row.get("pond_n") or 0) for row in results.get("iterations") or []), default=0) + 1
    if pond_n > MAX_PONDS:
        raise ValueError("search already reached the four-pond cap")
    query = str(results["pending_query"]["query"])
    pond_dir = run_dir / "ponds" / f"pond-{pond_n:02d}"
    prepare_dir = pond_dir / "prepare"
    backend = _decision_backend(run_dir, backend)
    plan_path = run_dir / "epoch0" / "plan.json"
    plan = _read_json(plan_path)
    set_id, db = _approved_retrieval(run_dir, plan, backend, db)
    result = _run_command([
        sys.executable, str(PIPELINE), "prepare", "--query", query,
        "--env-file", env_file, "--output-dir", str(prepare_dir),
        "--expand-model", "gpt-5.6-luna", "--expand-reasoning-effort", "medium",
        "--limit", str(RETRIEVAL_LIMIT),
        *_backend_args(backend, db),
    ], run_dir=run_dir, log=pond_dir / "compile.log",
       stage=f"search_harness.pond_{pond_n:02d}.compile", timeout=300)
    payload = _read_json(resolve_artifact_path(result["payload_json"]))
    validate_standard_traits(payload)
    load_env_file(Path(env_file))
    compiled_locations = {field: deepcopy(payload["role_search_filters"].get(field))
                          for field in LOCATION_FIELDS if payload["role_search_filters"].get(field)}
    apply_shared_plan_scope(payload, plan, backend=backend, set_id=set_id)
    _ensure_hiring_company_context(results, plan)
    payload, pattern_edits = _llm_pattern_defaults(
        payload=payload, plan=plan, results=results, run_dir=run_dir,
        pond_n=pond_n, query=query, client=client)
    _price_usage_log(run_dir / "usage.jsonl")
    if compiled_locations or re.search(r"\b(worldwide|global|anywhere)\b", query, re.I):
        filters = payload["role_search_filters"]
        before = {field: deepcopy(filters.get(field)) for field in LOCATION_FIELDS if filters.get(field)}
        for field in LOCATION_FIELDS:
            filters.pop(field, None)
        filters.update(compiled_locations)
        if before != compiled_locations:
            pattern_edits.append({"pattern": "query_location_scope", "field": "location",
                                  "from": before or None, "to": compiled_locations or None})
    validate_standard_traits(payload)
    payload_path = pond_dir / "payload.json"
    _write_json(payload_path, payload)
    results["pending_payload"] = {
        "pond_n": pond_n, "query": query, "payload_json": str(payload_path),
        "ledger": str(prepare_dir / "pipeline.ledger.json"), "payload": payload,
        "rerank_exclusions": [], "rerank_only": False,
        "pattern_default_edits": pattern_edits, "proposed_payload": deepcopy(payload),
    }
    results["status"] = "awaiting_payload_review"
    _save(results, run_dir)
    return run_dir / "results.json"


def review_payload(*, run_dir: Path, payload_path: Path | None = None,
                   rerank_exclusions: Sequence[str] = (), human_reviewed: bool = False) -> Path:
    results = _read_json(run_dir / "results.json")
    if results.get("status") != "awaiting_payload_review" or not results.get("pending_payload"):
        raise ValueError("search has no compiled payload awaiting review")
    pending = dict(results["pending_payload"])
    target = Path(str(pending["payload_json"]))
    reviewed = _read_json(payload_path or target)
    validate_standard_traits(reviewed)
    exclusions = list(dict.fromkeys(" ".join(str(value).split()) for value in rerank_exclusions
                                    if str(value).strip()))
    _write_json(target, reviewed)
    proposed = pending.get("proposed_payload") or pending.get("payload") or {}
    human_delta = _edit_delta(
        _input_snapshot(str(pending["query"]), proposed, []),
        _input_snapshot(str(pending["query"]), reviewed, exclusions),
    )
    pending["human_edit_delta"] = human_delta if any((
        human_delta.get("query"), human_delta.get("traits_added"), human_delta.get("traits_removed"),
        human_delta.get("filters"), human_delta.get("rerank_exclusions"),
    )) else None
    pending["payload"] = reviewed
    pending["rerank_exclusions"] = exclusions
    pending["human_reviewed"] = human_reviewed
    results["pending_payload"] = pending
    results["status"] = "ready_to_rerank" if pending.get("rerank_only") else "ready_to_run"
    _save(results, run_dir)
    return run_dir / "results.json"


def run_pond(*, run_dir: Path, env_file: str, backend: str | None = None,
             db: str = DEFAULT_LOCAL_DB,
             client: Any | None = None) -> Path:
    results = _read_json(run_dir / "results.json")
    if results.get("status") not in {"ready_to_run", "ready_to_rerank"} or not results.get("pending_payload"):
        raise ValueError("search has no reviewed payload ready to run")
    pending = dict(results["pending_payload"])
    load_env_file(Path(env_file))
    pond_n = int(pending["pond_n"])
    pond_dir = run_dir / "ponds" / f"pond-{pond_n:02d}"
    backend = _decision_backend(run_dir, backend)
    plan = _read_json(run_dir / "epoch0" / "plan.json")
    set_id, db = _approved_retrieval(run_dir, plan, backend, db)
    payload = _read_json(Path(str(pending["payload_json"])))
    apply_shared_plan_scope(payload, plan, backend=backend, set_id=set_id)
    validate_standard_traits(payload)
    _write_json(Path(str(pending["payload_json"])), payload)
    command = [
        sys.executable, str(PIPELINE), "run", "--ledger", str(pending["ledger"]),
        "--env-file", env_file, "--execute-approved",
        "--filter-model", "gpt-5.6-luna", "--filter-reasoning-effort", "none",
        "--model", "gpt-5.6-luna", "--reasoning-effort", "medium",
        "--limit", str(RETRIEVAL_LIMIT), *_backend_args(backend, db),
    ]
    if pending.get("rerank_exclusions"):
        command += ["--evaluation-query", _evaluation_text(
            str(pending["query"]), pending["rerank_exclusions"])]
    if pending.get("rerank_only"):
        command.append("--force-llm")
    else:
        command += ["--query", str(pending["query"]), "--payload-json", str(pending["payload_json"])]
    result = _run_command(command, run_dir=run_dir, log=pond_dir / "run.log",
                          stage=f"search_harness.pond_{pond_n:02d}.run")
    artifacts = result.get("artifacts") or {}
    rows_path = resolve_artifact_path(artifacts.get("jsonl"))
    if not rows_path.is_file():
        raise ValueError(f"search result JSONL is missing: {rows_path}")
    rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows.sort(key=lambda row: float(row.get("final_score") or 0), reverse=True)
    arm = {
        "key": f"pond_{pond_n:02d}", "query": str(pending["query"]),
        "payload_json": str(pending["payload_json"]), "ledger": str(pending["ledger"]),
        "traits": payload["traits"], "has_domain_intent": payload["has_domain_intent"],
        "result_count": len(rows), "artifacts": artifacts,
    }
    profiles = _profiles(artifacts.get("profiles_path"))
    review_rows = _review_rows(rows)
    below_threshold = bool(
        review_rows and _rerank_score(review_rows[0]) < REVIEW_SCORE_THRESHOLD)
    _ensure_hiring_company_context(results, plan)
    refs = [current_company_ref(
        profiles.get(str(row.get("person_id") or "")) or {}, row.get("current_companies"))
        for row in review_rows]
    company_contexts, rapidapi_stats = resolve_company_contexts(refs)
    _merge_rapidapi_stats(results, rapidapi_stats)
    candidates = _review_candidates(rows, profiles, company_contexts, refs)
    candidates = _annotate_company_fit(
        candidates=candidates, results=results, run_dir=run_dir, pond_n=pond_n,
        plan=plan, client=client)
    snapshot = _input_snapshot(str(pending["query"]), payload, pending.get("rerank_exclusions") or [])
    prior = results["iterations"][-1] if results.get("iterations") else None
    prior_input = (prior or {}).get("input") or {
        "query": str(results["frozen_initial_queries"][0]["query"]),
        "traits": [], "filters": {}, "rerank_exclusions": [],
    }
    iteration = {
        "jd_id": results["jd_id"], "epoch_n": len(results["iterations"]) + 1,
        "pond_n": pond_n, "query": str(pending["query"]),
        "payload_sha": hashlib.sha256(Path(str(pending["payload_json"])).read_bytes()).hexdigest(),
        "input": snapshot, "edit_delta": _edit_delta(prior_input, snapshot),
        "pattern_default_edits": deepcopy(pending.get("pattern_default_edits") or []),
        "human_edit_delta": deepcopy(pending.get("human_edit_delta")),
        "payload_reviewed": bool(pending.get("human_reviewed")),
        "pool_stats": _pool_stats(rows, len(candidates)), "diagnosis": None,
        "below_threshold": below_threshold,
        "human_override": None, "next_move": None, "shortlist_grades": candidates,
        "reviewed_count": len(candidates), "result_count": len(rows), "arm": arm,
        "cost_usd": _pond_costs(run_dir).get(pond_n, 0.0), "gt_recall": None,
    }
    iteration["result_delta"] = _result_delta(prior, iteration)
    results["iterations"].append(iteration)
    results["pending_query"] = None
    results["pending_payload"] = None
    if pond_n >= MAX_PONDS and not pending.get("rerank_only"):
        iteration["next_move"] = {"action": "stop", "next_query": None,
                                  "rationale": "Four-pond cap reached; candidate quality is unreviewed."}
        results["status"] = "completed"
    else:
        results["status"] = "awaiting_diagnosis"
    _save(results, run_dir)
    return run_dir / "results.json"


def reannotate_saved(*, run_dir: Path, env_file: str, pond: int | None = None,
                     client: Any | None = None) -> Path:
    """Refresh company context and fit labels from saved rerank rows; never searches."""
    load_env_file(Path(env_file))
    results = _read_json(run_dir / "results.json")
    plan = _read_json(run_dir / "epoch0" / "plan.json")
    results["hiring_company_context"] = None
    results["rapidapi"] = {"cache_hits": 0, "cache_misses": 0, "live_lookups": 0,
                           "unresolved": 0, "cost_usd": 0.0, "unit_cost_usd": 0.0,
                           "billing_basis": "unit_price_not_configured"}
    _ensure_hiring_company_context(results, plan)
    iterations = list(results.get("iterations") or [])
    if pond is not None:
        iterations = [row for row in iterations if int(row.get("pond_n") or 0) == pond][-1:]
    for iteration in iterations:
        pond_n = int(iteration["pond_n"])
        artifacts = (iteration.get("arm") or {}).get("artifacts") or {}
        rows_path = resolve_artifact_path(artifacts.get("jsonl"))
        rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        rows.sort(key=lambda row: float(row.get("final_score") or 0), reverse=True)
        profiles = _profiles(artifacts.get("profiles_path"))
        review_rows = _review_rows(rows)
        refs = [current_company_ref(
            profiles.get(str(row.get("person_id") or "")) or {}, row.get("current_companies"))
            for row in review_rows]
        contexts, stats = resolve_company_contexts(refs)
        _merge_rapidapi_stats(results, stats)
        candidates = _review_candidates(rows, profiles, contexts, refs)
        saved = {str(row.get("person") or ""): row
                 for row in iteration.get("shortlist_grades") or []}
        for candidate in candidates:
            prior = saved.get(str(candidate.get("person") or "")) or {}
            if prior.get("fit_override"):
                candidate["fit_override"] = deepcopy(prior["fit_override"])
        iteration["shortlist_grades"] = _annotate_company_fit(
            candidates=candidates, results=results, run_dir=run_dir, pond_n=pond_n,
            plan=plan, client=client)
        iteration["pool_stats"] = _pool_stats(rows, len(iteration["shortlist_grades"]))
        iteration["reviewed_count"] = len(iteration["shortlist_grades"])
        iteration["below_threshold"] = bool(
            review_rows and _rerank_score(review_rows[0]) < REVIEW_SCORE_THRESHOLD)
    _price_usage_log(run_dir / "usage.jsonl")
    costs = _pond_costs(run_dir)
    for iteration in results.get("iterations") or []:
        iteration["cost_usd"] = costs.get(int(iteration["pond_n"]), 0.0)
    _save(results, run_dir)
    return run_dir / "results.json"
