#!/usr/bin/env python3
"""Resumable Sales Nav local/tool-call handoff orchestrator.

The orchestrator owns local run state and exits with explicit
``blocked_tool_call`` instructions for the harness/agent. The harness calls the
named MCP tool through its native tool layer, saves the JSON response to
``save_response_to``, then reruns the provided continue command.

Normal runs now support a multi-query search plan. Each search is persisted,
ingested from full artifact content, enriched, re-ingested, and finally mutual
member IDs are resolved into local URLs before export/scoring.
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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_BASE = Path(".powerpacks/sales-nav/runs")
PIPELINE_REL = Path("packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py")
ARTIFACTS_REL = Path("packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py")
SCORER_REL = Path("packs/sales-nav/primitives/score_sales_nav_leads/score_sales_nav_leads.py")
DEFAULT_COUNT = 25
DEFAULT_ENRICH_LIMIT = int(os.environ.get("POWERPACKS_SALES_NAV_ENRICH_LIMIT", "100"))
DEFAULT_MUTUAL_URL_LIMIT = int(os.environ.get("POWERPACKS_SALES_NAV_MUTUAL_URL_LIMIT", "100"))

# Keep MCP args aligned with mcp_server.server:mcp_sales_nav_search.
SALES_NAV_SEARCH_ARGS = {
    "set_id",
    "company_ids",
    "company_names",
    "past_company_ids",
    "past_company_names",
    "seniority_ids",
    "function_ids",
    "geography_ids",
    "headcount_ids",
    "title",
    "keywords",
    "count",
    "start_offset",
    "conversation_id",
    "persist_artifact",
}
PLAN_METADATA_KEYS = {
    "id",
    "label",
    "name",
    "description",
    "reason",
    "notes",
    "args",
    "search_args",
    "tool_args",
    "queries",
    "searches",
    "steps",
    "criteria",
    "score_criteria",
}


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


def pipeline_continue_command(ledger: Path, response_path: str | None = None, extra: list[str] | None = None) -> str:
    parts = [sys.executable, str(PIPELINE_REL), "continue", "--ledger", str(ledger)]
    if response_path:
        parts.extend(["--response", response_path])
    if extra:
        parts.extend(extra)
    return " ".join(shlex.quote(part) for part in parts)


def block_tool_call(
    ledger_path_: Path,
    ledger: dict[str, Any],
    *legacy_args: Any,
    step: str | None = None,
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
    save_response_to: str | None = None,
    message: str | None = None,
    plan_index: int | None = None,
    artifact_id: str | None = None,
) -> int:
    # Backward-compatible positional form used by older tests/callers:
    # block_tool_call(lp, ledger, tool, args, save_to, next_cmd, msg)
    legacy_continue_command: str | None = None
    if legacy_args:
        if len(legacy_args) != 5:
            raise TypeError("legacy block_tool_call expects 5 positional args")
        legacy_tool, legacy_payload, legacy_save_to, legacy_next_cmd, legacy_message = legacy_args
        tool_name = tool_name or str(legacy_tool)
        tool_args = tool_args or dict(legacy_payload or {})
        save_response_to = save_response_to or str(legacy_save_to)
        legacy_continue_command = str(legacy_next_cmd)
        message = message or str(legacy_message)
        step = step or str(tool_name)
    if not step or not tool_name or tool_args is None or not save_response_to:
        raise TypeError("block_tool_call requires step, tool_name, tool_args, and save_response_to")
    message = message or f"Call {tool_name}, save the JSON response, then continue."
    block = {
        "primitive": "sales_nav_pipeline",
        "status": "blocked_tool_call",
        "message": message,
        "tool_server": "powerset-search",
        "tool_name": tool_name,
        "tool_args": tool_args,
        "save_response_to": save_response_to,
        "continue_command": legacy_continue_command or pipeline_continue_command(ledger_path_, save_response_to),
        "ledger": str(ledger_path_),
        "step": step,
    }
    if plan_index is not None:
        block["plan_index"] = plan_index
    if artifact_id:
        block["artifact_id"] = artifact_id
    ledger["current_block"] = block
    mark(ledger_path_, ledger, step, "blocked_tool_call", summary=block)
    emit(block)
    return 30


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
    if not args.query or not args.set_id:
        raise RuntimeError("Need --query and --set-id for init, or pass --state")
    conversation_id = args.conversation_id or str(uuid.uuid4())
    out_dir = path.parent
    cmd = [
        sys.executable,
        str(ROOT / ARTIFACTS_REL),
        "init",
        "--query", args.query,
        "--set-id", args.set_id,
        "--conversation-id", conversation_id,
        "--out-dir", str(out_dir),
    ]
    payload = require(run(cmd), "sales_nav_artifacts init")
    state = Path(payload["state"])
    ledger.setdefault("artifacts", {}).update({
        "state": str(state),
        "run_dir": str(state.parent),
        "conversation_id": conversation_id,
        "set_id": args.set_id,
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
        tool_args = {key: value for key, value in raw_args.items() if key in SALES_NAV_SEARCH_ARGS and value not in (None, "", [], {})}
        if set_id:
            tool_args["set_id"] = set_id
        elif "set_id" not in tool_args:
            raise RuntimeError("Search plan item is missing set_id")
        if conversation_id:
            tool_args["conversation_id"] = conversation_id
        tool_args["persist_artifact"] = True
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
    artifacts = ledger.setdefault("artifacts", {})
    existing = artifacts.get("search_plan")
    if isinstance(existing, list):
        return existing

    raw: Any = None
    source = None
    if args.search_plan_json:
        raw = read_json(Path(args.search_plan_json), None)
        source = str(args.search_plan_json)
    elif args.search_args_json:
        raw = read_json(Path(args.search_args_json), None)
        source = str(args.search_args_json)

    if raw is None:
        # Existing-state follow-ups (export/score/resolve mutual URLs) do not
        # need a search plan.
        if state.exists() and read_json(state, {}).get("counts", {}).get("leads", 0):
            return []
        raise RuntimeError("Need --search-args-json or --search-plan-json for a new Sales Nav search")

    state_payload = read_json(state, {}) or {}
    set_id = args.set_id or artifacts.get("set_id") or state_payload.get("set_id")
    conversation_id = artifacts.get("conversation_id") or state_payload.get("conversation_id") or args.conversation_id
    plan, criteria = normalize_search_plan(raw, set_id=set_id, conversation_id=conversation_id, default_count=args.count)
    if not plan:
        raise RuntimeError("Search plan is empty")
    artifacts["search_plan"] = plan
    artifacts["search_plan_source"] = source
    if criteria and not artifacts.get("score_criteria"):
        artifacts["score_criteria"] = criteria
    save(ledger_path_, ledger)
    return plan


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


def member_ids_for_enrichment(state_path: Path, *, artifact_id: str | None, limit: int) -> list[int]:
    rows = load_leads(state_path)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if artifact_id and str(row.get("artifact_id") or "") != str(artifact_id):
            continue
        if truthy(row.get("enriched")):
            continue
        if not row.get("member_id"):
            continue
        candidates.append(row)
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
    cmd = [
        sys.executable,
        str(ROOT / ARTIFACTS_REL),
        "pending-mutual-ids",
        "--state", str(state_path),
        "--limit", str(limit),
    ]
    if include_unresolved:
        cmd.append("--include-unresolved")
    return require(run(cmd), "pending-mutual-ids")


def response_summary(payload: dict[str, Any]) -> dict[str, Any]:
    keep = [
        "artifact_id",
        "id",
        "total_count",
        "max_total_count",
        "results_returned",
        "accumulated_count",
        "has_more",
        "next_start_offset",
        "enriched",
        "failed",
        "unresolvable",
        "total_requested",
        "updated_leads",
        "resolved",
        "unresolved",
        "cache_only",
        "error",
    ]
    summary = {key: payload.get(key) for key in keep if key in payload}
    if isinstance(summary.get("resolved"), dict):
        summary["resolved_count"] = len(summary.pop("resolved"))
    if isinstance(summary.get("unresolved"), list):
        summary["unresolved_count"] = len(summary.pop("unresolved"))
    artifact = payload.get("artifact")
    if not summary.get("artifact_id") and isinstance(artifact, dict) and artifact.get("id"):
        summary["artifact_id"] = artifact.get("id")
    return summary


def ingest_page(state: Path, response: Path, *, prefer_content: bool, step: str, ledger_path_: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(ROOT / ARTIFACTS_REL),
        "ingest-page",
        "--state", str(state),
        "--response", str(response),
    ]
    if prefer_content:
        cmd.append("--prefer-content")
    payload = require(run(cmd), step)
    mark(ledger_path_, ledger, step, "completed", summary=payload, command=" ".join(map(shlex.quote, cmd)))
    return payload


def ingest_member_urls(state: Path, response: Path, *, step: str, ledger_path_: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(ROOT / ARTIFACTS_REL),
        "ingest-member-urls",
        "--state", str(state),
        "--response", str(response),
    ]
    payload = require(run(cmd), step)
    mark(ledger_path_, ledger, step, "completed", summary=payload, command=" ".join(map(shlex.quote, cmd)))
    return payload


def process_response(args: argparse.Namespace, ledger_path_: Path, ledger: dict[str, Any], state: Path) -> None:
    if not args.response:
        return
    response_path = Path(args.response)
    payload = read_json(response_path, {}) or {}
    block = ledger.get("current_block") or {}
    step = block.get("step") or ("ingest_full_artifact" if args.prefer_content else "ingest_page")
    tool_name = block.get("tool_name")

    if tool_name == "sales_nav_search":
        summary = response_summary(payload)
        artifact_id = summary.get("artifact_id")
        if artifact_id:
            ledger.setdefault("artifacts", {}).setdefault("search_artifacts", {})[step] = artifact_id
        mark(ledger_path_, ledger, step, "completed", summary=summary, response=str(response_path))
        # If persistence was unavailable, still ingest the compact response so
        # the run produces local files.
        if not artifact_id:
            ingest_page(state, response_path, prefer_content=False, step=f"ingest_compact_{step}", ledger_path_=ledger_path_, ledger=ledger)
    elif tool_name == "get_artifact" or args.prefer_content:
        ingest_page(state, response_path, prefer_content=True, step=step, ledger_path_=ledger_path_, ledger=ledger)
    elif tool_name == "enrich_extended_profiles":
        mark(ledger_path_, ledger, step, "completed", summary=response_summary(payload), response=str(response_path))
    elif tool_name == "sales_nav_resolve_member_ids":
        ingest_member_urls(state, response_path, step=step, ledger_path_=ledger_path_, ledger=ledger)
    else:
        # Legacy/manual continuation: store the response and continue.
        mark(ledger_path_, ledger, step, "completed", summary=response_summary(payload), response=str(response_path))

    ledger["current_block"] = None
    save(ledger_path_, ledger)


def latest_artifact_for_search(ledger: dict[str, Any], search_step: str) -> str | None:
    summary = ledger.get("steps", {}).get(search_step, {}).get("summary") or {}
    artifact_id = summary.get("artifact_id")
    if artifact_id:
        return str(artifact_id)
    artifact = summary.get("artifact")
    if isinstance(artifact, dict) and artifact.get("id"):
        return str(artifact["id"])
    return None


def advance_searches(args: argparse.Namespace, ledger_path_: Path, ledger: dict[str, Any], state: Path) -> int | None:
    plan = search_plan(args, ledger_path_, ledger, state)
    run_dir = state.parent
    pages = run_dir / "pages"
    pages.mkdir(parents=True, exist_ok=True)

    for index, item in enumerate(plan):
        search_step = f"search_{index:03d}"
        artifact_step = f"get_artifact_{index:03d}"
        enrich_step = f"enrich_profiles_{index:03d}"
        enriched_artifact_step = f"get_artifact_after_enrich_{index:03d}"

        if not done(ledger, search_step):
            save_to = str(pages / f"sales-nav-search-{index:03d}.response.json")
            tool_args = dict(item.get("args") or {})
            return block_tool_call(
                ledger_path_,
                ledger,
                step=search_step,
                tool_name="sales_nav_search",
                tool_args=tool_args,
                save_response_to=save_to,
                plan_index=index,
                message=f"Call Sales Nav search for plan item {index + 1}/{len(plan)} ({item.get('label')}). Save the JSON response, then continue.",
            )

        artifact_id = latest_artifact_for_search(ledger, search_step)
        if artifact_id and not done(ledger, artifact_step):
            save_to = str(pages / f"artifact-full-{index:03d}.json")
            payload = {"artifact_id": artifact_id, "offset": 0, "limit": args.artifact_limit, "include_content": True}
            return block_tool_call(
                ledger_path_,
                ledger,
                step=artifact_step,
                tool_name="get_artifact",
                tool_args=payload,
                save_response_to=save_to,
                plan_index=index,
                artifact_id=artifact_id,
                message="Call get_artifact(include_content=true) for the persisted Sales Nav search, save it, then continue.",
            )
        if not artifact_id and not done(ledger, f"ingest_compact_{search_step}"):
            mark(ledger_path_, ledger, artifact_step, "completed", summary={"reason": "no_artifact_id_compact_response_ingested"})

        if not args.skip_enrich and not done(ledger, enrich_step):
            member_ids = member_ids_for_enrichment(state, artifact_id=artifact_id, limit=args.enrich_limit)
            if member_ids:
                save_to = str(pages / f"enrich-profiles-{index:03d}.response.json")
                state_payload = read_json(state, {}) or {}
                payload = {
                    "conversation_id": (ledger.get("artifacts") or {}).get("conversation_id") or state_payload.get("conversation_id"),
                    "member_ids": member_ids,
                    "set_id": (ledger.get("artifacts") or {}).get("set_id") or state_payload.get("set_id"),
                }
                return block_tool_call(
                    ledger_path_,
                    ledger,
                    step=enrich_step,
                    tool_name="enrich_extended_profiles",
                    tool_args=payload,
                    save_response_to=save_to,
                    plan_index=index,
                    artifact_id=artifact_id,
                    message=f"Enrich {len(member_ids)} Sales Nav lead profiles, save the JSON response, then continue.",
                )
            mark(ledger_path_, ledger, enrich_step, "completed", summary={"reason": "no_unenriched_member_ids", "artifact_id": artifact_id})

        if not args.skip_enrich and artifact_id and done(ledger, enrich_step) and not done(ledger, enriched_artifact_step):
            save_to = str(pages / f"artifact-full-after-enrich-{index:03d}.json")
            payload = {"artifact_id": artifact_id, "offset": 0, "limit": args.artifact_limit, "include_content": True}
            return block_tool_call(
                ledger_path_,
                ledger,
                step=enriched_artifact_step,
                tool_name="get_artifact",
                tool_args=payload,
                save_response_to=save_to,
                plan_index=index,
                artifact_id=artifact_id,
                message="Reload the enriched Sales Nav artifact with include_content=true, save it, then continue.",
            )

    return None


def resolve_mutual_urls(args: argparse.Namespace, ledger_path_: Path, ledger: dict[str, Any], state: Path) -> int | None:
    if args.skip_mutual_url_resolution:
        mark(ledger_path_, ledger, "resolve_mutual_urls", "completed", summary={"reason": "skipped_by_flag"})
        return None
    pending = pending_mutual_ids(state, limit=args.mutual_url_limit, include_unresolved=bool(args.resolve_mutuals_external))
    member_ids = pending.get("member_ids") or []
    if not member_ids:
        if step_status(ledger, "resolve_mutual_urls") != "completed":
            mark(ledger_path_, ledger, "resolve_mutual_urls", "completed", summary={"pending": 0})
        return None

    batch_index = int((ledger.get("artifacts") or {}).get("mutual_url_batches") or 0)
    step = f"resolve_mutual_urls_{batch_index:03d}"
    if done(ledger, step):
        ledger.setdefault("artifacts", {})["mutual_url_batches"] = batch_index + 1
        save(ledger_path_, ledger)
        return resolve_mutual_urls(args, ledger_path_, ledger, state)
    save_to = str(state.parent / "pages" / f"member-urls-{batch_index:03d}.response.json")
    return block_tool_call(
        ledger_path_,
        ledger,
        step=step,
        tool_name="sales_nav_resolve_member_ids",
        tool_args={"member_ids": member_ids, "use_external_apis": bool(args.resolve_mutuals_external)},
        save_response_to=save_to,
        message=f"Resolve {len(member_ids)} mutual member IDs to LinkedIn URLs, save the JSON response, then continue.",
    )


def enrich_mutual_attribution_step(args: argparse.Namespace, ledger_path_: Path, ledger: dict[str, Any], state: Path) -> None:
    """Enrich mutuals with operator attribution. Runs discover if --discover-mutuals."""
    if done(ledger, "enrich_mutual_attribution") and not args.force:
        return
    mode = "discover" if getattr(args, "discover_mutuals", False) else "attribution"
    cmd = [
        sys.executable,
        str(ROOT / "packs/sales-nav/primitives/enrich_mutual_attribution/enrich_mutual_attribution.py"),
        "--state", str(state),
        "--mode", mode,
        "--env-file", args.env_file,
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
    if done(ledger, "export") and not args.force:
        return
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
        sys.executable,
        str(ROOT / SCORER_REL),
        "--state", str(state),
        "--criteria", criteria,
        "--threshold", str(args.threshold),
    ]
    result = require(run(cmd, timeout=args.timeout), "score_leads")
    ledger.setdefault("artifacts", {})["scores"] = result.get("output_dir") or result.get("matches_csv")
    mark(ledger_path_, ledger, "score_leads", "completed", summary=result, command=" ".join(map(shlex.quote, cmd)))
    return None


def cmd_run(args: argparse.Namespace) -> int:
    try:
        ledger_path_ = ledger_path(args)
        ledger = load(ledger_path_)
        ledger["current_block"] = None if not args.response else ledger.get("current_block")
        save(ledger_path_, ledger)
        state = ensure_init(args, ledger_path_, ledger)
        process_response(args, ledger_path_, ledger, state)

        blocked = advance_searches(args, ledger_path_, ledger, state)
        if blocked is not None:
            return blocked
        blocked = resolve_mutual_urls(args, ledger_path_, ledger, state)
        if blocked is not None:
            return blocked
        enrich_mutual_attribution_step(args, ledger_path_, ledger, state)
        export_state(args, ledger_path_, ledger, state)
        blocked = score_if_requested(args, ledger_path_, ledger, state)
        if blocked is not None:
            return blocked

        ledger["current_block"] = None
        save(ledger_path_, ledger)
        emit({
            "primitive": "sales_nav_pipeline",
            "status": "completed",
            "ledger": str(ledger_path_),
            "state": str(state),
            "artifacts": ledger.get("artifacts", {}),
        })
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
        "ledger": str(path),
        "current_block": ledger.get("current_block"),
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
    parser.add_argument("--set-id")
    parser.add_argument("--conversation-id")
    parser.add_argument("--run-id")
    parser.add_argument("--search-args-json")
    parser.add_argument("--search-plan-json")
    parser.add_argument("--response", type=Path)
    # Legacy flags retained for old continue commands. New continues infer the
    # response action from ledger.current_block.step/tool_name.
    parser.add_argument("--prefer-content", action="store_true")
    parser.add_argument("--enriched", action="store_true")
    parser.add_argument("--require-enriched", action="store_true")
    parser.add_argument("--skip-enrich", action="store_true")
    parser.add_argument("--enrich-limit", type=int, default=DEFAULT_ENRICH_LIMIT)
    parser.add_argument("--artifact-limit", type=int, default=1000)
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
    parser = argparse.ArgumentParser(description="Run a resumable Sales Nav MCP/local artifact workflow")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_parser = sub.add_parser("run")
    add_common(run_parser)
    run_parser.set_defaults(func=cmd_run)
    cont_parser = sub.add_parser("continue")
    add_common(cont_parser)
    cont_parser.set_defaults(func=cmd_run)
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
