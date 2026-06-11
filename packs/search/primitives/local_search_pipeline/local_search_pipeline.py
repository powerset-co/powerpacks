#!/usr/bin/env python3
"""Local-only Powerpacks search pipeline backed by DuckDB.

This orchestrator is intentionally separate from search_network_pipeline.py:
local search scope is the DuckDB file itself, so there is no set_id, operator
resolution, Postgres hydration, or TurboPuffer access in this command path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
PRIMITIVES_DIR = ROOT / "packs/search/primitives"
LIB_DIR = PRIMITIVES_DIR / "lib"
SHARED_DIR = PRIMITIVES_DIR / "shared"
LOCAL_DIR = PRIMITIVES_DIR / "local"
TURBOPUFFER_DIR = PRIMITIVES_DIR / "turbopuffer"
PAYLOAD_KEYS = {"intent_type", "source_type", "normalized_query", "vertical", "role_search_filters", "traits", "notes"}
REMOTE_SCOPE_KEYS = {"set_id", "operator_ids", "allowed_operator_ids", "searcher_operator_id"}
UNSUPPORTED_LOCAL_FILTERS = {
    "investor_names",
    "operator_interaction_min",
    "operator_interaction_max",
    "set_interaction_min",
    "set_interaction_max",
}
COMPANY_RESOLVE_FILTER_KEYS = {
    "company_names",
    "company_ids",
    "current_company_names",
    "company_semantic_queries",
    "sector_types",
    "entity_types",
    "technology_types",
    "customer_types",
    "customer_type",
    "company_cities",
    "company_states",
    "company_countries",
    "company_metro_areas",
    "company_macro_regions",
    "funding_stage_min",
    "funding_stage_max",
    "funding_amount_min",
    "funding_amount_max",
    "headcount_min",
    "headcount_max",
    "valuation_min",
    "valuation_max",
    "founded_year_min",
    "founded_year_max",
    "last_funding_after",
    "last_funding_before",
    "yc_batches",
    "accelerators",
    "stages",
    "company_stages",
    "stage",
}
PREVIEW_FILTER_KEYS = [
    "company_names", "company_ids", "company_semantic_queries", "education_names", "education_ids",
    "metro_areas", "cities", "states", "countries", "macro_regions", "seniority_bands",
    "years_experience_min", "years_experience_max", "position_after_date", "position_before_date",
    "is_current_role", "is_current_company", "tech_skills",
    "sector_types", "entity_types", "technology_types", "customer_types", "customer_type",
    "company_cities", "company_states", "company_countries", "company_metro_areas", "company_macro_regions",
    "funding_stage_min", "funding_stage_max", "funding_amount_min", "funding_amount_max",
    "headcount_min", "headcount_max", "valuation_min", "valuation_max", "founded_year_min",
    "founded_year_max", "last_funding_after", "last_funding_before", "yc_batches", "accelerators",
    "stages", "company_stages", "stage", "x_followers_min", "x_followers_max",
    "li_followers_min", "li_followers_max", "li_connections_min", "li_connections_max",
    "ig_followers_min", "ig_followers_max",
]
ARTIFACT_KEYS = {"state", "retrieval_artifact", "profiles_path", "llm_profiles_path", "csv", "jsonl", "manifest", "artifact_dir"}
COUNT_KEYS = {
    "resolved_count",
    "base_candidate_count",
    "company_union_candidate_count",
    "returned_people",
    "hydrated",
    "requested",
    "row_count",
    "frontier_count",
    "hydrated_count",
    "passed_count",
    "filtered_count",
    "scored_count",
    "ranked_count",
}
MODE_KEYS = {"search_mode", "retrieval_mode", "prefilter_short_circuit", "base_id_batch_count", "base_id_batch_size", "company_union_added", "limit", "top_k", "profiles_compressed"}


class PipelineError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def parse_jsons(raw: str) -> list[Any]:
    out: list[Any] = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(raw):
        while index < len(raw) and raw[index].isspace():
            index += 1
        if index >= len(raw):
            break
        try:
            obj, end = decoder.raw_decode(raw, index)
            out.append(obj)
            index = end
        except json.JSONDecodeError:
            next_start = raw.find("{", index + 1)
            if next_start < 0:
                break
            index = next_start
    return out


def relative_or_absolute(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_env_file_into(env: dict[str, str], env_file: str | None) -> None:
    if not env_file:
        return
    path = Path(env_file)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value and not env.get(key.strip()):
            env[key.strip()] = value


def local_child_env(db_path: Path, *, env_file: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    load_env_file_into(env, env_file)
    return env


def run_command(cmd: list[str], *, db_path: Path, timeout: int, env_file: str | None = None) -> dict[str, Any]:
    started = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=local_child_env(db_path, env_file=env_file),
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    json_objects = parse_jsons(proc.stdout or "")
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "json_objects": json_objects,
        "json": json_objects[-1] if json_objects else None,
    }


def require_ok(result: dict[str, Any], step: str) -> dict[str, Any]:
    if result["returncode"] != 0:
        detail = (result.get("stderr") or result.get("stdout") or "").strip()
        raise PipelineError(f"{step} failed rc={result['returncode']}: {detail[-1200:]}")
    return result.get("json") or {}


def ledger_path_for(args: argparse.Namespace) -> Path:
    if args.ledger:
        return Path(args.ledger)
    if args.state:
        return Path(str(args.state) + ".local-pipeline.json")
    return ROOT / ".powerpacks/runs/local-search-pipeline.json"


def load_ledger(path: Path) -> dict[str, Any]:
    ledger = read_json(path, {}) or {}
    ledger.setdefault("created_at", now())
    ledger.setdefault("steps", {})
    ledger.setdefault("artifacts", {})
    return ledger


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = now()
    write_json(path, ledger)


def mark(path: Path, ledger: dict[str, Any], step: str, status: str, **extra: Any) -> None:
    record = ledger.setdefault("steps", {}).setdefault(step, {"id": step})
    record.update(status=status, **extra)
    if status in {"completed", "skipped", "failed"}:
        record["finished_at"] = now()
    save_ledger(path, ledger)


def done(ledger: dict[str, Any], step: str) -> bool:
    return ledger.get("steps", {}).get(step, {}).get("status") == "completed"


def compact_summary(value: Any) -> Any:
    if isinstance(value, list):
        return {"count": len(value)} if len(value) > 20 else [compact_summary(item) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"candidate_ids", "candidates", "base_candidate_ids", "company_union_candidate_ids", "company_union_candidates", "profile_ids", "rows"}:
            out[key + "_count" if not key.endswith("_count") else key] = len(item) if isinstance(item, list) else 0
        elif key in ARTIFACT_KEYS or key in COUNT_KEYS or key in MODE_KEYS or key in {"primitive", "status", "namespace", "query", "task_id", "created_at", "source"}:
            out[key] = compact_summary(item)
        elif key == "artifacts" and isinstance(item, dict):
            out[key] = {artifact_key: artifact_value for artifact_key, artifact_value in item.items() if isinstance(artifact_value, (str, int, float, bool))}
        elif key.endswith("_count") or key.endswith("_path"):
            out[key] = item
    return out


def collect_artifacts(summary: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ARTIFACT_KEYS:
        value = summary.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    artifacts = summary.get("artifacts")
    if isinstance(artifacts, dict):
        for key, value in artifacts.items():
            if isinstance(value, str) and value:
                out[key] = value
    return out


def latest_step(state: Path, step_id: str) -> dict[str, Any]:
    data = read_json(state, {}) or {}
    for step in reversed(data.get("steps", [])):
        if step.get("id") == step_id:
            return step.get("output") or {}
    return {}


def pipeline_summary(ledger: dict[str, Any]) -> dict[str, Any]:
    steps = ledger.get("steps", {}) or {}

    def step_summary(step: str) -> dict[str, Any]:
        return (steps.get(step, {}) or {}).get("summary", {}) or {}

    resolved = step_summary("resolve_companies")
    prefilters = step_summary("apply_prefilters")
    retrieval = step_summary("execute_role_search")
    hydrate = step_summary("hydrate_people")
    llm_filter = step_summary("llm_filter_candidates")
    llm_rerank = step_summary("llm_rerank_candidates")
    persist = step_summary("persist_search_results")
    return {
        key: value
        for key, value in {
            "resolved_companies": resolved.get("resolved_count"),
            "search_mode": retrieval.get("search_mode") or prefilters.get("search_mode"),
            "retrieval_mode": retrieval.get("retrieval_mode"),
            "base_candidates": prefilters.get("base_candidate_count"),
            "company_union_candidates": prefilters.get("company_union_candidate_count") or retrieval.get("company_union_candidate_count"),
            "company_union_added": retrieval.get("company_union_added"),
            "returned_people": retrieval.get("returned_people"),
            "hydrated": hydrate.get("hydrated"),
            "llm_passed": llm_filter.get("passed_count"),
            "llm_filtered": llm_filter.get("filtered_count"),
            "ranked": llm_rerank.get("ranked_count"),
            "rows": persist.get("row_count"),
        }.items()
        if value is not None
    }


def payload_from_expand_output(output: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in output.items() if key in PAYLOAD_KEYS}


def _dedupe_present(values: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if value in (None, "", [], {}):
            continue
        marker = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(value)
    return out


def _entity_values(values: Any, *, prefer_display: bool = False) -> list[str]:
    out: list[str] = []
    for value in values or []:
        if isinstance(value, dict):
            raw = (
                value.get("display_value") or value.get("value") or value.get("name") or value.get("id")
                if prefer_display
                else value.get("id") or value.get("display_value") or value.get("value") or value.get("name")
            )
        else:
            raw = value
        if raw not in (None, ""):
            out.append(str(raw))
    return list(dict.fromkeys(out))


def _pattern_examples(patterns: Any) -> list[str]:
    """Extract safe lexical hints from prod role pattern metadata."""
    examples: list[str] = []
    for item in patterns or []:
        if not isinstance(item, dict):
            continue
        for example in item.get("examples") or []:
            if isinstance(example, str) and example.strip():
                examples.append(example.strip())
    return list(dict.fromkeys(examples))


def normalize_query_expansion_payload(payload: dict[str, Any], *, query: str | None = None) -> dict[str, Any]:
    """Return local DuckDB payload shape for Powerpacks or prod/API expansion.

    Prod/network expansion uses `filters.role_semantic_query`,
    `filters.role_bm25_queries`, and entity objects. Local DuckDB primitives use
    `role_search_filters.semantic_query`, `role_search_filters.bm25_queries`, and
    scalar local filters. Normalizing at this boundary lets local_search_pipeline
    consume the same query-expansion artifact where fields are statically
    translatable.
    """
    if not isinstance(payload, dict):
        return payload
    source_filters = payload.get("role_search_filters")
    if isinstance(source_filters, dict):
        normalized = dict(source_filters)
    elif isinstance(payload.get("filters"), dict):
        normalized = dict(payload["filters"])
    else:
        return payload

    if "role_semantic_query" in normalized and "semantic_query" not in normalized:
        normalized["semantic_query"] = normalized.get("role_semantic_query")
    if "role_bm25_queries" in normalized:
        normalized["bm25_queries"] = _dedupe_present([
            *(normalized.get("bm25_queries") or []),
            *(normalized.get("role_bm25_queries") or []),
        ])

    id_entity_keys = {
        "company_ids", "cities", "states", "countries", "metro_areas", "macro_regions",
        "company_cities", "company_states", "company_countries", "company_metro_areas",
        "company_macro_regions", "seniority_bands", "role_ids", "degree_levels", "fields_of_study",
        "sector_types", "entity_types", "technology_types", "customer_types", "yc_batches",
    }
    for key in id_entity_keys:
        if isinstance(normalized.get(key), list) and normalized[key] and isinstance(normalized[key][0], dict):
            normalized[key] = _entity_values(normalized[key])
    if isinstance(normalized.get("education_ids"), list) and normalized["education_ids"] and isinstance(normalized["education_ids"][0], dict):
        normalized.setdefault("education_names", _entity_values(normalized["education_ids"], prefer_display=True))
        normalized["education_ids"] = _entity_values(normalized["education_ids"])

    examples = _pattern_examples(normalized.get("role_core_patterns"))
    if examples:
        normalized["bm25_queries"] = _dedupe_present([*(normalized.get("bm25_queries") or []), *examples])

    normalized = {key: value for key, value in normalized.items() if is_present(value)}
    out = dict(payload)
    out["role_search_filters"] = normalized
    out.setdefault("intent_type", "role_search")
    out.setdefault("source_type", payload.get("source_type") or ("prod_expand_query" if "filters" in payload else "query"))
    out.setdefault("normalized_query", payload.get("normalized_query") or payload.get("original_query") or query)
    out.setdefault("vertical", payload.get("vertical") or "people")
    return out


def payload_filters(payload: dict[str, Any]) -> dict[str, Any]:
    filters = payload.get("role_search_filters")
    return dict(filters) if isinstance(filters, dict) else {}


def _query_tokens_for_title_cluster(payload: dict[str, Any]) -> set[str]:
    filters = payload_filters(payload)
    text_parts = [str(payload.get("normalized_query") or "")]
    text_parts.extend(str(value) for value in filters.get("bm25_queries") or [] if value)
    return {token for token in re.findall(r"[a-z0-9]+", " ".join(text_parts).lower()) if len(token) > 2}


def _with_local_title_clustering_status(payload: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(payload))
    filters = payload_filters(out)
    filters["local_title_clustering_status"] = status
    out["role_search_filters"] = filters
    notes = out.get("notes")
    if not isinstance(notes, list):
        notes = []
    if status.get("status") == "error":
        notes.append(f"local title clustering skipped: {status.get('error')}")
    out["notes"] = notes
    return out


def apply_local_title_clustering(payload: dict[str, Any], db_path: Path, *, max_clusters: int = 20) -> dict[str, Any]:
    """Augment local expansion with DuckDB-scoped title clusters.

    Prod runs TitleClusterer during query expansion before search execution.
    Local mirrors that layer boundary by reading title inventory from the DuckDB
    people table while preparing the payload.  This is intentionally
    conservative: clusters must overlap the original/BM25 query tokens before
    they become executable BM25/regex hints, so local clustering does not broaden
    hard role filters with unrelated in-scope titles.
    """
    filters = payload_filters(payload)
    if not filters or not db_path.exists():
        return _with_local_title_clustering_status(payload, {"status": "skipped_no_filters_or_db"})
    if not any(filters.get(key) for key in ["role_ids", "role_tracks", "seniority_bands", "company_ids"]):
        return _with_local_title_clustering_status(payload, {"status": "skipped_no_title_scope"})
    try:
        for _path in [LIB_DIR, SHARED_DIR, LOCAL_DIR, TURBOPUFFER_DIR]:
            if str(_path) not in sys.path:
                sys.path.insert(0, str(_path))
        from local_duckdb_store import LocalDuckDBSearchStore  # type: ignore
        from search_common import filters_from_role_payload  # type: ignore

        store = LocalDuckDBSearchStore(str(db_path))
        title_filters = filters_from_role_payload(filters)
        clusters = store.title_clusters_for_filters(title_filters, max_titles=10000)
    except Exception as exc:
        return _with_local_title_clustering_status(payload, {"status": "error", "error": str(exc)[:300]})
    finally:
        try:
            store.conn.close()  # type: ignore[name-defined]
        except Exception:
            pass

    if not clusters:
        return _with_local_title_clustering_status(payload, {"status": "completed", "cluster_count": 0, "selected_count": 0})
    query_tokens = _query_tokens_for_title_cluster(payload)
    selected: list[dict[str, Any]] = []
    keywords: list[str] = []
    for cluster in clusters:
        title = str(cluster.get("display_title") or "").strip()
        title_tokens = {token for token in re.findall(r"[a-z0-9]+", title.lower()) if len(token) > 2}
        if not title or (query_tokens and not (query_tokens & title_tokens)):
            continue
        selected.append(cluster)
        keywords.append(title)
        if len(selected) >= max_clusters:
            break
    if not selected:
        return _with_local_title_clustering_status(payload, {"status": "completed", "cluster_count": len(clusters), "selected_count": 0})

    out = json.loads(json.dumps(payload))
    out_filters = payload_filters(out)
    out_filters["local_title_clusters"] = selected
    out_filters["local_title_cluster_keywords"] = list(dict.fromkeys(keywords))
    out_filters["local_title_clustering_status"] = {"status": "completed", "cluster_count": len(clusters), "selected_count": len(selected)}
    out_filters["bm25_queries"] = _dedupe_present([*(out_filters.get("bm25_queries") or []), *keywords])
    existing_patterns = list(out_filters.get("role_core_patterns") or [])
    seen_regex = {str(item.get("regex")) for item in existing_patterns if isinstance(item, dict)}
    for keyword in keywords:
        regex = r"\b" + r"\s+".join(re.escape(token) for token in re.findall(r"[A-Za-z0-9]+", keyword)) + r"\b"
        if regex not in seen_regex:
            existing_patterns.append({"regex": regex, "examples": [keyword], "source": "local_title_cluster"})
            seen_regex.add(regex)
    out_filters["role_core_patterns"] = existing_patterns
    out["role_search_filters"] = out_filters
    return out


def is_present(value: Any) -> bool:
    return value not in (None, "", [], {})


def prepare_local_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    sanitized = json.loads(json.dumps(payload))
    filters = payload_filters(sanitized)
    ignored_scope_keys = sorted(
        {
            key
            for key in REMOTE_SCOPE_KEYS
            if key in sanitized or key in filters
        }
    )

    unsupported = sorted(key for key in UNSUPPORTED_LOCAL_FILTERS if is_present(filters.get(key)))
    if unsupported:
        raise PipelineError(f"local_search_pipeline does not support these remote-only filters yet: {', '.join(unsupported)}")

    sanitized["role_search_filters"] = filters
    notes = sanitized.get("notes")
    if not isinstance(notes, list):
        notes = []
    if ignored_scope_keys:
        notes.append(f"local_search_pipeline ignores remote scope keys: {', '.join(ignored_scope_keys)}")
    notes.append("Local search scope is the DuckDB file; no set_id/operator resolution is used.")
    sanitized["notes"] = notes
    return sanitized, ignored_scope_keys


def payload_quality_issues(payload: dict[str, Any]) -> list[str]:
    filters = payload_filters(payload)
    semantic_query = filters.get("semantic_query")
    bm25 = [item for item in (filters.get("bm25_queries") or []) if isinstance(item, str) and item.strip()]
    has_role_intent = bool(semantic_query or bm25 or filters.get("role_ids") or filters.get("role_names") or filters.get("titles") or filters.get("role_tracks"))
    issues: list[str] = []
    if has_role_intent and bm25 and (not isinstance(semantic_query, str) or len(semantic_query.strip()) < 80):
        issues.append("role/profile intent will run BM25/filter-only unless semantic_query has at least 80 characters")
    return issues


BROAD_POOL_RATIO = 0.6


def local_pool_estimate(payload: dict[str, Any], db_path: Path) -> dict[str, Any]:
    """Cheap filter-eligibility count so breadth is visible before LLM spend."""
    if not db_path.exists():
        return {"status": "skipped_no_db"}
    try:
        for _path in [LIB_DIR, SHARED_DIR, LOCAL_DIR, TURBOPUFFER_DIR]:
            if str(_path) not in sys.path:
                sys.path.insert(0, str(_path))
        from local_duckdb_store import LocalDuckDBSearchStore  # type: ignore
        from search_common import filters_from_role_payload  # type: ignore

        store = LocalDuckDBSearchStore(str(db_path))
        try:
            counts = store.filtered_people_count(filters_from_role_payload(payload_filters(payload)))
        finally:
            store.conn.close()
        return {"status": "completed", **counts}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


def compact_preview(payload: dict[str, Any], payload_json: Path, db_path: Path, removed_scope_keys: list[str]) -> dict[str, Any]:
    filters = payload_filters(payload)
    visible_filters = {}
    for key in PREVIEW_FILTER_KEYS:
        value = filters.get(key)
        if is_present(value):
            visible_filters[key] = value
    role = {key: filters.get(key) for key in ["semantic_query", "bm25_queries", "role_ids", "role_tracks"] if is_present(filters.get(key))}
    runtime_notes = payload_quality_issues(payload)
    pool = local_pool_estimate(payload, db_path)
    matched = pool.get("matched_people")
    total = pool.get("total_people")
    if matched is not None and total:
        if matched == 0:
            runtime_notes.append("hard filters match 0 people in the local index; modify the search or expect the zero-result SQL fallback")
        elif matched / total > BROAD_POOL_RATIO:
            runtime_notes.append(
                f"broad search: hard filters match {matched} of {total} people in the local index; "
                "consider narrowing or an agentic SQL prefilter before running LLM stages over most of the index"
            )
    return {
        "normalized_query": payload.get("normalized_query"),
        "payload_json": str(payload_json),
        "duckdb": str(db_path),
        "scope": "local_duckdb",
        "ignored_remote_scope_keys": removed_scope_keys,
        "role_title_intent": role or None,
        "filters": visible_filters,
        "pool_estimate": pool,
        "runtime_notes": runtime_notes,
    }


def output_dir_for(query: str, explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        return path if path.is_absolute() else ROOT / path
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:60] or "query"
    run_id = hashlib.sha1(f"local:{query}:{time.time()}".encode()).hexdigest()[:10]
    return ROOT / ".powerpacks/search" / f"{run_id}-local-{slug}"


def init_state(args: argparse.Namespace, ledger_path: Path, ledger: dict[str, Any], payload: dict[str, Any]) -> Path:
    if args.state:
        state = Path(args.state)
        ledger["state"] = str(state)
        save_ledger(ledger_path, ledger)
        return state
    if ledger.get("state") and not (args.query and args.payload_json):
        return Path(str(ledger["state"]))
    if not args.query:
        raise PipelineError("Need --state or --query")

    cmd = [
        sys.executable,
        str(ROOT / "packs/search/primitives/task_state/task_state.py"),
        "init",
        "--query",
        args.query,
    ]
    output = require_ok(run_command(cmd, db_path=args.db_path, timeout=args.timeout, env_file=getattr(args, "env_file", None)), "task_state init")
    state = Path(output["state"])
    ledger["state"] = str(state)
    ledger.setdefault("artifacts", {})["state"] = str(state)
    mark(ledger_path, ledger, "init_state", "completed", summary=compact_summary(output), command=" ".join(shlex.quote(item) for item in cmd))

    cmd = [
        sys.executable,
        str(ROOT / "packs/search/primitives/task_state/task_state.py"),
        "record-step",
        "--state",
        str(state),
        "--step-id",
        "expand_search_request",
        "--status",
        "completed",
        "--output-json",
        json.dumps(payload),
    ]
    output = require_ok(run_command(cmd, db_path=args.db_path, timeout=args.timeout, env_file=getattr(args, "env_file", None)), "record expand_search_request")
    mark(ledger_path, ledger, "record_expand_search_request", "completed", summary=compact_summary(output), command=" ".join(shlex.quote(item) for item in cmd))
    save_ledger(ledger_path, ledger)
    return state


def run_step(ledger_path: Path, ledger: dict[str, Any], step: str, cmd: list[str], args: argparse.Namespace, *, timeout: int | None = None) -> dict[str, Any]:
    if done(ledger, step) and not args.force:
        return (ledger.get("steps", {}).get(step, {}) or {}).get("summary", {}) or {}
    mark(ledger_path, ledger, step, "running", command=" ".join(shlex.quote(item) for item in cmd))
    output = require_ok(run_command(cmd, db_path=args.db_path, timeout=timeout or args.timeout, env_file=getattr(args, "env_file", None)), step)
    ledger.setdefault("artifacts", {}).update(collect_artifacts(output))
    mark(ledger_path, ledger, step, "completed", summary=compact_summary(output), command=" ".join(shlex.quote(item) for item in cmd))
    return output


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    args.db_path = Path(args.db).expanduser().resolve()
    if not args.db_path.exists():
        raise PipelineError(f"local DuckDB does not exist: {args.db_path}")

    ledger_path = ledger_path_for(args)
    ledger = load_ledger(ledger_path)
    ledger["mode"] = "local_duckdb"
    ledger["duckdb"] = str(args.db_path)
    ledger["current_block"] = None
    save_ledger(ledger_path, ledger)

    if args.payload_json:
        payload = read_json(Path(args.payload_json))
    elif args.state:
        payload = latest_step(Path(args.state), "expand_search_request")
    else:
        payload = {}
    if not isinstance(payload, dict) or not payload:
        raise PipelineError("Need --payload-json, or --state with an expand_search_request step")
    payload = normalize_query_expansion_payload(payload, query=args.query)
    payload = apply_local_title_clustering(payload, args.db_path)
    payload, ignored_scope_keys = prepare_local_payload(payload)
    if args.payload_json:
        sanitized_path = ledger_path.parent / f"{ledger_path.stem}.local-payload.json"
        write_json(sanitized_path, payload)
        ledger.setdefault("artifacts", {})["payload_json"] = str(sanitized_path)
        ledger["ignored_remote_scope_keys"] = ignored_scope_keys
        save_ledger(ledger_path, ledger)

    state = init_state(args, ledger_path, ledger, payload)
    filters = payload_filters(payload)
    child_env_file = "/dev/null"
    local_primitive_dir = ROOT / "packs/search/primitives/local_duckdb"

    steps: list[tuple[str, list[str]]] = []
    if any(is_present(filters.get(key)) for key in COMPANY_RESOLVE_FILTER_KEYS):
        steps.append((
            "resolve_companies",
            [sys.executable, str(local_primitive_dir / "resolve_companies.py"), "--db", str(args.db_path), "--state", str(state), "--env-file", child_env_file, "--write-state"],
        ))
    if filters.get("education_names"):
        steps.append((
            "resolve_education",
            [sys.executable, str(local_primitive_dir / "resolve_education.py"), "--db", str(args.db_path), "--state", str(state), "--env-file", child_env_file, "--write-state"],
        ))
    steps.extend([
        (
            "apply_prefilters",
            [sys.executable, str(local_primitive_dir / "apply_prefilters.py"), "--db", str(args.db_path), "--state", str(state), "--env-file", child_env_file, "--write-state"],
        ),
        (
            "execute_role_search",
            [
                sys.executable,
                str(local_primitive_dir / "execute_role_search.py"),
                "--db",
                str(args.db_path),
                "--state",
                str(state),
                "--env-file",
                child_env_file,
                "--write-state",
                "--limit",
                str(args.limit),
                "--top-k",
                str(args.top_k),
                *(
                    ["--extra-candidates-json", str(Path(args.extra_candidates_json).expanduser().resolve())]
                    if getattr(args, "extra_candidates_json", None)
                    else []
                ),
            ],
        ),
        (
            "hydrate_people",
            [sys.executable, str(local_primitive_dir / "hydrate_people.py"), "--db", str(args.db_path), "--state", str(state), "--env-file", child_env_file, "--write-state"],
        ),
    ])

    for step, cmd in steps:
        run_step(ledger_path, ledger, step, cmd, args)

    # LLM filter/rerank mirror the remote pipeline and are backend-agnostic:
    # they read hydrated profiles from task state and call OpenAI only (the
    # data path stays fully local). Skip them when retrieval came back empty
    # or when the caller asked for search-only.
    hydrated_count = int((ledger.get("steps", {}).get("hydrate_people", {}) or {}).get("summary", {}).get("hydrated") or 0)
    llm_steps: list[tuple[str, list[str]]] = []
    if not args.search_only and hydrated_count > 0:
        llm_steps.append((
            "llm_filter_candidates",
            [sys.executable, str(ROOT / "packs/search/primitives/llm_filter_candidates/llm_filter_candidates.py"), "--state", str(state), "--profile-scope", "auto", "--write-state"],
        ))
        if not args.filter_only:
            llm_steps.append((
                "llm_rerank_candidates",
                [sys.executable, str(ROOT / "packs/search/primitives/llm_rerank_candidates/llm_rerank_candidates.py"), "--state", str(state), "--write-state"],
            ))
    for step, cmd in llm_steps:
        run_step(ledger_path, ledger, step, cmd, args, timeout=args.llm_timeout)
        # If the conservative filter rejected everyone, there is nothing to rank.
        if step == "llm_filter_candidates":
            passed = int((ledger.get("steps", {}).get(step, {}) or {}).get("summary", {}).get("passed_count") or 0)
            if passed == 0:
                break

    run_step(ledger_path, ledger, "persist_search_results", [sys.executable, str(ROOT / "packs/search/primitives/persist_search_results/results_io.py"), "export", "--state", str(state)], args)

    ledger["current_block"] = None
    save_ledger(ledger_path, ledger)
    return {
        "primitive": "local_search_pipeline",
        "status": "completed",
        "mode": "local_duckdb",
        "duckdb": str(args.db_path),
        "ledger": str(ledger_path),
        "state": str(state),
        "ignored_remote_scope_keys": ignored_scope_keys,
        "summary": pipeline_summary(ledger),
        "artifacts": ledger.get("artifacts", {}),
    }


def cmd_run(args: argparse.Namespace) -> int:
    try:
        emit(run_pipeline(args))
        return 0
    except Exception as exc:
        emit({"primitive": "local_search_pipeline", "status": "failed", "error": str(exc)})
        return 1


def cmd_prepare(args: argparse.Namespace) -> int:
    try:
        db_path = Path(args.db).expanduser().resolve()
        if not db_path.exists():
            raise PipelineError(f"local DuckDB does not exist: {db_path}")
        out_dir = output_dir_for(args.query, args.output_dir)
        payload_json = out_dir / "expand_search_request.local.json"
        full_json = out_dir / "expand_search_request.full.json"
        ledger = out_dir / "local-search.pipeline.json"
        env = dict(os.environ)
        cmd = [
            sys.executable,
            str(ROOT / "packs/search/primitives/expand_search_request/expand_search_request.py"),
            "--query",
            args.query,
            "--env-file",
            args.env_file,
            "--timeout",
            str(args.timeout),
        ]
        if args.model:
            cmd.extend(["--model", args.model])
        proc = subprocess.run(cmd, cwd=ROOT, env=env, text=True, capture_output=True, timeout=args.timeout + 30)
        parsed = parse_jsons(proc.stdout or "")
        if proc.returncode != 0:
            raise PipelineError(f"expand_search_request failed rc={proc.returncode}: {((proc.stderr or proc.stdout or '').strip())[-1200:]}")
        expanded = parsed[-1] if parsed else {}
        payload = normalize_query_expansion_payload(payload_from_expand_output(expanded), query=args.query)
        payload = apply_local_title_clustering(payload, db_path)
        payload, removed_scope_keys = prepare_local_payload(payload)
        write_json(full_json, expanded)
        write_json(payload_json, payload)
        execute_command = (
            "uv run --project . python packs/search/primitives/local_search_pipeline/local_search_pipeline.py run "
            f"--db {shlex.quote(relative_or_absolute(db_path))} "
            f"--ledger {shlex.quote(relative_or_absolute(ledger))} "
            f"--query {shlex.quote(args.query)} "
            f"--payload-json {shlex.quote(relative_or_absolute(payload_json))}"
        )
        emit({
            "primitive": "local_search_pipeline",
            "status": "preview_ready",
            "query": args.query,
            "duckdb": str(db_path),
            "payload_json": str(payload_json),
            "ledger": str(ledger),
            "ignored_remote_scope_keys": removed_scope_keys,
            "preview": compact_preview(payload, payload_json, db_path, removed_scope_keys),
            "execute_command": execute_command,
        })
        return 0
    except Exception as exc:
        emit({"primitive": "local_search_pipeline", "status": "failed", "error": str(exc)})
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local-only Powerpacks search against a DuckDB index")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Extract query payload and emit a local run command")
    prepare.add_argument("--query", required=True)
    prepare.add_argument("--db", default=".powerpacks/search-index/local-search.duckdb")
    prepare.add_argument("--env-file", default=".env")
    prepare.add_argument("--output-dir")
    prepare.add_argument("--model")
    prepare.add_argument("--timeout", type=int, default=120)
    prepare.set_defaults(func=cmd_prepare)

    run = sub.add_parser("run", help="Run local search from a prepared payload")
    run.add_argument("--query")
    run.add_argument("--payload-json")
    run.add_argument("--state")
    run.add_argument("--ledger")
    run.add_argument("--db", default=".powerpacks/search-index/local-search.duckdb")
    run.add_argument("--env-file", default=".env")
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--top-k", type=int, default=1000)
    run.add_argument("--timeout", type=int, default=600)
    run.add_argument("--llm-timeout", type=int, default=3600, help="Timeout for LLM filter/rerank steps")
    run.add_argument("--force", action="store_true")
    run.add_argument("--search-only", action="store_true", help="Skip LLM filter/rerank after retrieval + hydration (data path stays fully local either way)")
    run.add_argument("--filter-only", action="store_true", help="Run the cheap conservative LLM filter but skip LLM rerank")
    run.add_argument(
        "--extra-candidates-json",
        help="JSON file with agentic SQL vertical people (search-sql skill output); unioned into retrieval so they go through the same hydration and LLM filter/rerank as every other candidate",
    )
    run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
