#!/usr/bin/env python3
"""Resumable Sales Navigator run: search plan → leads on disk → enrich → mutual URLs → export → score.

The pipeline makes its own calls. With a set (``--set-id`` or ``POWERPACKS_DEFAULT_SET_ID``,
the chat's Search-in scope) it calls the Powerset API, which searches every Sales Nav seat in
the set and merges. Without a set it searches through the user's own LinkedIn session on this
Mac (packs/ingestion/primitives/discover/linkedin/linkedin_session.py): one seat, no profile
enrichment, no mutual URL lookup, 100 requests a day.

A run is a search plan of one or more queries (``--search-plan-json``). Running again with a
new plan file appends the queries whose ids are new, so an agent can look at the per-query
summaries in the output, decide the results are wrong or thin, and add three more. Every
query's leads land in the run's ``leads.jsonl`` deduped by member id; the output's
``searches`` list says, per query, what it asked for, how many it found and the top ten.

The only stop is the paid LLM scoring approval (``blocked_approval``).
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
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.ingestion.primitives.discover.linkedin import linkedin_session as li  # noqa: E402
from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import api_base, bearer_token  # noqa: E402

DEFAULT_BASE = Path(".powerpacks/sales-nav/runs")
PIPELINE_REL = Path("packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py")
ARTIFACTS_REL = Path("packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py")
SCORER_REL = Path("packs/sales-nav/primitives/score_sales_nav_leads/score_sales_nav_leads.py")
ATTRIBUTION_REL = Path("packs/sales-nav/primitives/enrich_mutual_attribution/enrich_mutual_attribution.py")
DEFAULT_COUNT = 25
DEFAULT_ENRICH_LIMIT = int(os.environ.get("POWERPACKS_SALES_NAV_ENRICH_LIMIT", "100"))
DEFAULT_MUTUAL_URL_LIMIT = int(os.environ.get("POWERPACKS_SALES_NAV_MUTUAL_URL_LIMIT", "100"))
CLOUD_WORKERS = 4
SUMMARY_LEADS = 10

# The search arguments the Powerset API's /v2/sales-nav/search takes; the local session
# takes the same names and reports the ones it cannot honour.
SEARCH_ARGS = {
    "company_ids", "company_names", "past_company_ids", "past_company_names",
    "seniority_ids", "function_ids", "geography_ids", "geography_names", "headcount_ids",
    "industry_id", "title", "title_ids", "keywords", "count", "start_offset",
}
PLAN_METADATA_KEYS = {
    "id", "label", "name", "description", "reason", "notes", "args", "search_args", "tool_args",
    "queries", "searches", "steps", "criteria", "score_criteria",
}
LOCAL_UNSUPPORTED = ("past_company_ids", "headcount_ids", "industry_id")


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def parse_jsons(text: str) -> list[Any]:
    out: list[Any] = []
    decoder = json.JSONDecoder()
    i = 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        try:
            obj, end = decoder.raw_decode(text, i)
            out.append(obj)
            i = end
        except json.JSONDecodeError:
            j = text.find("{", i + 1)
            if j < 0:
                break
            i = j
    return out


def run(cmd: list[str], timeout: int = 300) -> dict[str, Any]:
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    json_objects = parse_jsons(proc.stdout or "")
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "json": json_objects[-1] if json_objects else None,
        "json_objects": json_objects,
    }


def require(result: dict[str, Any], step: str) -> dict[str, Any]:
    if result["returncode"] != 0:
        detail = ((result.get("stderr") or result.get("stdout") or "").strip())[-1500:]
        raise RuntimeError(f"{step} failed: {detail}")
    payload = result.get("json") or {}
    if not isinstance(payload, dict):
        return {}
    return payload


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:60] or "sales-nav"


def approval_id(kind: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return f"{kind}_{digest}"


# ── Ledger ─────────────────────────────────────────────────────────────────────

def ledger_path(args: argparse.Namespace) -> Path:
    if args.ledger:
        return Path(args.ledger)
    if args.state:
        return Path(str(args.state) + ".pipeline.json")
    run_id = getattr(args, "run_id", None) or f"sales-nav-{slug(getattr(args, 'query', None) or 'run')}-{uuid.uuid4().hex[:8]}"
    return DEFAULT_BASE / run_id / "pipeline.json"


def load(path: Path) -> dict[str, Any]:
    ledger = read_json(path, {}) or {}
    ledger.setdefault("primitive", "sales_nav_pipeline")
    ledger.setdefault("created_at", now())
    ledger.setdefault("steps", {})
    ledger.setdefault("approvals", {})
    ledger.setdefault("artifacts", {})
    return ledger


def save(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = now()
    write_json(path, ledger)


def done(ledger: dict[str, Any], step: str) -> bool:
    return ledger.get("steps", {}).get(step, {}).get("status") == "completed"


def step_status(ledger: dict[str, Any], step: str) -> str | None:
    status = ledger.get("steps", {}).get(step, {}).get("status")
    return str(status) if status else None


def mark(path: Path, ledger: dict[str, Any], step: str, status: str, **kwargs: Any) -> None:
    rec = ledger.setdefault("steps", {}).setdefault(step, {"id": step})
    rec.update(status=status, **kwargs)
    rec["updated_at"] = now()
    save(path, ledger)


def approved(ledger: dict[str, Any], aid: str) -> bool:
    return bool(ledger.get("approvals", {}).get(aid, {}).get("confirmed"))


def block_approval(path: Path, ledger: dict[str, Any], kind: str, payload: dict[str, Any], message: str, next_cmd: str) -> int:
    aid = approval_id(kind, payload)
    block = {
        "primitive": "sales_nav_pipeline",
        "status": "blocked_approval",
        "approval_type": kind,
        "approval_id": aid,
        "message": message,
        "payload": payload,
        "continue_command": next_cmd,
        "ledger": str(path),
    }
    ledger["current_block"] = block
    mark(path, ledger, kind, "blocked_approval", summary=block)
    emit(block)
    return 20


def set_id_of(ledger: dict[str, Any]) -> str:
    return str((ledger.get("artifacts") or {}).get("set_id") or "")


def backend_of(ledger: dict[str, Any]) -> str:
    """'cloud' with a set (the Powerset API, every seat in the set), 'local' without (this Mac's session)."""
    return "cloud" if set_id_of(ledger) else "local"


# ── Cloud: the Powerset API ─────────────────────────────────────────────────────

def cloud_call(route: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{api_base()}/v2/sales-nav{route}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {bearer_token()}", "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode(errors="replace")
        raise RuntimeError(f"Powerset API {route} answered HTTP {exc.code}: {detail}") from exc


def cloud_searches(plan_args: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=CLOUD_WORKERS) as pool:
        return list(pool.map(lambda args: cloud_call("/search", args), plan_args))


# ── Local: the user's own LinkedIn session ──────────────────────────────────────

def local_namespace(args: dict[str, Any], title_ids: list[dict[str, Any]]) -> SimpleNamespace:
    company_ids = [int(c) for c in args.get("company_ids") or []]
    names = {str(k): v for k, v in (args.get("company_names") or {}).items()}
    return SimpleNamespace(
        degree="2nd",
        company_id=company_ids or None,
        company_name=[names.get(str(c), "") for c in company_ids] or None,
        title_id=[int(t["id"]) for t in title_ids] or None,
        title_name=[str(t.get("text") or "") for t in title_ids] or None,
        region_id=list(args.get("geography_ids") or []) or None,
        region_name=list(args.get("geography_names") or []) or None,
        seniority=[str(s) for s in args.get("seniority_ids") or []] or None,
        function=[str(f) for f in args.get("function_ids") or []] or None,
        keywords=args.get("keywords") or None,
        start=int(args.get("start_offset") or 0),
        count=int(args.get("count") or DEFAULT_COUNT),
    )


def lead_row(lead: dict[str, Any]) -> dict[str, Any]:
    """A local SalesNavLead dict in the shape the API's search returns (its _ingest_lead)."""
    mutual_ids = list(lead.get("mutual_member_ids") or [])
    return {
        "name": f"{lead.get('first_name') or ''} {lead.get('last_name') or ''}".strip(),
        "title": lead.get("title") or "",
        "company": lead.get("company_name") or "",
        "location": lead.get("location") or "",
        "member_id": lead.get("member_id"),
        "profile_id": lead.get("profile_id"),
        "linkedin_url": None,
        "profile_picture_url": lead.get("profile_picture_url"),
        "source_account_ids": [],
        "degree": lead.get("degree"),
        "mutual_count": lead.get("total_mutual_count") or len(mutual_ids),
        "total_mutual_count": lead.get("total_mutual_count"),
        "mutual_member_ids": mutual_ids,
        "mutuals": [{"member_id": mid, "first_name": ""} for mid in mutual_ids],
        "intro_text": lead.get("mutual_intro_text"),
    }


def local_title_ids(title: str) -> list[dict[str, Any]]:
    matches = li.sales_nav("titles", SimpleNamespace(query=title))["matches"]
    return [{"id": matches[0]["id"], "text": matches[0]["name"]}] if matches else []


def local_searches(plan_args: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """All the plan's searches in one browser run, answered in the API's response shape."""
    namespaces = []
    for args in plan_args:
        title_ids = list(args.get("title_ids") or [])
        if args.get("title") and not title_ids:
            title_ids = local_title_ids(args["title"])
        namespaces.append(local_namespace(args, title_ids))
    answers = li.sales_nav_searches(namespaces)
    results = []
    for args, namespace, answer in zip(plan_args, namespaces, answers):
        leads = [lead_row(lead) for lead in answer["leads"]]
        end = namespace.start + len(leads)
        total = int(answer["total"])
        results.append({
            "backend": "local",
            "leads": leads,
            "total_count": total,
            "max_total_count": total,
            "results_returned": len(leads),
            "accumulated_count": len(leads),
            "has_more": end < total,
            "next_start_offset": end if end < total else None,
            "filters_used": {key: args[key] for key in SEARCH_ARGS if args.get(key) not in (None, "", [], {})},
            "unsupported_filters": [key for key in LOCAL_UNSUPPORTED if args.get(key)],
            "requests_today": answer["requests_today"],
            "day_cap": answer["day_cap"],
        })
    return results


def local_resolve(companies: list[str], titles: list[str]) -> dict[str, Any]:
    resolved_companies = {}
    for query in companies:
        matches = li.sales_nav("companies", SimpleNamespace(query=query))["matches"]
        resolved_companies[query] = [
            {"org_id": m["id"], "name": m["name"], "headcount": m.get("headcount"), "location": m.get("location"),
             "recommended": index == 0}
            for index, m in enumerate(matches)
        ]
    resolved_titles = {}
    for query in titles:
        matches = li.sales_nav("titles", SimpleNamespace(query=query))["matches"]
        resolved_titles[query] = [{"title_id": m["id"], "name": m["name"], "recommended": index == 0} for index, m in enumerate(matches)]
    return {"backend": "local", "resolved_companies": resolved_companies, "resolved_titles": resolved_titles}


# ── Run folder ──────────────────────────────────────────────────────────────────

def ensure_init(args: argparse.Namespace, path: Path, ledger: dict[str, Any]) -> Path:
    if args.state:
        state = Path(args.state)
        ledger.setdefault("artifacts", {})["state"] = str(state)
        if state.exists():
            state_payload = read_json(state, {}) or {}
            if state_payload.get("conversation_id"):
                ledger["artifacts"]["conversation_id"] = state_payload["conversation_id"]
            if state_payload.get("set_id"):
                ledger["artifacts"]["set_id"] = state_payload["set_id"]
        save(path, ledger)
        return state
    if done(ledger, "init_local_artifacts"):
        return Path(ledger["artifacts"]["state"])
    if not args.query:
        raise RuntimeError("Need --query for a new run, or --state for an existing one")
    set_id = args.set_id or os.environ.get("POWERPACKS_DEFAULT_SET_ID", "").strip()
    conversation_id = args.conversation_id or str(uuid.uuid4())
    out_dir = path.parent
    cmd = [
        sys.executable, str(ROOT / ARTIFACTS_REL), "init",
        "--query", args.query,
        "--set-id", set_id,
        "--conversation-id", conversation_id,
        "--out-dir", str(out_dir),
    ]
    payload = require(run(cmd), "sales_nav_artifacts init")
    state = Path(payload["state"])
    ledger.setdefault("artifacts", {}).update({
        "state": str(state),
        "run_dir": str(state.parent),
        "conversation_id": conversation_id,
        "set_id": set_id,
    })
    mark(path, ledger, "init_local_artifacts", "completed", summary=payload, command=" ".join(map(shlex.quote, cmd)))
    return state


def _candidate_query_items(raw: Any) -> tuple[list[Any], str | None]:
    if isinstance(raw, list):
        return raw, None
    if isinstance(raw, dict):
        criteria = raw.get("score_criteria") or raw.get("criteria")
        for key in ("queries", "searches", "steps"):
            value = raw.get(key)
            if isinstance(value, list):
                return value, str(criteria) if criteria else None
        return [raw], str(criteria) if criteria else None
    return [], None


def normalize_search_plan(raw: Any, *, set_id: str | None, conversation_id: str | None, default_count: int) -> tuple[list[dict[str, Any]], str | None]:
    items, criteria = _candidate_query_items(raw)
    plan: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        nested_args = item.get("tool_args") or item.get("search_args") or item.get("args")
        if isinstance(nested_args, dict):
            source_args = dict(nested_args)
        else:
            source_args = {key: value for key, value in item.items() if key not in PLAN_METADATA_KEYS}

        raw_args = dict(source_args)
        tool_args = {key: value for key, value in raw_args.items() if key in SEARCH_ARGS and value not in (None, "", [], {})}
        if set_id:
            tool_args["set_id"] = set_id
        if conversation_id:
            tool_args["conversation_id"] = conversation_id
        tool_args["count"] = int(tool_args.get("count") or default_count)
        tool_args.setdefault("start_offset", 0)

        label = item.get("label") or item.get("name") or item.get("description") or f"search {index + 1}"
        plan.append({
            "id": str(item.get("id") or f"search_{index + 1}"),
            "label": str(label),
            "args": tool_args,
            "raw_args": raw_args,
        })
    return plan, criteria


def search_plan(args: argparse.Namespace, ledger_path_: Path, ledger: dict[str, Any], state: Path) -> list[dict[str, Any]]:
    """The run's queries: what the ledger has, plus any new ids from this call's plan file."""
    artifacts = ledger.setdefault("artifacts", {})
    existing = list(artifacts.get("search_plan") or [])

    raw: Any = None
    source = None
    if args.search_plan_json:
        raw = read_json(Path(args.search_plan_json), None)
        source = str(args.search_plan_json)
    elif args.search_args_json:
        raw = read_json(Path(args.search_args_json), None)
        source = str(args.search_args_json)

    if raw is None:
        if existing or (state.exists() and read_json(state, {}).get("counts", {}).get("leads", 0)):
            return existing
        raise RuntimeError("Need --search-args-json or --search-plan-json for a new Sales Nav search")

    state_payload = read_json(state, {}) or {}
    set_id = args.set_id or artifacts.get("set_id") or state_payload.get("set_id")
    conversation_id = artifacts.get("conversation_id") or state_payload.get("conversation_id") or args.conversation_id
    plan, criteria = normalize_search_plan(raw, set_id=set_id, conversation_id=conversation_id, default_count=args.count)
    known = {item["id"] for item in existing}
    merged = existing + [item for item in plan if item["id"] not in known]
    if not merged:
        raise RuntimeError("Search plan is empty")
    artifacts["search_plan"] = merged
    artifacts.setdefault("search_plan_sources", [])
    if source and source not in artifacts["search_plan_sources"]:
        artifacts["search_plan_sources"].append(source)
    if criteria and not artifacts.get("score_criteria"):
        artifacts["score_criteria"] = criteria
    save(ledger_path_, ledger)
    return merged


def state_paths(state_path: Path) -> dict[str, Path]:
    state = read_json(state_path, {}) or {}
    files = state.get("files") or {}
    return {key: Path(value) for key, value in files.items() if isinstance(value, str)}


def load_leads(state_path: Path) -> list[dict[str, Any]]:
    paths = state_paths(state_path)
    leads_path = paths.get("leads_jsonl")
    return read_jsonl(leads_path) if leads_path else []


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def member_ids_for_enrichment(state_path: Path, *, limit: int) -> list[int]:
    rows = load_leads(state_path)
    candidates = [row for row in rows if row.get("member_id") and not truthy(row.get("enriched"))]
    candidates.sort(key=lambda row: int(row.get("mutual_count") or 0), reverse=True)
    out: list[int] = []
    seen: set[int] = set()
    for row in candidates:
        try:
            mid = int(row.get("member_id"))
        except (TypeError, ValueError):
            continue
        if mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
        if limit and len(out) >= limit:
            break
    return out


def pending_mutual_ids(state_path: Path, *, limit: int, include_unresolved: bool = False) -> dict[str, Any]:
    cmd = [sys.executable, str(ROOT / ARTIFACTS_REL), "pending-mutual-ids", "--state", str(state_path), "--limit", str(limit)]
    if include_unresolved:
        cmd.append("--include-unresolved")
    return require(run(cmd), "pending-mutual-ids")


def ingest_page(state: Path, response: Path) -> dict[str, Any]:
    cmd = [sys.executable, str(ROOT / ARTIFACTS_REL), "ingest-page", "--state", str(state), "--response", str(response)]
    return require(run(cmd), "ingest-page")


def ingest_member_urls(state: Path, response: Path) -> dict[str, Any]:
    cmd = [sys.executable, str(ROOT / ARTIFACTS_REL), "ingest-member-urls", "--state", str(state), "--response", str(response)]
    return require(run(cmd), "ingest-member-urls")


# ── Searches ────────────────────────────────────────────────────────────────────

def filters_description(args: dict[str, Any]) -> str:
    """What a query asked for, in words, the way the cloud agent reads it back."""
    parts = []
    names = {str(k): v for k, v in (args.get("company_names") or {}).items()}
    if args.get("company_ids"):
        parts.append("company=" + ",".join(names.get(str(c), str(c)) for c in args["company_ids"]))
    past = {str(k): v for k, v in (args.get("past_company_names") or {}).items()}
    if args.get("past_company_ids"):
        parts.append("past_company=" + ",".join(past.get(str(c), str(c)) for c in args["past_company_ids"]))
    if args.get("title"):
        parts.append(f"title={args['title']}")
    if args.get("title_ids"):
        parts.append("title_ids=" + ",".join(str(t.get("text") or t.get("id")) for t in args["title_ids"]))
    if args.get("seniority_ids"):
        parts.append("seniority=" + ",".join(li.SENIORITY_LEVELS.get(str(s), str(s)) for s in args["seniority_ids"]))
    if args.get("function_ids"):
        parts.append("functions=" + ",".join(li.FUNCTIONS.get(str(f), str(f)) for f in args["function_ids"]))
    if args.get("geography_ids"):
        geo_names = list(args.get("geography_names") or [])
        parts.append("geo=" + ",".join(geo_names[i] if i < len(geo_names) and geo_names[i] else str(g) for i, g in enumerate(args["geography_ids"])))
    if args.get("headcount_ids"):
        parts.append("headcount=" + ",".join(map(str, args["headcount_ids"])))
    if args.get("keywords"):
        parts.append(f"keywords={args['keywords']}")
    if int(args.get("start_offset") or 0):
        parts.append(f"start_offset={args['start_offset']}")
    return "; ".join(parts) if parts else "no specific filters"


def lead_summaries(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": lead.get("name"), "title": lead.get("title"), "company": lead.get("company"),
            "mutual_count": lead.get("mutual_count"), "location": lead.get("location") or "",
            "linkedin_url": lead.get("linkedin_url") or "",
        }
        for lead in leads[:SUMMARY_LEADS]
    ]


def search_summary(item: dict[str, Any], payload: dict[str, Any], ingested: dict[str, Any]) -> dict[str, Any]:
    leads = payload.get("leads") or []
    return {
        "id": item["id"],
        "label": item["label"],
        "filters_description": filters_description(item["args"]),
        "total_count": payload.get("total_count"),
        "max_total_count": payload.get("max_total_count"),
        "results_returned": len(leads),
        "new_leads": ingested.get("leads_ingested"),
        "lead_count_after": ingested.get("lead_count"),
        "has_more": payload.get("has_more"),
        "next_start_offset": payload.get("next_start_offset"),
        "lead_summaries": lead_summaries(leads),
        "refinement_hint": payload.get("refinement_hint"),
        "unsupported_filters": payload.get("unsupported_filters") or [],
        "error": payload.get("error"),
        "requests_today": payload.get("requests_today"),
    }


def run_searches(args: argparse.Namespace, ledger_path_: Path, ledger: dict[str, Any], state: Path) -> None:
    plan = search_plan(args, ledger_path_, ledger, state)
    pages = state.parent / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    pending = [(index, item) for index, item in enumerate(plan) if not done(ledger, f"search_{index:03d}")]
    if not pending:
        return
    backend = backend_of(ledger)
    plan_args = [dict(item["args"]) for _, item in pending]
    payloads = cloud_searches(plan_args) if backend == "cloud" else local_searches(plan_args)
    for (index, item), payload in zip(pending, payloads):
        response_path = pages / f"sales-nav-search-{index:03d}.response.json"
        write_json(response_path, payload)
        if payload.get("error") and not payload.get("leads"):
            raise RuntimeError(f"search {item['label']!r}: {payload['error']}")
        ingested = ingest_page(state, response_path)
        mark(ledger_path_, ledger, f"search_{index:03d}", "completed",
             summary=search_summary(item, payload, ingested), response=str(response_path), backend=backend)


def search_summaries(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    plan = (ledger.get("artifacts") or {}).get("search_plan") or []
    return [ledger["steps"][f"search_{index:03d}"]["summary"] for index in range(len(plan)) if done(ledger, f"search_{index:03d}")]


# ── Enrich, mutual URLs, attribution ───────────────────────────────────────────

def enrich_profiles(args: argparse.Namespace, ledger_path_: Path, ledger: dict[str, Any], state: Path) -> None:
    if args.skip_enrich or done(ledger, "enrich_profiles"):
        return
    if backend_of(ledger) == "local":
        mark(ledger_path_, ledger, "enrich_profiles", "completed", summary={"reason": "local session: no profile enrichment"})
        return
    member_ids = member_ids_for_enrichment(state, limit=args.enrich_limit)
    if not member_ids:
        mark(ledger_path_, ledger, "enrich_profiles", "completed", summary={"reason": "no_unenriched_member_ids"})
        return
    artifacts = ledger.get("artifacts") or {}
    payload = cloud_call("/enrich", {"conversation_id": artifacts["conversation_id"], "member_ids": member_ids, "set_id": artifacts["set_id"]})
    response_path = state.parent / "pages" / "enrich-profiles.response.json"
    write_json(response_path, payload)
    ingested = ingest_page(state, response_path) if payload.get("leads") else {}
    summary = {key: payload.get(key) for key in ("enriched", "failed", "unresolvable", "total_requested", "updated_leads", "error") if key in payload}
    summary["leads_ingested"] = ingested.get("leads_ingested")
    mark(ledger_path_, ledger, "enrich_profiles", "completed", summary=summary, response=str(response_path))


def resolve_mutual_urls(args: argparse.Namespace, ledger_path_: Path, ledger: dict[str, Any], state: Path) -> None:
    if args.skip_mutual_url_resolution:
        mark(ledger_path_, ledger, "resolve_mutual_urls", "completed", summary={"reason": "skipped_by_flag"})
        return
    if backend_of(ledger) == "local":
        mark(ledger_path_, ledger, "resolve_mutual_urls", "completed", summary={"reason": "local session: no member id lookup"})
        return
    pending = pending_mutual_ids(state, limit=args.mutual_url_limit, include_unresolved=bool(args.resolve_mutuals_external))
    member_ids = pending.get("member_ids") or []
    if not member_ids:
        if step_status(ledger, "resolve_mutual_urls") != "completed":
            mark(ledger_path_, ledger, "resolve_mutual_urls", "completed", summary={"pending": 0})
        return
    batch_index = int((ledger.get("artifacts") or {}).get("mutual_url_batches") or 0)
    payload = cloud_call("/member-urls", {"member_ids": member_ids, "use_external_apis": bool(args.resolve_mutuals_external)})
    response_path = state.parent / "pages" / f"member-urls-{batch_index:03d}.response.json"
    write_json(response_path, payload)
    ingested = ingest_member_urls(state, response_path)
    ledger.setdefault("artifacts", {})["mutual_url_batches"] = batch_index + 1
    mark(ledger_path_, ledger, f"resolve_mutual_urls_{batch_index:03d}", "completed",
         summary={"requested": len(member_ids), "resolved": len(payload.get("resolved") or {}), "unresolved": len(payload.get("unresolved") or []), **ingested},
         response=str(response_path))
    mark(ledger_path_, ledger, "resolve_mutual_urls", "completed", summary={"batches": batch_index + 1})


def enrich_mutual_attribution_step(args: argparse.Namespace, ledger_path_: Path, ledger: dict[str, Any], state: Path) -> None:
    """Enrich mutuals with operator attribution. Runs discover if --discover-mutuals."""
    if done(ledger, "enrich_mutual_attribution") and not args.force:
        return
    if backend_of(ledger) == "local":
        mark(ledger_path_, ledger, "enrich_mutual_attribution", "completed", summary={"reason": "local session: no set to attribute against"})
        return
    mode = "discover" if getattr(args, "discover_mutuals", False) else "attribution"
    cmd = [
        sys.executable, str(ROOT / ATTRIBUTION_REL),
        "--state", str(state),
        "--mode", mode,
        "--env-file", str(ROOT / ".env"),
    ]
    if getattr(args, "discover_stagger", None):
        cmd.extend(["--stagger", str(args.discover_stagger)])
    if getattr(args, "discover_max_leads", None):
        cmd.extend(["--max-leads", str(args.discover_max_leads)])
    timeout = 600 if mode == "discover" else 60
    result = run(cmd, timeout=timeout)
    if result.get("returncode") != 0:
        mark(ledger_path_, ledger, "enrich_mutual_attribution", "completed",
             summary={"reason": "failed", "error": (result.get("stderr") or "")[-200:]})
        return
    payload = result.get("json") or {}
    mark(ledger_path_, ledger, "enrich_mutual_attribution", "completed",
         summary=payload, command=" ".join(map(shlex.quote, cmd)))


def export_state(args: argparse.Namespace, ledger_path_: Path, ledger: dict[str, Any], state: Path) -> None:
    cmd = [sys.executable, str(ROOT / ARTIFACTS_REL), "export", "--state", str(state)]
    payload = require(run(cmd), "export")
    ledger.setdefault("artifacts", {}).update({
        "leads_csv": payload.get("leads_csv"),
        "mutuals_csv": payload.get("mutuals_csv"),
    })
    mark(ledger_path_, ledger, "export", "completed", summary=payload, command=" ".join(map(shlex.quote, cmd)))


def score_if_requested(args: argparse.Namespace, ledger_path_: Path, ledger: dict[str, Any], state: Path) -> int | None:
    criteria = args.criteria or (ledger.get("artifacts") or {}).get("score_criteria")
    if not criteria:
        return None
    payload = {"criteria": criteria, "threshold": args.threshold, "state": str(state)}
    aid = approval_id("llm", payload)
    if not approved(ledger, aid) and not args.confirm_llm:
        next_cmd = (
            f"{sys.executable} {PIPELINE_REL} approve llm --ledger {shlex.quote(str(ledger_path_))} "
            f"--approval-id {aid} --confirm && "
            f"{sys.executable} {PIPELINE_REL} continue --ledger {shlex.quote(str(ledger_path_))} --criteria {shlex.quote(criteria)}"
        )
        return block_approval(ledger_path_, ledger, "llm", payload, f"Score Sales Nav leads with LLM against criteria '{criteria}'?", next_cmd)
    if done(ledger, "score_leads") and not args.force:
        return None
    cmd = [
        sys.executable, str(ROOT / SCORER_REL),
        "--state", str(state),
        "--criteria", criteria,
        "--threshold", str(args.threshold),
    ]
    result = require(run(cmd, timeout=args.timeout), "score_leads")
    ledger.setdefault("artifacts", {})["scores"] = result.get("output_dir") or result.get("matches_csv")
    mark(ledger_path_, ledger, "score_leads", "completed", summary=result, command=" ".join(map(shlex.quote, cmd)))
    return None


# ── Commands ────────────────────────────────────────────────────────────────────

def cmd_run(args: argparse.Namespace) -> int:
    try:
        ledger_path_ = ledger_path(args)
        ledger = load(ledger_path_)
        ledger["current_block"] = None
        save(ledger_path_, ledger)
        state = ensure_init(args, ledger_path_, ledger)

        run_searches(args, ledger_path_, ledger, state)
        enrich_profiles(args, ledger_path_, ledger, state)
        resolve_mutual_urls(args, ledger_path_, ledger, state)
        enrich_mutual_attribution_step(args, ledger_path_, ledger, state)
        export_state(args, ledger_path_, ledger, state)
        blocked = score_if_requested(args, ledger_path_, ledger, state)
        if blocked is not None:
            return blocked

        ledger["current_block"] = None
        save(ledger_path_, ledger)
        state_payload = read_json(state, {}) or {}
        emit({
            "primitive": "sales_nav_pipeline",
            "status": "completed",
            "backend": backend_of(ledger),
            "ledger": str(ledger_path_),
            "state": str(state),
            "counts": state_payload.get("counts"),
            "searches": search_summaries(ledger),
            "artifacts": ledger.get("artifacts", {}),
        })
        return 0
    except Exception as exc:
        emit({"primitive": "sales_nav_pipeline", "status": "failed", "error": str(exc)})
        return 1


def cmd_resolve(args: argparse.Namespace) -> int:
    """Company names and titles to the ids a search plan needs, through whichever backend the set says."""
    try:
        set_id = args.set_id or os.environ.get("POWERPACKS_DEFAULT_SET_ID", "").strip()
        if set_id:
            payload = cloud_call("/resolve", {"set_id": set_id, "companies": args.company or None, "titles": args.title or None})
            payload["backend"] = "cloud"
        else:
            payload = local_resolve(args.company or [], args.title or [])
        emit({"primitive": "sales_nav_pipeline", "status": "ok", **payload})
        return 0
    except Exception as exc:
        emit({"primitive": "sales_nav_pipeline", "status": "failed", "error": str(exc)})
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    path = ledger_path(args)
    ledger = load(path)
    statuses = [rec.get("status") for rec in ledger.get("steps", {}).values() if rec.get("status")]
    emit({
        "primitive": "sales_nav_pipeline",
        "status": "ok",
        "backend": backend_of(ledger),
        "ledger": str(path),
        "current_block": ledger.get("current_block"),
        "searches": search_summaries(ledger),
        "artifacts": ledger.get("artifacts", {}),
        "step_counts": {status: statuses.count(status) for status in sorted(set(statuses))},
    })
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    if not args.confirm:
        emit({"primitive": "sales_nav_pipeline", "status": "blocked", "error": "pass --confirm"})
        return 2
    path = ledger_path(args)
    ledger = load(path)
    current = ledger.get("current_block") or {}
    aid = args.approval_id or current.get("approval_id")
    if not aid:
        emit({"primitive": "sales_nav_pipeline", "status": "failed", "error": "no approval_id"})
        return 1
    ledger.setdefault("approvals", {})[aid] = {
        "confirmed": True,
        "type": args.kind,
        "approved_at": now(),
        "payload": current.get("payload", {}),
    }
    ledger["current_block"] = None
    save(path, ledger)
    emit({"primitive": "sales_nav_pipeline", "status": "ok", "approval_id": aid})
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ledger")
    parser.add_argument("--state")
    parser.add_argument("--query")
    parser.add_argument("--set-id", help="Powerset set; default POWERPACKS_DEFAULT_SET_ID. Without one the search runs through this Mac's LinkedIn session.")
    parser.add_argument("--conversation-id")
    parser.add_argument("--run-id")
    parser.add_argument("--search-args-json")
    parser.add_argument("--search-plan-json", help="queries to run; on an existing run, the ids not seen before are added")
    parser.add_argument("--skip-enrich", action="store_true")
    parser.add_argument("--enrich-limit", type=int, default=DEFAULT_ENRICH_LIMIT)
    parser.add_argument("--skip-mutual-url-resolution", action="store_true")
    parser.add_argument("--mutual-url-limit", type=int, default=DEFAULT_MUTUAL_URL_LIMIT)
    parser.add_argument("--resolve-mutuals-external", action="store_true")
    parser.add_argument("--discover-mutuals", action="store_true", help="Run full mutual discovery (Phase 2) instead of attribution-only")
    parser.add_argument("--discover-stagger", type=float, default=2.0, help="Seconds between discover API batches")
    parser.add_argument("--discover-max-leads", type=int, default=25, help="Max leads for mutual discovery")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--criteria")
    parser.add_argument("--threshold", type=float, default=0.7)
    parser.add_argument("--confirm-llm", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--timeout", type=int, default=3600)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a resumable Sales Navigator search, through the Powerset API or this Mac's LinkedIn session")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_parser = sub.add_parser("run")
    add_common(run_parser)
    run_parser.set_defaults(func=cmd_run)
    cont_parser = sub.add_parser("continue", help="same as run: add queries, score, or pick up after an approval")
    add_common(cont_parser)
    cont_parser.set_defaults(func=cmd_run)
    resolve_parser = sub.add_parser("resolve", help="company names and titles to LinkedIn ids")
    resolve_parser.add_argument("--company", action="append")
    resolve_parser.add_argument("--title", action="append")
    resolve_parser.add_argument("--set-id")
    resolve_parser.set_defaults(func=cmd_resolve)
    status_parser = sub.add_parser("status")
    add_common(status_parser)
    status_parser.set_defaults(func=cmd_status)
    approve_parser = sub.add_parser("approve")
    approve_parser.add_argument("kind", choices=["llm"])
    approve_parser.add_argument("--ledger")
    approve_parser.add_argument("--state")
    approve_parser.add_argument("--approval-id")
    approve_parser.add_argument("--confirm", action="store_true")
    approve_parser.set_defaults(func=cmd_approve)
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
