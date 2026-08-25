"""The one pre-search checkpoint: plan + initial queries, then the fixed run.

  no --plan-approved -> prepare_review: build the plan, probe the network
                        floors, generate the queries, stop
  --plan-approved    -> validate; a plan whose population/geography binding
                        changed re-probes and regenerates queries for one more
                        review; otherwise bind the corpus and initialize_run
  set-query          -> update_pending_query rewrites the next pond's query

Also owns the query vocabulary the rest of the harness reads back: the query-arm
and trait contracts, the shared plan scope applied to every compiled payload,
and the occupation head parsed out of a query for the brief and next-move source.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:  # direct script execution
    from location_scope import enforce_payload_location, location_scope_from_plan
    from network_floors import floor_binding, probe_populations, sparsity_lines
    from plan_filters import enforce_payload_retrieval_filters, validate_plan_filter_contract
    from subprocess_utils import run_checked
    from harness.artifacts import ROOT, _now, _read_json, _write_json
    from harness.summary import _save
except ImportError:  # pragma: no cover - module execution
    from ..location_scope import enforce_payload_location, location_scope_from_plan
    from ..network_floors import floor_binding, probe_populations, sparsity_lines
    from ..plan_filters import enforce_payload_retrieval_filters, validate_plan_filter_contract
    from ..subprocess_utils import run_checked
    from .artifacts import ROOT, _now, _read_json, _write_json
    from .summary import _save

BUILD_PLAN = ROOT / "packs/search/primitives/deep_search/build_eval_inputs.py"
DECOMPOSE = ROOT / "packs/search/primitives/deep_search/decompose_jd.py"
HARNESS_CLI = ROOT / "packs/search/primitives/deep_search/search_harness.py"
NETWORK_FLOORS_FILE = "network_floors.json"
TEMPORAL_VALUES = {"current", "past", "all"}
MEANING_VALUES = {"role", "experience", "location", "education", "company", "investor", "general"}
_OCCUPATION_HEAD_STOPWORDS = {
    "a", "an", "the", "senior", "staff", "principal", "junior", "lead", "founding",
}


def validate_query_arms(value: Any) -> list[dict[str, str]]:
    raw = value.get("queries") if isinstance(value, dict) else value
    if not isinstance(raw, list) or not 1 <= len(raw) <= 2:
        raise ValueError("queries must contain 1 or 2 query arms")
    arms = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"key", "query"}:
            raise ValueError("each query arm must contain only key and query")
        key = str(item.get("key") or "").strip()
        query = " ".join(str(item.get("query") or "").split())
        if not key or not query:
            raise ValueError(f"query arm {index + 1} has an empty key or query")
        arms.append({"key": key, "query": query})
    if len({row["key"] for row in arms}) != len(arms):
        raise ValueError("query arm keys must be unique")
    if len({row["query"].casefold() for row in arms}) != len(arms):
        raise ValueError("query arm text must be unique")
    return arms


def validate_standard_traits(payload: Mapping[str, Any]) -> None:
    traits = payload.get("traits")
    if not isinstance(traits, list) or not isinstance(payload.get("has_domain_intent"), bool):
        raise ValueError("payload needs top-level traits and has_domain_intent")
    if not isinstance(payload.get("role_search_filters"), dict):
        raise ValueError("payload needs role_search_filters")
    for index, trait in enumerate(traits):
        if not isinstance(trait, dict) or not str(trait.get("value") or "").strip():
            raise ValueError(f"trait {index + 1} is invalid")
        if trait.get("temporal") not in TEMPORAL_VALUES or trait.get("meaning") not in MEANING_VALUES:
            raise ValueError(f"trait {index + 1} has invalid temporal or meaning")


def apply_shared_plan_scope(payload: dict[str, Any], plan: Mapping[str, Any], *,
                            backend: str, set_id: str | None) -> dict[str, Any]:
    _location, location_filters = location_scope_from_plan(dict(plan))
    enforce_payload_location(payload, location_filters)
    enforce_payload_retrieval_filters(payload, validate_plan_filter_contract(dict(plan)))
    filters = payload.setdefault("role_search_filters", {})
    filters.pop("age_min", None)
    filters.pop("age_max", None)
    if backend == "powerset" and set_id:
        filters["set_id"] = set_id
    return payload


def _plan_generation_command(args: Any, epoch0: Path, plan_path: Path) -> list[object]:
    command: list[object] = [
        sys.executable, BUILD_PLAN, "--run-dir", epoch0, "--jd-file", args.jd_file,
        "--created-at", args.created_at, "--plan-only", "--model", args.plan_model,
        "--reasoning-effort", args.plan_reasoning_effort,
    ]
    if args.jd_url:
        command += ["--source-url", args.jd_url]
    source_json = epoch0.parent / "source.json"
    if source_json.is_file():
        command += ["--source-json", source_json]
    if args.set_id:
        command += ["--set-id", args.set_id]
    if args.preferences:
        command += ["--preferences", args.preferences]
    return command


def _query_generation_command(args: Any, plan_path: Path, queries_path: Path) -> list[object]:
    return [
        sys.executable, DECOMPOSE, "--jd-file", args.jd_file, "--plan", plan_path,
        "--model", args.query_model, "--reasoning-effort", args.query_reasoning_effort,
        "--query-only", "--dynamic-simple", "--out", queries_path,
    ]


def prepare_review(
    args: Any,
    run_dir: Path,
    plan_path: Path,
    queries_path: Path,
    *,
    resolve_identity: Callable[..., tuple[dict[str, Any], str | None, str]],
    probe_floors: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    epoch0 = run_dir / "epoch0"
    epoch0.mkdir(parents=True, exist_ok=True)
    if not plan_path.exists():
        run_checked(_plan_generation_command(args, epoch0, plan_path),
                    expected_paths=[plan_path], description="build deep-search plan")
    floors_path = run_dir / NETWORK_FLOORS_FILE
    if not floors_path.exists():
        plan = _read_json(plan_path)
        retrieval, args.set_id, args.db = resolve_identity(
            args.backend, plan, args.set_id, args.db)
        floors = probe_floors(
            plan,
            backend=args.backend,
            retrieval_identity=retrieval,
            env_file=getattr(args, "env_file", ".env"),
        )
        _write_json(floors_path, floors)
    else:
        floors = _read_json(floors_path)
    if not queries_path.exists():
        run_checked(_query_generation_command(args, plan_path, queries_path),
                    expected_paths=[queries_path], description="generate initial search queries")
    arms = validate_query_arms(json.loads(queries_path.read_text(encoding="utf-8")))
    review = "Edit the plan and one or two queries, then rerun with --plan-approved."
    sparse = sparsity_lines(floors)
    if sparse:
        review += "\n" + "\n".join(sparse)
    return {
        "primitive": "deep_search_loop", "status": "awaiting_plan_approval", "mode": "simple",
        "plan": str(plan_path), "queries": str(queries_path), "query_arms": arms,
        "network_floors": floors["floors"], "network_floors_artifact": str(floors_path),
        "source_started": False,
        "review": review,
        "next": "rerun with --plan-approved",
    }


def _occupation_heads(queries: Sequence[Any]) -> set[str]:
    heads: set[str] = set()
    for raw in queries:
        head = re.split(r"\s+with\s+|\s+who\s+|\s+in\s+|,|—|\||/",
                        str(raw or "").lower(), maxsplit=1)[0]
        tokens = [token for token in re.findall(r"[a-z][a-z-]+", head)
                  if token not in _OCCUPATION_HEAD_STOPWORDS]
        if tokens:
            heads.add(" ".join(tokens[-2:]) if len(tokens) >= 2 else tokens[0])
            heads.add(tokens[-1])
    return heads


def _source_occupation(query: Any) -> str:
    heads = _occupation_heads([query])
    return max(heads, key=lambda value: (len(value.split()), len(value)), default="")


def initialize_run(*, run_dir: Path, jd_path: Path, plan_path: Path, queries_path: Path) -> Path:
    results_path = run_dir / "results.json"
    if results_path.exists():
        return results_path
    bound_jd = run_dir / "jd.txt"
    if jd_path.resolve() != bound_jd.resolve():
        shutil.copyfile(jd_path, bound_jd)
    plan = _read_json(plan_path)
    queries = validate_query_arms(json.loads(queries_path.read_text(encoding="utf-8")))
    must = ((plan.get("traits") or {}).get("must_have") or [])
    defining = next((str(row.get("trait") or "").strip() for row in must
                     if row.get("tier") == "core" and str(row.get("trait") or "").strip()), None)
    scope = plan.get("search_scope") or {}
    hiring_company = dict(plan.get("hiring_company") or {})
    floors_path = run_dir / NETWORK_FLOORS_FILE
    network_floors = _read_json(floors_path) if floors_path.is_file() else None
    results = {
        "schema_version": "search-harness.v1", "created_at": _now(),
        "jd_id": str(plan.get("job_id") or run_dir.name),
        "company": str(hiring_company.get("name") or ""),
        "hiring_company": hiring_company,
        "candidate_populations": deepcopy(plan.get("candidate_populations") or []),
        "comp_band": deepcopy(plan.get("comp_band")),
        "title": str(plan.get("job_title") or plan.get("source_title") or ""),
        "url": str(plan.get("source_url") or ""),
        "brief": {
            "occupation": (_source_occupation(queries[0]["query"]) or
                           str(plan.get("normalized_archetype") or plan.get("job_title") or "")),
            "defining_capability": defining, "geography": str(scope.get("location") or ""),
        },
        "frozen_initial_queries": queries, "pending_query": queries[0],
        "pending_payload": None, "status": "ready_to_compile", "iterations": [],
        "raw_model_responses": [], "hiring_company_context": None,
        "network_floors": network_floors,
        "rapidapi": {"cache_hits": 0, "cache_misses": 0, "live_lookups": 0,
                     "unresolved": 0, "cost_usd": 0.0, "unit_cost_usd": 0.0,
                     "billing_basis": "unit_price_not_configured"},
    }
    _save(results, run_dir)
    return results_path


def run_search_harness(args: Any, run_dir: Path, decision_path: Path | None, *,
                    validate_plan: Callable[..., dict[str, Any]],
                    resolve_identity: Callable[..., tuple[dict[str, Any], str | None, str]],
                    bind_plan: Callable[..., tuple[Path, str]],
                    probe_floors: Callable[..., dict[str, Any]] = probe_populations) -> dict[str, Any]:
    plan_path = Path(args.approved_plan).resolve() if args.approved_plan else run_dir / "epoch0" / "plan.json"
    queries_path = Path(args.queries_file).resolve() if args.queries_file else run_dir / "queries.json"
    if args.plan_approved and args.approved_plan:
        raise ValueError("use only one of --plan-approved or --approved-plan")
    approved = bool(args.plan_approved or args.approved_plan)
    if not approved:
        return prepare_review(
            args,
            run_dir,
            plan_path,
            queries_path,
            resolve_identity=resolve_identity,
            probe_floors=probe_floors,
        )
    if not plan_path.is_file():
        raise ValueError("reviewed plan must exist before --plan-approved")
    plan = validate_plan(plan_path, expected_source_url=args.jd_url)
    retrieval, args.set_id, args.db = resolve_identity(args.backend, plan, args.set_id, args.db)
    floors_path = run_dir / NETWORK_FLOORS_FILE
    saved_floors = _read_json(floors_path) if floors_path.is_file() else {}
    if saved_floors.get("binding") != floor_binding(plan, args.backend, retrieval):
        floors = probe_floors(
            plan,
            backend=args.backend,
            retrieval_identity=retrieval,
            env_file=getattr(args, "env_file", ".env"),
        )
        _write_json(floors_path, floors)
        queries_path = run_dir / "queries.json"
        run_checked(_query_generation_command(args, plan_path, queries_path),
                    expected_paths=[queries_path], description="regenerate changed-binding queries")
        arms = validate_query_arms(json.loads(queries_path.read_text(encoding="utf-8")))
        return {
            "primitive": "deep_search_loop", "status": "awaiting_query_review", "mode": "simple",
            "plan": str(plan_path), "queries": str(queries_path), "query_arms": arms,
            "network_floors": floors["floors"], "network_floors_artifact": str(floors_path),
            "source_started": False,
            "review": "Review the regenerated queries, then rerun with --plan-approved.",
            "next": "review queries.json, then rerun with --plan-approved",
        }
    if not queries_path.is_file():
        raise ValueError("reviewed queries must exist before --plan-approved")
    plan_path, _digest = bind_plan(run_dir, plan_path, retrieval, Path(args.jd_file),
                                   reviewed_queries_path=queries_path)
    results_path = initialize_run(run_dir=run_dir, jd_path=Path(args.jd_file),
                                  plan_path=plan_path, queries_path=queries_path)
    return {
        "primitive": "deep_search_loop", "status": "ready_to_compile", "mode": "simple",
        "results": str(results_path), "manifest": str(run_dir / "manifest.json"),
        "decision": str(decision_path) if decision_path else None,
        "next": f"run {HARNESS_CLI.name} compile-pond --run-dir {run_dir}",
    }


def update_pending_query(*, run_dir: Path, query: str) -> Path:
    query = " ".join(str(query or "").split())
    if not query:
        raise ValueError("query cannot be empty")
    results = _read_json(run_dir / "results.json")
    if results.get("status") not in {"ready_to_compile", "awaiting_payload_review", "ready_to_run"}:
        raise ValueError("the current query is not editable")
    pond_n = max((int(row.get("pond_n") or 0) for row in results.get("iterations") or []), default=0) + 1
    if results.get("iterations"):
        prior = results["iterations"][-1]
        delta = prior.get("proposal_delta")
        if isinstance(delta, dict) and isinstance(delta.get("proposal"), Mapping):
            actual = dict(delta.get("actual") or {})
            actual["next_query"] = query
            delta["actual"] = actual
            proposal = delta["proposal"]
            delta["changed"] = (
                proposal.get("action") != actual.get("action") or
                proposal.get("next_query") != actual.get("next_query")
            )
    results["pending_query"] = {"key": f"pond_{pond_n:02d}", "query": query}
    results["pending_payload"] = None
    results["status"] = "ready_to_compile"
    _save(results, run_dir)
    return run_dir / "results.json"
