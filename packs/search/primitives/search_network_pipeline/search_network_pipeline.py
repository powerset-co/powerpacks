#!/usr/bin/env python3
"""Resumable orchestrator for the search-network primitive pipeline, both backends.

One orchestrator, one `--backend {powerset,local}` switch:
- `powerset` (default): TurboPuffer retrieval + Postgres hydration, scoped by set/operators.
- `local`: the same mechanical primitives against the local DuckDB index. Scope is the
  DuckDB file itself — no set_id, operator resolution, Postgres, or TurboPuffer. Children
  bind the backend via POWERPACKS_LOCAL_SEARCH_DB in their environment (shared/search_backend_mode);
  company/education resolution dispatches to the local/ resolve primitives.

This runner can prepare the parallel `expand_search_request` payload, then run
the mechanical retrieval, hydration, LLM filter/rerank, and persistence steps.
For manual runs it needs either an existing task `--state` or a `--query` plus
`--payload-json` containing the `expand_search_request` shape.
"""
from __future__ import annotations

import argparse, hashlib, json, os, re, shlex, subprocess, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
PRIMITIVES_DIR = ROOT / "packs/search/primitives"
LIB_DIR = PRIMITIVES_DIR / "lib"
SHARED_DIR = PRIMITIVES_DIR / "shared"
LOCAL_DIR = PRIMITIVES_DIR / "local"
TURBOPUFFER_DIR = PRIMITIVES_DIR / "turbopuffer"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))
from seniority_bands import parse_pinned_seniority_bands, pin_payload_seniority_bands, pin_payload_current_role, pin_payload_semantic_query  # noqa: E402
from search_common import apply_trait_currentness  # noqa: E402
DEFAULT_MODEL = "gpt-5.1"
DEFAULT_REASONING_EFFORT = os.environ.get("LLM_RERANK_REASONING_EFFORT", "low")
DEFAULT_FILTER_BATCH_SIZE = int(os.environ.get("POWERPACKS_LLM_FILTER_BATCH_SIZE", "2"))
DEFAULT_FILTER_CONCURRENCY = int(os.environ.get("POWERPACKS_LLM_FILTER_CONCURRENCY", os.environ.get("SEARCH_V2_LLM_FILTER_MAX_CONCURRENT", "1000")))
DEFAULT_RERANK_CONCURRENCY = int(os.environ.get("LLM_RERANK_CONCURRENCY", os.environ.get("SEARCH_V2_RERANK_MAX_CONCURRENT", "400")))
PAYLOAD_KEYS = {"intent_type", "source_type", "normalized_query", "vertical", "role_search_filters", "notes"}
LOCAL_PAYLOAD_KEYS = PAYLOAD_KEYS | {"traits"}
DEFAULT_LOCAL_DB = ".powerpacks/search-index/local-search.duckdb"
DEFAULT_TOP_K = {"powerset": 10000, "local": 1000}
REMOTE_SCOPE_KEYS = {"set_id", "operator_ids", "allowed_operator_ids", "searcher_operator_id"}
UNSUPPORTED_LOCAL_FILTERS = {
    "investor_names",
    "operator_interaction_min",
    "operator_interaction_max",
    "set_interaction_min",
    "set_interaction_max",
}
COMPANY_RESOLVE_FILTER_KEYS = {
    "company_names", "company_ids", "current_company_names", "company_semantic_queries",
    "sector_types", "entity_types", "technology_types", "customer_types", "customer_type",
    "company_cities", "company_states", "company_countries", "company_metro_areas",
    "company_macro_regions", "funding_stage_min", "funding_stage_max", "funding_amount_min",
    "funding_amount_max", "headcount_min", "headcount_max", "valuation_min", "valuation_max",
    "founded_year_min", "founded_year_max", "last_funding_after", "last_funding_before",
    "yc_batches", "accelerators", "stages", "company_stages", "stage",
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
BROAD_POOL_RATIO = 0.6

class Blocked(Exception):
    def __init__(self, payload: dict[str, Any], code: int = 20):
        self.payload, self.code = payload, code
        super().__init__(payload.get("message", "blocked"))

class Failed(Exception): pass

def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def emit(x: Any) -> None: print(json.dumps(x, indent=2, sort_keys=True))
def read_json(p: Path, default=None):
    if not p.exists(): return default
    try: return json.loads(p.read_text())
    except Exception: return default

def write_json(p: Path, x: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(x, indent=2, sort_keys=True)+"\n")

def parse_jsons(s: str) -> list[Any]:
    out=[]; dec=json.JSONDecoder(); i=0
    while i < len(s):
        while i < len(s) and s[i].isspace(): i += 1
        if i >= len(s): break
        try: obj,end=dec.raw_decode(s,i); out.append(obj); i=end
        except json.JSONDecodeError:
            j=s.find("{", i+1)
            if j < 0: break
            i=j
    return out

def run(cmd: list[str], *, env_file: str = ".env", timeout: int = 600, stream_stderr: bool = False, skip_env_files: bool = False, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    env=dict(os.environ)
    if extra_env: env.update(extra_env)
    if not skip_env_files:
        for f in [ROOT/env_file, (ROOT/"../network-search-api/.env").resolve()]:
            if f.exists():
                for line in f.read_text(errors="ignore").splitlines():
                    if not line.strip() or line.lstrip().startswith("#") or "=" not in line: continue
                    k,v=line.split("=",1)
                    if k not in env and v.strip(): env[k]=v.strip().strip('"').strip("'")
    t=time.monotonic()
    if not stream_stderr:
        p=subprocess.run(cmd, cwd=ROOT, env=env, text=True, capture_output=True, timeout=timeout)
        stdout, stderr, returncode = p.stdout, p.stderr, p.returncode
    else:
        p=subprocess.Popen(cmd, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout_parts: list[str]=[]; stderr_parts: list[str]=[]
        def drain(pipe, parts: list[str], *, echo: bool=False) -> None:
            if pipe is None: return
            for line in pipe:
                parts.append(line)
                if echo:
                    sys.stderr.write(line); sys.stderr.flush()
        out_thread=threading.Thread(target=drain,args=(p.stdout,stdout_parts),daemon=True)
        err_thread=threading.Thread(target=drain,args=(p.stderr,stderr_parts),kwargs={"echo":True},daemon=True)
        out_thread.start(); err_thread.start()
        try:
            returncode=p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill(); returncode=p.wait(); stderr_parts.append(f"\nprocess timed out after {timeout}s\n")
        out_thread.join(timeout=2); err_thread.join(timeout=2)
        stdout, stderr = "".join(stdout_parts), "".join(stderr_parts)
    js=parse_jsons(stdout or "")
    return {"cmd":cmd,"returncode":returncode,"stdout":stdout,"stderr":stderr,"elapsed_seconds":round(time.monotonic()-t,3),"json_objects":js,"json":js[-1] if js else None}

def require_ok(res: dict[str, Any], step: str) -> dict[str, Any]:
    if res["returncode"] != 0:
        raise Failed(f"{step} failed rc={res['returncode']}: {((res.get('stderr') or res.get('stdout') or '').strip())[-1200:]}")
    return res.get("json") or {}

def ledger_path_for(state: Path|None, explicit: Path|None) -> Path:
    if explicit: return explicit
    if state: return Path(str(state)+".pipeline.json")
    return ROOT/".powerpacks/runs/search-network-pipeline.json"

def load_ledger(p: Path) -> dict[str, Any]:
    x=read_json(p,{}) or {}; x.setdefault("created_at", now()); x.setdefault("steps",{}); x.setdefault("approvals",{}); x.setdefault("artifacts",{}); return x

def save(p: Path, l: dict[str, Any]) -> None: l["updated_at"]=now(); write_json(p,l)
def done(l: dict[str, Any], step: str) -> bool: return l.get("steps",{}).get(step,{}).get("status")=="completed"
def mark(p: Path, l: dict[str, Any], step: str, status: str, **kw) -> None:
    r=l.setdefault("steps",{}).setdefault(step,{"id":step}); r.update(status=status, **kw)
    if status in {"completed","skipped","failed","blocked_approval","blocked_user_action"}: r["finished_at"]=now()
    save(p,l)

def approval_id(kind: str, payload: dict[str, Any]) -> str:
    return kind+"_"+hashlib.sha1(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:12]

def is_approved(l: dict[str, Any], aid: str) -> bool: return bool(l.get("approvals",{}).get(aid,{}).get("confirmed"))

def uv_python_command(args, subcommand: str, lp: Path, extra: str = "") -> str:
    env_file=getattr(args,"env_file",".env") or ".env"
    base=(
        f"uv run --env-file {shlex.quote(env_file)} --project . python "
        f"packs/search/primitives/search_network_pipeline/search_network_pipeline.py {subcommand} "
        f"--ledger {shlex.quote(str(lp))}"
    )
    return base + (" " + extra if extra else "")

def payload_from_expand_output(out: dict[str, Any], *, backend: str = "powerset") -> dict[str, Any]:
    keys = LOCAL_PAYLOAD_KEYS if backend == "local" else PAYLOAD_KEYS
    return {k:v for k,v in out.items() if k in keys}

def comparable_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()

def payload_quality_issues(payload: dict[str, Any]) -> list[str]:
    f=payload.get("role_search_filters") if isinstance(payload.get("role_search_filters"),dict) else {}
    sq=f.get("semantic_query")
    bm25=[x for x in (f.get("bm25_queries") or []) if isinstance(x,str) and x.strip()]
    has_role_or_profile_intent=bool(sq or bm25 or f.get("role_ids") or f.get("role_names") or f.get("titles"))
    issues=[]
    if has_role_or_profile_intent:
        if not isinstance(sq,str) or len(sq.strip()) < 80:
            issues.append("role/profile intent needs role_search_filters.semantic_query prose with at least 80 characters")
        elif any(comparable_text(sq)==comparable_text(x) for x in bm25):
            issues.append("semantic_query must not duplicate a bm25/title phrase")
    return issues

def payload_quality_issues_local(payload: dict[str, Any]) -> list[str]:
    filters = payload_filters(payload)
    semantic_query = filters.get("semantic_query")
    bm25 = [item for item in (filters.get("bm25_queries") or []) if isinstance(item, str) and item.strip()]
    has_role_intent = bool(semantic_query or bm25 or filters.get("role_ids") or filters.get("role_names") or filters.get("titles") or filters.get("role_tracks"))
    issues: list[str] = []
    if has_role_intent and bm25 and (not isinstance(semantic_query, str) or len(semantic_query.strip()) < 80):
        issues.append("role/profile intent will run BM25/filter-only unless semantic_query has at least 80 characters")
    return issues

def compact_preview(payload: dict[str, Any], payload_json: Path, quality_issues: list[str]) -> dict[str, Any]:
    f=payload.get("role_search_filters") if isinstance(payload.get("role_search_filters"),dict) else {}
    filters={}
    for k in [
        "company_names","company_ids","company_semantic_queries","investor_names",
        "education_names","education_ids","metro_areas","cities","states","countries",
        "macro_regions","seniority_bands","years_experience_min","years_experience_max",
        "position_after_date","position_before_date","is_current_role","is_current_company",
        "tech_skills","x_followers_min","li_followers_min","operator_interaction_min",
    ]:
        v=f.get(k)
        if v not in (None, [], ""):
            filters[k]=v
    role={}
    for k in ["semantic_query","bm25_queries","role_ids"]:
        v=f.get(k)
        if v not in (None, [], ""):
            role[k]=v
    return {
        "normalized_query": payload.get("normalized_query"),
        "payload_json": str(payload_json),
        "set_scope": f.get("set_id") or "env/default set or personal-set fallback",
        "role_title_intent": role or None,
        "filters": filters,
        "runtime_blockers": quality_issues,
    }

def company_directory_tool_args(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return MCP args for company-only people lookup, else None.

    This protects the company-directory fast path even if a harness calls
    `prepare` for a simple "people at Company" query.
    """
    f=payload.get("role_search_filters") if isinstance(payload.get("role_search_filters"),dict) else {}
    company_names=[x for x in (f.get("company_names") or f.get("current_company_names") or []) if isinstance(x,str) and x.strip()]
    company_ids=[x for x in (f.get("company_ids") or []) if str(x).strip()]
    if not company_names and not company_ids:
        return None
    if f.get("has_domain_intent") is True:
        return None
    allowed={"company_names","current_company_names","company_ids","is_current_company","set_id","has_domain_intent"}
    if any(v not in (None, [], "") and k not in allowed for k,v in f.items()):
        return None
    args={"page":0,"page_size":50,"company_limit":5}
    if company_ids:
        args["company_id"]=str(company_ids[0])
    else:
        args["company_name"]=company_names[0]
    if f.get("set_id"):
        args["set_id"]=f["set_id"]
    return args

def prepare_output_dir(query: str, explicit: str|None) -> Path:
    if explicit:
        p=Path(explicit); return p if p.is_absolute() else ROOT/p
    slug=re.sub(r"[^a-z0-9]+","-",query.lower()).strip("-")[:60] or "query"
    rid=hashlib.sha1(f"{query}:{time.time()}".encode()).hexdigest()[:10]
    return ROOT/".powerpacks/search"/f"{rid}-{slug}"

def block(lp: Path, l: dict[str, Any], args, kind: str, step: str, payload: dict[str, Any], msg: str):
    aid=approval_id(kind,payload)
    approve=uv_python_command(args,"approve",lp,f"{kind} --approval-id {aid} --confirm")
    cont=uv_python_command(args,"continue",lp)
    b={"primitive":"search_network_pipeline","status":"blocked_approval","approval_type":kind,"approval_id":aid,"message":msg,"payload":payload,"ledger":str(lp),"continue_command":f"{approve} && {cont}"}
    l["current_block"]=b; mark(lp,l,step,"blocked_approval",summary=compact_summary(b)); raise Blocked(b)

LARGE_LIST_KEYS={"candidate_ids","candidates","company_union_candidate_ids","company_union_candidates","base_candidate_ids","profile_ids","rows","people"}
ARTIFACT_KEYS={"state","retrieval_artifact","profiles_path","llm_profiles_path","csv","jsonl","manifest","artifact_dir","query_results_csv","raw_rerank_results_jsonl","scores_jsonl","filtered_jsonl","batch_prompts_jsonl"}
COUNT_KEYS={"resolved_count","hard_semantic_count","base_candidate_count","company_union_candidate_count","returned_people","hydrated","requested","row_count","frontier_count","hydrated_count","position_rows_count","unique_people_count","candidate_count","scored_count","passed_count","filtered_count","ranked_count"}
MODE_KEYS={"search_mode","retrieval_mode","prefilter_short_circuit","base_id_batch_count","base_id_batch_size","company_union_added","limit","top_k","profiles_compressed"}

def compact_summary(value: Any) -> Any:
    if isinstance(value, list):
        return {"count":len(value)} if len(value)>20 else [compact_summary(v) for v in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any]={}
    for k,v in value.items():
        if k in LARGE_LIST_KEYS:
            out[k+"_count" if not k.endswith("_count") else k]=len(v) if isinstance(v,list) else 0
        elif k in ARTIFACT_KEYS or k in COUNT_KEYS or k in MODE_KEYS or k in {"primitive","status","approval_type","approval_id","message","ledger","continue_command","error","namespace","query","task_id","created_at","source"}:
            out[k]=compact_summary(v)
        elif k=="artifacts" and isinstance(v,dict):
            out[k]={ak:av for ak,av in v.items() if isinstance(av,(str,int,float,bool))}
        elif k.endswith("_count") or k.endswith("_path"):
            out[k]=v
    return out

def collect_artifacts(summary: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any]={}
    for k in ARTIFACT_KEYS:
        v=summary.get(k)
        if isinstance(v,str) and v:
            out[k]=v
    artifacts=summary.get("artifacts")
    if isinstance(artifacts,dict):
        for k,v in artifacts.items():
            if isinstance(v,str) and v:
                out[k]=v
    return out

def pipeline_summary(l: dict[str, Any]) -> dict[str, Any]:
    steps=l.get("steps",{}) or {}
    def s(step: str) -> dict[str, Any]:
        return (steps.get(step,{}) or {}).get("summary",{}) or {}
    resolved=s("resolve_companies")
    pre=s("apply_prefilters")
    retrieval=s("execute_role_search")
    hydrate=s("hydrate_people")
    llm_filter=s("llm_filter_candidates")
    rerank=s("llm_rerank_candidates")
    persist=s("persist_search_results")
    return {k:v for k,v in {
        "resolved_companies": resolved.get("resolved_count"),
        "search_mode": retrieval.get("search_mode") or pre.get("search_mode"),
        "retrieval_mode": retrieval.get("retrieval_mode"),
        "base_candidates": pre.get("base_candidate_count"),
        "company_union_candidates": pre.get("company_union_candidate_count") or retrieval.get("company_union_candidate_count"),
        "company_union_added": retrieval.get("company_union_added"),
        "returned_people": retrieval.get("returned_people"),
        "hydrated": hydrate.get("hydrated"),
        "llm_scored": llm_filter.get("scored_count"),
        "llm_passed": llm_filter.get("passed_count"),
        "llm_filtered": llm_filter.get("filtered_count"),
        "ranked": rerank.get("ranked_count"),
        "rows": persist.get("row_count"),
    }.items() if v is not None}

def state_has_step(state: Path, step: str) -> bool:
    s=read_json(state,{}) or {}; return any(x.get("id")==step for x in s.get("steps",[]))

def pinned_bands_from_args(args) -> list[str]:
    raw=getattr(args,"seniority_bands",None)
    if not raw: return []
    try: return parse_pinned_seniority_bands(raw)
    except ValueError as e: raise Failed(str(e)) from e

# ---------------------------------------------------------------------------
# Local-backend payload shaping (moved from the retired local_search_pipeline)
# ---------------------------------------------------------------------------

def is_present(value: Any) -> bool:
    return value not in (None, "", [], {})

def payload_filters(payload: dict[str, Any]) -> dict[str, Any]:
    filters = payload.get("role_search_filters")
    return dict(filters) if isinstance(filters, dict) else {}

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
    scalar local filters. Normalizing at this boundary lets the local backend
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
    # Trait temporals are the extractor's currentness contract; honor them at
    # this boundary the same way role_payload_from_state does on the state path
    # (e.g. a temporal=current role trait must become is_current_role=true so
    # past senior/staff positions don't admit people who have since moved on).
    normalized = apply_trait_currentness(normalized, payload.get("traits"))
    out = dict(payload)
    out["role_search_filters"] = normalized
    out.setdefault("intent_type", "role_search")
    out.setdefault("source_type", payload.get("source_type") or ("prod_expand_query" if "filters" in payload else "query"))
    out.setdefault("normalized_query", payload.get("normalized_query") or payload.get("original_query") or query)
    out.setdefault("vertical", payload.get("vertical") or "people")
    return out

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

    # There is no concept of a set/operator locally — scope is the DuckDB
    # file itself. Remove the keys outright rather than relying on every
    # downstream consumer to ignore them.
    for key in REMOTE_SCOPE_KEYS:
        sanitized.pop(key, None)
        filters.pop(key, None)

    unsupported = sorted(key for key in UNSUPPORTED_LOCAL_FILTERS if is_present(filters.get(key)))
    if unsupported:
        raise Failed(f"local backend does not support these remote-only filters yet: {', '.join(unsupported)}")

    sanitized["role_search_filters"] = filters
    notes = sanitized.get("notes")
    if not isinstance(notes, list):
        notes = []
    if ignored_scope_keys:
        notes.append(f"local backend ignores remote scope keys: {', '.join(ignored_scope_keys)}")
    notes.append("Local search scope is the DuckDB file; no set_id/operator resolution is used.")
    sanitized["notes"] = notes
    return sanitized, ignored_scope_keys

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
        # Lazy by design: the import firewall requires this module to import
        # cleanly with DuckDB/local modules absent (remote-only installs).
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

def configure_local_backend_mode(db_path: Path) -> None:
    """Mark this process as local-backend before any filter construction.

    Shared filter helpers branch on backend mode: in remote mode an env
    POWERPACKS_DEFAULT_SET_ID resolves through Postgres into an
    allowed_operator_ids filter. Local search scope is the DuckDB file, so
    in-process payload transforms (pool estimate, title clustering) must run
    with local mode configured or a foreign set id silently zeroes the pool
    and prepare makes a network call.
    """
    for _path in [LIB_DIR, SHARED_DIR, LOCAL_DIR, TURBOPUFFER_DIR]:
        if str(_path) not in sys.path:
            sys.path.insert(0, str(_path))
    import search_backend_mode  # type: ignore

    search_backend_mode.configure_local_backend(db_path)

def local_pool_estimate(payload: dict[str, Any], db_path: Path) -> dict[str, Any]:
    """Cheap filter-eligibility count so breadth is visible before LLM spend."""
    if not db_path.exists():
        return {"status": "skipped_no_db"}
    try:
        configure_local_backend_mode(db_path)
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

def compact_preview_local(payload: dict[str, Any], payload_json: Path, db_path: Path, removed_scope_keys: list[str]) -> dict[str, Any]:
    filters = payload_filters(payload)
    visible_filters = {}
    for key in PREVIEW_FILTER_KEYS:
        value = filters.get(key)
        if is_present(value):
            visible_filters[key] = value
    role = {key: filters.get(key) for key in ["semantic_query", "bm25_queries", "role_ids", "role_tracks"] if is_present(filters.get(key))}
    runtime_notes = payload_quality_issues_local(payload)
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
        elif matched <= max(3, total // 100):
            runtime_notes.append(
                f"suspiciously narrow pool: hard filters match only {matched} of {total} people; "
                "check the extracted hard filters before executing — a seniority band inferred from a role noun "
                "(e.g. 'product managers' -> seniority_bands: [manager]) is a common cause; recommend modify"
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

def local_db_path(args) -> Path:
    return Path(getattr(args, "db", None) or DEFAULT_LOCAL_DB).expanduser().resolve()

def local_run_kwargs(args, db_path: Path) -> dict[str, Any]:
    """Child-process env contract for local mode: bind the backend via env var,
    never merge .env (a foreign POWERPACKS_DEFAULT_SET_ID must not leak into a
    local run)."""
    return {"env_file": "/dev/null", "skip_env_files": True, "extra_env": {"POWERPACKS_LOCAL_SEARCH_DB": str(db_path)}}

def init_state(args, lp: Path, l: dict[str, Any]) -> Path:
    pinned_bands=pinned_bands_from_args(args)
    pin_current=bool(getattr(args,"current_role",False))
    if args.state or (l.get("state") and not (args.query and args.payload_json)):
        if pinned_bands:
            raise Failed("--seniority-bands only applies when the run starts from --query plus --payload-json; an existing --state already recorded its expand_search_request filters")
        if pin_current:
            raise Failed("--current-role only applies when the run starts from --query plus --payload-json; an existing --state already recorded its expand_search_request filters")
        if args.state:
            state=Path(args.state); l["state"]=str(state); save(lp,l); return state
        return Path(l["state"])
    if not args.query or not args.payload_json:
        b={"primitive":"search_network_pipeline","status":"blocked_user_action","message":"Need --state, or --query plus --payload-json from expand_search_request.","ledger":str(lp)}
        l["current_block"]=b; mark(lp,l,"init_state","blocked_user_action",summary=b); raise Blocked(b,21)
    cmd=[sys.executable,str(ROOT/"packs/search/primitives/task_state/task_state.py"),"init","--query",args.query]
    res=run(cmd, env_file=args.env_file, timeout=args.timeout); out=require_ok(res,"task_state init")
    state=Path(out["state"]); l["state"]=str(state); l.setdefault("artifacts",{})["state"]=str(state)
    mark(lp,l,"init_state","completed",summary=compact_summary(out),command=" ".join(cmd))
    payload=read_json(Path(args.payload_json))
    expand_output = payload if isinstance(payload, dict) and "role_search_filters" in payload else {"role_search_filters": payload}
    if pinned_bands:
        expand_output=pin_payload_seniority_bands(expand_output, pinned_bands)
    if pin_current:
        expand_output=pin_payload_current_role(expand_output, True)
    cmd=[sys.executable,str(ROOT/"packs/search/primitives/task_state/task_state.py"),"record-step","--state",str(state),"--step-id","expand_search_request","--status","completed","--output-json",json.dumps(expand_output)]
    out=require_ok(run(cmd, env_file=args.env_file, timeout=args.timeout),"record expand_search_request")
    mark(lp,l,"record_expand_search_request","completed",summary=compact_summary(out),command=" ".join(cmd))
    save(lp,l); return state

def init_state_local(args, lp: Path, l: dict[str, Any], payload: dict[str, Any], run_kwargs: dict[str, Any]) -> Path:
    if args.state:
        state=Path(args.state); l["state"]=str(state); save(lp,l); return state
    if l.get("state") and not (args.query and args.payload_json):
        return Path(str(l["state"]))
    if not args.query:
        raise Failed("Need --state or --query")
    cmd=[sys.executable,str(ROOT/"packs/search/primitives/task_state/task_state.py"),"init","--query",args.query]
    out=require_ok(run(cmd, timeout=args.timeout, **run_kwargs),"task_state init")
    state=Path(out["state"]); l["state"]=str(state); l.setdefault("artifacts",{})["state"]=str(state)
    mark(lp,l,"init_state","completed",summary=compact_summary(out),command=" ".join(shlex.quote(x) for x in cmd))
    cmd=[sys.executable,str(ROOT/"packs/search/primitives/task_state/task_state.py"),"record-step","--state",str(state),"--step-id","expand_search_request","--status","completed","--output-json",json.dumps(payload)]
    out=require_ok(run(cmd, timeout=args.timeout, **run_kwargs),"record expand_search_request")
    mark(lp,l,"record_expand_search_request","completed",summary=compact_summary(out),command=" ".join(shlex.quote(x) for x in cmd))
    save(lp,l); return state

def latest_step(state: Path, step_id: str) -> dict[str, Any]:
    data = read_json(state, {}) or {}
    for step in reversed(data.get("steps", [])):
        if step.get("id") == step_id:
            return step.get("output") or {}
    return {}

def maybe_payload_filters(state: Path) -> dict[str, Any]:
    s=read_json(state,{}) or {}
    for step in reversed(s.get("steps",[])):
        if step.get("id")=="expand_search_request":
            return ((step.get("output") or {}).get("role_search_filters") or {})
    return {}

def run_pipeline(args) -> dict[str, Any]:
    lp=ledger_path_for(Path(args.state) if args.state else None, Path(args.ledger) if args.ledger else None)
    l=load_ledger(lp); l["current_block"]=None; save(lp,l)
    state=init_state(args,lp,l)
    top_k=args.top_k if args.top_k is not None else DEFAULT_TOP_K["powerset"]
    steps=[("resolve_set_operators",[sys.executable,str(ROOT/"packs/search/primitives/resolve_set_operators/resolve_set_operators.py"),"--state",str(state),"--env-file",args.env_file,"--write-state"])]
    f=maybe_payload_filters(state)
    if f.get("investor_names"):
        steps.append(("resolve_investors",[sys.executable,str(ROOT/"packs/search/primitives/resolve_investors/resolve_investors.py"),"--state",str(state),"--env-file",args.env_file,"--write-state"]))
    if f.get("company_names") or f.get("company_ids") or f.get("current_company_names") or f.get("company_semantic_queries") or f.get("sector_types") or f.get("investor_names"):
        steps.append(("resolve_companies",[sys.executable,str(ROOT/"packs/search/primitives/turbopuffer/turbopuffer_resolve_companies.py"),"--state",str(state),"--env-file",args.env_file,"--write-state"]))
    if f.get("education_names"):
        steps.append(("resolve_education",[sys.executable,str(ROOT/"packs/search/primitives/turbopuffer/turbopuffer_resolve_education.py"),"--state",str(state),"--env-file",args.env_file,"--write-state"]))
    steps += [
        ("apply_prefilters",[sys.executable,str(ROOT/"packs/search/primitives/apply_prefilters/apply_prefilters.py"),"--state",str(state),"--env-file",args.env_file,"--write-state"]),
        ("execute_role_search",[sys.executable,str(ROOT/"packs/search/primitives/execute_role_search/execute_role_search.py"),"--state",str(state),"--env-file",args.env_file,"--write-state","--limit",str(args.limit),"--top-k",str(top_k)]),
        ("hydrate_people",[sys.executable,str(ROOT/"packs/search/primitives/hydrate_people/hydrate_people.py"),"--state",str(state),"--env-file",args.env_file,"--write-state"]),
    ]
    for step,cmd in steps:
        if done(l,step) and not args.force: continue
        mark(lp,l,step,"running",command=" ".join(shlex.quote(x) for x in cmd))
        out=require_ok(run(cmd, env_file=args.env_file, timeout=args.timeout),step)
        l.setdefault("artifacts",{}).update(collect_artifacts(out))
        mark(lp,l,step,"completed",summary=compact_summary(out),command=" ".join(shlex.quote(x) for x in cmd))
    if not args.search_only:
        payload={
            "state":str(state),
            "model":args.model,
            "mode":"filter_only" if args.filter_only else "filter_rerank",
            "filter_batch_size":args.filter_batch_size,
            "filter_concurrency":args.filter_concurrency,
            "rerank_concurrency":args.rerank_concurrency,
            "reasoning_effort":args.reasoning_effort,
        }; aid=approval_id("llm",payload)
        if not is_approved(l,aid) and not args.confirm_llm and not args.execute_approved:
            block(
                lp,
                l,
                args,
                "llm",
                "llm_filter_rerank",
                payload,
                "Run LLM filter + rerank for this search? This may spend OpenAI credits and usually takes 2-3 minutes.",
            )
        llm_steps=[
            ("llm_filter_candidates",[sys.executable,str(ROOT/"packs/search/primitives/llm_filter_candidates/llm_filter_candidates.py"),"--state",str(state),"--profile-scope","auto","--batch-size",str(args.filter_batch_size),"--concurrency",str(args.filter_concurrency),"--write-state"]),
        ]
        if not args.filter_only:
            llm_steps.append(("llm_rerank_candidates",[sys.executable,str(ROOT/"packs/search/primitives/llm_rerank_candidates/llm_rerank_candidates.py"),"--state",str(state),"--concurrency",str(args.rerank_concurrency),"--model",args.model,"--reasoning-effort",args.reasoning_effort,"--write-state"]))
        for step,cmd in llm_steps:
            if done(l,step) and not args.force: continue
            mark(lp,l,step,"running",command=" ".join(shlex.quote(x) for x in cmd))
            out=require_ok(run(cmd, env_file=args.env_file, timeout=args.llm_timeout, stream_stderr=True),step)
            l.setdefault("artifacts",{}).update(collect_artifacts(out))
            mark(lp,l,step,"completed",summary=compact_summary(out),command=" ".join(shlex.quote(x) for x in cmd))
    if not done(l,"persist_search_results") or args.force:
        cmd=[sys.executable,str(ROOT/"packs/search/primitives/persist_search_results/results_io.py"),"export","--state",str(state)]
        mark(lp,l,"persist_search_results","running",command=" ".join(cmd)); out=require_ok(run(cmd, env_file=args.env_file, timeout=args.timeout),"persist_search_results"); l.setdefault("artifacts",{}).update(collect_artifacts(out)); mark(lp,l,"persist_search_results","completed",summary=compact_summary(out),command=" ".join(cmd))
    l["current_block"]=None; save(lp,l)
    return {"primitive":"search_network_pipeline","status":"completed","ledger":str(lp),"state":str(state),"summary":pipeline_summary(l),"artifacts":l.get("artifacts",{})}

def run_pipeline_local(args) -> dict[str, Any]:
    db_path=local_db_path(args)
    if not db_path.exists():
        raise Failed(f"local DuckDB does not exist: {db_path}")
    configure_local_backend_mode(db_path)
    run_kwargs=local_run_kwargs(args, db_path)
    pinned_bands=pinned_bands_from_args(args)
    pin_current=bool(getattr(args,"current_role",False))
    if pinned_bands and args.state and not args.payload_json:
        raise Failed("--seniority-bands only applies when the run starts from --payload-json; an existing --state already recorded its expand_search_request filters")
    if pin_current and args.state and not args.payload_json:
        raise Failed("--current-role only applies when the run starts from --payload-json; an existing --state already recorded its expand_search_request filters")

    lp=ledger_path_for(Path(args.state) if args.state else None, Path(args.ledger) if args.ledger else None)
    l=load_ledger(lp)
    l["mode"]="local_duckdb"; l["duckdb"]=str(db_path); l["current_block"]=None
    save(lp,l)

    if args.payload_json:
        payload=read_json(Path(args.payload_json))
    elif args.state:
        payload=latest_step(Path(args.state),"expand_search_request")
    else:
        payload={}
    if not isinstance(payload,dict) or not payload:
        raise Failed("Need --payload-json, or --state with an expand_search_request step")
    payload=normalize_query_expansion_payload(payload, query=args.query)
    if pinned_bands:
        payload=pin_payload_seniority_bands(payload, pinned_bands)
    if pin_current:
        payload=pin_payload_current_role(payload, True)
    payload, ignored_scope_keys=prepare_local_payload(payload)
    payload=apply_local_title_clustering(payload, db_path)
    if args.payload_json:
        sanitized_path=lp.parent/f"{lp.stem}.local-payload.json"
        write_json(sanitized_path,payload)
        l.setdefault("artifacts",{})["payload_json"]=str(sanitized_path)
        l["ignored_remote_scope_keys"]=ignored_scope_keys
        save(lp,l)

    state=init_state_local(args,lp,l,payload,run_kwargs)
    filters=payload_filters(payload)
    top_k=args.top_k if args.top_k is not None else DEFAULT_TOP_K["local"]

    steps: list[tuple[str, list[str]]]=[]
    if any(is_present(filters.get(key)) for key in COMPANY_RESOLVE_FILTER_KEYS):
        steps.append(("resolve_companies",[sys.executable,str(LOCAL_DIR/"local_resolve_companies.py"),"--state",str(state),"--env-file","/dev/null","--write-state"]))
    if filters.get("education_names"):
        steps.append(("resolve_education",[sys.executable,str(LOCAL_DIR/"local_resolve_education.py"),"--state",str(state),"--env-file","/dev/null","--write-state"]))
    steps += [
        ("apply_prefilters",[sys.executable,str(ROOT/"packs/search/primitives/apply_prefilters/apply_prefilters.py"),"--state",str(state),"--env-file","/dev/null","--write-state"]),
        ("execute_role_search",[
            sys.executable,str(ROOT/"packs/search/primitives/execute_role_search/execute_role_search.py"),
            "--state",str(state),"--env-file","/dev/null","--write-state",
            "--limit",str(args.limit),"--top-k",str(top_k),
            *(["--extra-candidates-json",str(Path(args.extra_candidates_json).expanduser().resolve())] if getattr(args,"extra_candidates_json",None) else []),
        ]),
        ("hydrate_people",[sys.executable,str(ROOT/"packs/search/primitives/hydrate_people/hydrate_people.py"),"--state",str(state),"--env-file","/dev/null","--write-state","--local-db",str(db_path)]),
    ]
    for step,cmd in steps:
        if done(l,step) and not args.force: continue
        mark(lp,l,step,"running",command=" ".join(shlex.quote(x) for x in cmd))
        out=require_ok(run(cmd, timeout=args.timeout, **run_kwargs),step)
        l.setdefault("artifacts",{}).update(collect_artifacts(out))
        mark(lp,l,step,"completed",summary=compact_summary(out),command=" ".join(shlex.quote(x) for x in cmd))

    # LLM filter/rerank are backend-agnostic: they read hydrated profiles from
    # task state and call OpenAI only (the data path stays fully local). Skip
    # them when retrieval came back empty or when the caller asked search-only.
    hydrated_count=int((l.get("steps",{}).get("hydrate_people",{}) or {}).get("summary",{}).get("hydrated") or 0)
    if not args.search_only and hydrated_count > 0:
        llm_steps=[("llm_filter_candidates",[sys.executable,str(ROOT/"packs/search/primitives/llm_filter_candidates/llm_filter_candidates.py"),"--state",str(state),"--profile-scope","auto","--write-state"])]
        if not args.filter_only:
            llm_steps.append(("llm_rerank_candidates",[sys.executable,str(ROOT/"packs/search/primitives/llm_rerank_candidates/llm_rerank_candidates.py"),"--state",str(state),"--write-state"]))
        for step,cmd in llm_steps:
            if not (done(l,step) and not args.force):
                mark(lp,l,step,"running",command=" ".join(shlex.quote(x) for x in cmd))
                # LLM children DO need .env (OPENAI_API_KEY) but must stay in
                # local backend mode, so keep the env var and load env files.
                out=require_ok(run(cmd, env_file=args.env_file, timeout=args.llm_timeout, stream_stderr=True, extra_env=run_kwargs["extra_env"]),step)
                l.setdefault("artifacts",{}).update(collect_artifacts(out))
                mark(lp,l,step,"completed",summary=compact_summary(out),command=" ".join(shlex.quote(x) for x in cmd))
            if step=="llm_filter_candidates":
                passed=int((l.get("steps",{}).get(step,{}) or {}).get("summary",{}).get("passed_count") or 0)
                if passed==0:
                    break

    if not done(l,"persist_search_results") or args.force:
        cmd=[sys.executable,str(ROOT/"packs/search/primitives/persist_search_results/results_io.py"),"export","--state",str(state)]
        mark(lp,l,"persist_search_results","running",command=" ".join(cmd))
        out=require_ok(run(cmd, timeout=args.timeout, **run_kwargs),"persist_search_results")
        l.setdefault("artifacts",{}).update(collect_artifacts(out))
        mark(lp,l,"persist_search_results","completed",summary=compact_summary(out),command=" ".join(cmd))

    l["current_block"]=None; save(lp,l)
    return {
        "primitive":"search_network_pipeline",
        "status":"completed",
        "mode":"local_duckdb",
        "duckdb":str(db_path),
        "ledger":str(lp),
        "state":str(state),
        "ignored_remote_scope_keys":ignored_scope_keys,
        "summary":pipeline_summary(l),
        "artifacts":l.get("artifacts",{}),
    }

def cmd_run(args):
    try:
        emit(run_pipeline_local(args) if getattr(args,"backend","powerset")=="local" else run_pipeline(args)); return 0
    except Blocked as e: emit(e.payload); return e.code
    except Exception as e: emit({"primitive":"search_network_pipeline","status":"failed","error":str(e)}); return 1

def cmd_prepare(args):
    """Run extraction and emit a compact preview without requiring repo inspection."""
    try:
        if getattr(args,"backend","powerset")=="local":
            return cmd_prepare_local(args)
        out_dir=prepare_output_dir(args.query,args.output_dir)
        payload_json=out_dir/"expand_search_request.json"
        expand_json=out_dir/"expand_search_request.full.json"
        ledger=out_dir/"pipeline.ledger.json"
        cmd=[sys.executable,str(ROOT/"packs/search/primitives/expand_search_request/expand_search_request.py"),"--query",args.query,"--env-file",args.env_file,"--timeout",str(args.timeout)]
        if args.model: cmd += ["--model",args.model]
        expand=require_ok(run(cmd, env_file=args.env_file, timeout=args.timeout+30),"expand_search_request")
        payload=payload_from_expand_output(expand)
        if getattr(args,"preserve_query_semantic",False):
            payload=pin_payload_semantic_query(payload,args.query)
        pinned_bands=pinned_bands_from_args(args)
        if pinned_bands:
            payload=pin_payload_seniority_bands(payload,pinned_bands)
        if getattr(args,"current_role",False):
            payload=pin_payload_current_role(payload,True)
        write_json(expand_json,expand); write_json(payload_json,payload)
        company_args=company_directory_tool_args(payload)
        if company_args:
            emit({
                "primitive":"search_network_pipeline",
                "status":"company_directory_fast_path",
                "message":"Company-only people lookup: call MCP list_company_people and skip search-network retrieval.",
                "query":args.query,
                "payload_json":str(payload_json),
                "tool":"list_company_people",
                "tool_args":company_args,
            })
            return 0
        issues=payload_quality_issues(payload)
        extra=f"--query {shlex.quote(args.query)} --payload-json {shlex.quote(str(payload_json))} --execute-approved"
        if getattr(args,"limit",0): extra += f" --limit {int(args.limit)}"
        if getattr(args,"filter_only",False): extra += " --filter-only"
        if pinned_bands: extra += f" --seniority-bands {shlex.quote(','.join(pinned_bands))}"
        if getattr(args,"current_role",False): extra += " --current-role"
        emit({
            "primitive":"search_network_pipeline",
            "status":"preview_ready" if not issues else "blocked_user_action",
            "message":"Show preview and ask: Execute this search or modify it?" if not issues else "Regenerate or modify extraction before retrieval.",
            "query":args.query,
            "payload_json":str(payload_json),
            "expand_json":str(expand_json),
            "ledger":str(ledger),
            "quality_issues":issues,
            "preview":compact_preview(payload,payload_json,issues),
            "execute_command":uv_python_command(args,"run",ledger,extra),
        })
        return 0
    except Exception as e:
        emit({"primitive":"search_network_pipeline","status":"failed","error":str(e)}); return 1

def cmd_prepare_local(args):
    try:
        db_path=local_db_path(args)
        if not db_path.exists():
            raise Failed(f"local DuckDB does not exist: {db_path}")
        configure_local_backend_mode(db_path)
        out_dir=prepare_output_dir(args.query,args.output_dir)
        payload_json=out_dir/"expand_search_request.json"
        expand_json=out_dir/"expand_search_request.full.json"
        ledger=out_dir/"pipeline.ledger.json"
        cmd=[sys.executable,str(ROOT/"packs/search/primitives/expand_search_request/expand_search_request.py"),"--query",args.query,"--env-file",args.env_file,"--timeout",str(args.timeout)]
        if args.model: cmd += ["--model",args.model]
        expand=require_ok(run(cmd, env_file=args.env_file, timeout=args.timeout+30),"expand_search_request")
        payload=normalize_query_expansion_payload(payload_from_expand_output(expand, backend="local"), query=args.query)
        if getattr(args,"preserve_query_semantic",False):
            payload=pin_payload_semantic_query(payload,args.query)
        pinned_bands=pinned_bands_from_args(args)
        if pinned_bands:
            payload=pin_payload_seniority_bands(payload,pinned_bands)
        if getattr(args,"current_role",False):
            payload=pin_payload_current_role(payload,True)
        payload, removed_scope_keys=prepare_local_payload(payload)
        payload=apply_local_title_clustering(payload, db_path)
        write_json(expand_json,expand); write_json(payload_json,payload)
        extra=(
            f"--backend local --db {shlex.quote(str(db_path))} "
            f"--query {shlex.quote(args.query)} --payload-json {shlex.quote(str(payload_json))} --execute-approved"
        )
        if getattr(args,"limit",0): extra += f" --limit {int(args.limit)}"
        if getattr(args,"filter_only",False): extra += " --filter-only"
        if pinned_bands: extra += f" --seniority-bands {shlex.quote(','.join(pinned_bands))}"
        if getattr(args,"current_role",False): extra += " --current-role"
        emit({
            "primitive":"search_network_pipeline",
            "status":"preview_ready",
            "mode":"local_duckdb",
            "query":args.query,
            "duckdb":str(db_path),
            "payload_json":str(payload_json),
            "expand_json":str(expand_json),
            "ledger":str(ledger),
            "ignored_remote_scope_keys":removed_scope_keys,
            "preview":compact_preview_local(payload,payload_json,db_path,removed_scope_keys),
            "execute_command":uv_python_command(args,"run",ledger,extra),
        })
        return 0
    except Exception as e:
        emit({"primitive":"search_network_pipeline","status":"failed","error":str(e)}); return 1

def cmd_status(args):
    lp=ledger_path_for(Path(args.state) if args.state else None, Path(args.ledger) if args.ledger else None); l=load_ledger(lp)
    emit({"primitive":"search_network_pipeline","status":"ok","ledger":str(lp),"state":l.get("state"),"mode":l.get("mode"),"current_block":l.get("current_block"),"artifacts":l.get("artifacts",{}),"summary":pipeline_summary(l),"step_counts":{s:sum(1 for r in l.get('steps',{}).values() if r.get('status')==s) for s in sorted({r.get('status') for r in l.get('steps',{}).values()})}}); return 0

def cmd_approve(args):
    if not args.confirm: emit({"status":"blocked","error":"pass --confirm"}); return 2
    lp=ledger_path_for(Path(args.state) if args.state else None, Path(args.ledger) if args.ledger else None); l=load_ledger(lp); cur=l.get("current_block") or {}; aid=args.approval_id or cur.get("approval_id")
    if not aid: emit({"status":"failed","error":"no approval_id"}); return 1
    l.setdefault("approvals",{})[aid]={"confirmed":True,"type":args.kind,"approved_at":now(),"payload":cur.get("payload",{})}; l["current_block"]=None; save(lp,l); emit({"primitive":"search_network_pipeline","status":"ok","approval_id":aid}); return 0

def add_backend(p):
    p.add_argument("--backend",choices=("powerset","local"),default="powerset",help="powerset = TurboPuffer + Postgres remote retrieval (default); local = the local DuckDB index (scope is the DB file; no set/operator resolution)")
    p.add_argument("--db",default=DEFAULT_LOCAL_DB,help="Local DuckDB path (used only with --backend local)")

def add_run(p):
    add_backend(p); p.add_argument("--ledger"); p.add_argument("--state"); p.add_argument("--query"); p.add_argument("--payload-json"); p.add_argument("--env-file",default=".env"); p.add_argument("--seniority-bands",help="Comma-separated canonical seniority bands (e.g. senior,staff) pinned as a hard retrieval filter; REPLACES any expansion-derived role_search_filters.seniority_bands"); p.add_argument("--current-role",action="store_true",help="Pin is_current_role=true as a hard retrieval filter so only CURRENT in-band positions qualify a person (a current founder who was once a senior engineer no longer matches on the old role)"); p.add_argument("--limit",type=int,default=0,help="Max unique people to keep locally after retrieval; 0 means keep full retrieved frontier"); p.add_argument("--top-k",type=int,default=None,help="Retrieval top_k; defaults to 10000 (powerset) or 1000 (local)"); p.add_argument("--extra-candidates-json",help="JSON file with agentic SQL vertical people (search-sql skill output); unioned into retrieval so they go through the same hydration and LLM filter/rerank as every other candidate (local backend only)"); p.add_argument("--search-only",action="store_true",help="Skip LLM filter/rerank after retrieval + hydration"); p.add_argument("--filter-only",action="store_true",help="Run the cheap conservative LLM filter but skip LLM rerank; final ranking is owned by a downstream evaluator"); p.add_argument("--execute-approved",action="store_true",help="User already approved the search preview; run retrieval, hydration, LLM filter/rerank, and persistence without a second gate"); p.add_argument("--confirm-llm",action="store_true",help="Backward-compatible alias for approving the LLM filter/rerank stage"); p.add_argument("--model",default=DEFAULT_MODEL); p.add_argument("--reasoning-effort",default=DEFAULT_REASONING_EFFORT,help="LLM rerank reasoning effort; default is low"); p.add_argument("--filter-batch-size",type=int,default=DEFAULT_FILTER_BATCH_SIZE,help="LLM filter candidates per request; default is 2"); p.add_argument("--filter-concurrency",type=int,default=DEFAULT_FILTER_CONCURRENCY,help="LLM filter batch fanout; mirrors SEARCH_V2_LLM_FILTER_MAX_CONCURRENT"); p.add_argument("--rerank-concurrency",type=int,default=DEFAULT_RERANK_CONCURRENCY,help="LLM rerank fanout; mirrors SEARCH_V2_RERANK_MAX_CONCURRENT"); p.add_argument("--timeout",type=int,default=600); p.add_argument("--llm-timeout",type=int,default=3600); p.add_argument("--force",action="store_true")

def build_parser() -> argparse.ArgumentParser:
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("prepare"); add_backend(p); p.add_argument("--query",required=True); p.add_argument("--env-file",default=".env"); p.add_argument("--output-dir"); p.add_argument("--model"); p.add_argument("--timeout",type=int,default=60); p.add_argument("--limit",type=int,default=0,help="Cap unique people kept after retrieval; threaded into the emitted execute_command"); p.add_argument("--filter-only",action="store_true",help="Emit an execute_command that runs the cheap LLM filter but skips per-run LLM rerank (for multi-profile fan-out)"); p.add_argument("--seniority-bands",help="Comma-separated canonical seniority bands pinned as a hard retrieval filter; applied to the prepared payload and threaded into the emitted execute_command"); p.add_argument("--current-role",action="store_true",help="Pin is_current_role=true on the prepared payload and thread --current-role into the emitted execute_command so only CURRENT in-band positions qualify a person"); p.add_argument("--preserve-query-semantic",action="store_true",help="Use the raw --query verbatim as role_search_filters.semantic_query instead of the LLM-rewritten prose; keeps expansion's bm25 + structured filters. Higher recall (the vector stays specific per probe) — recommended for recall/ground-truth sourcing and wide-search probes."); p.set_defaults(func=cmd_prepare)
    r=sub.add_parser("run"); add_run(r); r.set_defaults(func=cmd_run)
    c=sub.add_parser("continue"); add_run(c); c.set_defaults(func=cmd_run)
    s=sub.add_parser("status"); s.add_argument("--ledger"); s.add_argument("--state"); s.set_defaults(func=cmd_status)
    a=sub.add_parser("approve"); a.add_argument("kind",choices=["llm"]); a.add_argument("--ledger"); a.add_argument("--state"); a.add_argument("--approval-id"); a.add_argument("--confirm",action="store_true"); a.set_defaults(func=cmd_approve)
    return ap

def main():
    ap=build_parser()
    args=ap.parse_args(); raise SystemExit(args.func(args))
if __name__ == "__main__": main()
