#!/usr/bin/env python3
"""File-backed Sales Navigator MCP artifact store.

This primitive intentionally does not call the MCP itself. Agents save each MCP
page response to a small JSON file, then pass that file path here. The primitive
normalizes and appends/upserts local JSONL handoff files so later steps only
need paths, not large lead payloads in chat context. CSVs are written only as
final user-facing exports.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


LEAD_FIELDS = [
    "conversation_id",
    "set_id",
    "artifact_id",
    "member_id",
    "profile_id",
    "name",
    "title",
    "headline",
    "summary",
    "company",
    "location",
    "linkedin_url",
    "profile_picture_url",
    "source_account_ids",
    "mutual_count",
    "total_mutual_count",
    "total_interactions",
    "mutual_member_ids",
    "operators",
    "enriched",
    "experiences",
    "education",
    "result_index",
    "page_offset",
    "first_seen_at",
    "last_seen_at",
    "times_seen",
]

MUTUAL_FIELDS = [
    "conversation_id",
    "set_id",
    "artifact_id",
    "lead_member_id",
    "lead_name",
    "lead_linkedin_url",
    "mutual_member_id",
    "mutual_name",
    "mutual_person_id",
    "mutual_linkedin_url",
    "total_interactions",
    "source_account_ids",
    "operators",
    "first_seen_at",
    "last_seen_at",
    "times_seen",
]

MANIFEST_NAME = "manifest.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open() as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{path}:{line_number}: invalid JSONL") from exc
            if not isinstance(row, dict):
                raise RuntimeError(f"{path}:{line_number}: expected object")
            rows.append(row)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def slugify(value: str, max_length: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return (slug[:max_length].strip("-") or "sales-nav")


def default_run_dir(query: str, run_id: str | None = None) -> Path:
    rid = run_id or f"sales-nav-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    return Path(".powerpacks") / "sales-nav" / "runs" / f"{rid}-{slugify(query)}"


def state_paths(state: dict[str, Any]) -> dict[str, Path]:
    files = state.get("files") or {}
    return {key: Path(value) for key, value in files.items() if isinstance(value, str)}


def load_state(path: Path) -> dict[str, Any]:
    state = read_json(path)
    if not isinstance(state, dict):
        raise RuntimeError("state must be a JSON object")
    return state


def init_state(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.out_dir) if args.out_dir else default_run_dir(args.query, args.run_id)
    state_path = Path(args.state) if args.state else run_dir / "state.json"
    now = now_iso()
    files = {
        "leads_jsonl": str(run_dir / "leads.jsonl"),
        "mutuals_jsonl": str(run_dir / "mutuals.jsonl"),
        "member_urls_json": str(run_dir / "member_urls.json"),
        "final_leads_csv": str(run_dir / "exports" / "leads.csv"),
        "final_mutuals_csv": str(run_dir / "exports" / "mutuals.csv"),
        "manifest": str(run_dir / MANIFEST_NAME),
    }
    state = {
        "run_id": args.run_id or run_dir.name,
        "query": args.query,
        "set_id": args.set_id,
        "conversation_id": args.conversation_id,
        "created_at": now,
        "updated_at": now,
        "files": files,
        "pages": [],
        "artifact_ids": [],
        "counts": {"leads": 0, "mutual_edges": 0, "member_urls": 0},
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(Path(files["leads_jsonl"]), [])
    write_jsonl(Path(files["mutuals_jsonl"]), [])
    write_json(Path(files["member_urls_json"]), {"resolved": {}, "unresolved": []})
    write_json(state_path, state)
    write_manifest(state_path, state)
    return {"state": str(state_path), **state}


def response_leads(payload: dict[str, Any], *, prefer_content: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (leads, page_meta) from sales_nav_search or get_artifact output."""
    content_leads = (((payload.get("content") or {}).get("extended_results") or {}).get("leads") or [])
    if prefer_content and isinstance(content_leads, list) and content_leads:
        meta = {
            "source": "get_artifact_content",
            "artifact_id": payload.get("id"),
            "conversation_id": payload.get("conversation_id"),
            "offset": 0,
            "returned": len(content_leads),
            "total": len(content_leads),
            "has_more": False,
            "next_offset": None,
        }
        return [lead for lead in content_leads if isinstance(lead, dict)], meta

    if isinstance(payload.get("page"), dict):
        page = payload["page"]
        leads = page.get("results") or []
        meta = {
            "source": "get_artifact",
            "artifact_id": payload.get("id"),
            "conversation_id": payload.get("conversation_id"),
            "offset": page.get("offset", 0),
            "limit": page.get("limit"),
            "returned": page.get("returned", len(leads)),
            "total": page.get("total"),
            "has_more": page.get("has_more"),
            "next_offset": page.get("next_offset"),
        }
        return [lead for lead in leads if isinstance(lead, dict)], meta

    leads = payload.get("leads") or []
    meta = {
        "source": "sales_nav_search",
        "artifact_id": payload.get("artifact_id") or (payload.get("artifact") or {}).get("id"),
        "offset": payload.get("start_offset") or (payload.get("filters_used") or {}).get("start_offset") or 0,
        "returned": payload.get("results_returned", len(leads)),
        "total": payload.get("total_count"),
        "max_total_count": payload.get("max_total_count"),
        "has_more": payload.get("has_more"),
        "next_offset": payload.get("next_start_offset"),
        "filters_used": payload.get("filters_used"),
        "reconnect_required": payload.get("reconnect_required"),
        "error": payload.get("error"),
    }
    return [lead for lead in leads if isinstance(lead, dict)], meta


def list_ints(value: Any) -> list[int]:
    out: list[int] = []
    for item in value or []:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def source_account_ids_from_lead(lead: dict[str, Any]) -> list[str]:
    ids = [str(x) for x in (lead.get("source_account_ids") or []) if x]
    for op in lead.get("operators") or []:
        if isinstance(op, dict):
            for key in ["source_account_id", "account_id"]:
                if op.get(key):
                    ids.append(str(op[key]))
    return list(dict.fromkeys(ids))


def operator_ids(operators: Any) -> list[str]:
    ids = []
    for op in operators or []:
        if isinstance(op, dict) and op.get("operator_id"):
            ids.append(str(op["operator_id"]))
    return list(dict.fromkeys(ids))


def normalize_lead(
    lead: dict[str, Any],
    *,
    state: dict[str, Any],
    page_meta: dict[str, Any],
    index: int,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = now_iso()
    member_id = lead.get("member_id")
    member_id_str = str(member_id) if member_id is not None else ""
    source_ids = source_account_ids_from_lead(lead)
    mutual_ids = list_ints(lead.get("mutual_member_ids"))
    if not mutual_ids:
        mutual_ids = list_ints([m.get("member_id") for m in lead.get("mutuals") or [] if isinstance(m, dict)])
    row = dict(existing or {})
    row.pop("source_account_id", None)
    row.update({
        "conversation_id": state.get("conversation_id") or page_meta.get("conversation_id"),
        "set_id": state.get("set_id"),
        "artifact_id": page_meta.get("artifact_id") or row.get("artifact_id"),
        "member_id": member_id_str,
        "profile_id": lead.get("profile_id") or row.get("profile_id"),
        "name": lead.get("name") or row.get("name"),
        "title": lead.get("title") or row.get("title"),
        "headline": lead.get("headline") or row.get("headline"),
        "summary": lead.get("summary") or row.get("summary"),
        "company": lead.get("company") or row.get("company"),
        "location": lead.get("location") or row.get("location"),
        "linkedin_url": lead.get("linkedin_url") or row.get("linkedin_url"),
        "profile_picture_url": lead.get("profile_picture_url") or lead.get("profile_pic_url") or row.get("profile_picture_url"),
        "source_account_ids": list(dict.fromkeys((row.get("source_account_ids") or []) + source_ids)),
        "mutual_count": max(int_or_zero(row.get("mutual_count")), int_or_zero(lead.get("mutual_count")), len(mutual_ids)),
        "total_mutual_count": max(int_or_zero(row.get("total_mutual_count")), int_or_zero(lead.get("total_mutual_count")), len(mutual_ids)),
        "total_interactions": max(int_or_zero(row.get("total_interactions")), int_or_zero(lead.get("total_interactions"))),
        "mutual_member_ids": list(dict.fromkeys((row.get("mutual_member_ids") or []) + mutual_ids)),
        "operators": merge_operator_lists(row.get("operators") or [], lead.get("operators") or []),
        "enriched": bool(lead.get("enriched") or row.get("enriched") or lead.get("experiences") or lead.get("education")),
        "experiences": lead.get("experiences") or row.get("experiences") or [],
        "education": lead.get("education") or row.get("education") or [],
        "result_index": row.get("result_index", page_meta.get("offset", 0) + index),
        "page_offset": page_meta.get("offset", 0),
        "first_seen_at": row.get("first_seen_at") or now,
        "last_seen_at": now,
        "times_seen": int(row.get("times_seen") or 0) + 1,
    })
    return row


def merge_operator_lists(existing: list[Any], incoming: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for op in list(existing or []) + list(incoming or []):
        if not isinstance(op, dict):
            continue
        key = str(op.get("operator_id") or op.get("operator_name") or json.dumps(op, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        out.append(op)
    return out


def mutual_rows_for_lead(
    lead: dict[str, Any],
    lead_row: dict[str, Any],
    *,
    state: dict[str, Any],
    page_meta: dict[str, Any],
) -> list[dict[str, Any]]:
    now = now_iso()
    source_ids = lead_row.get("source_account_ids") or []
    rows: list[dict[str, Any]] = []
    mutuals = [m for m in lead.get("mutuals") or [] if isinstance(m, dict)]
    known_ids = {str(m.get("member_id")) for m in mutuals if m.get("member_id") is not None}
    for mid in lead_row.get("mutual_member_ids") or []:
        if str(mid) not in known_ids:
            mutuals.append({"member_id": mid})
    for mutual in mutuals:
        mid = mutual.get("member_id")
        if mid is None:
            continue
        operators = mutual.get("operators") or []
        rows.append({
            "conversation_id": state.get("conversation_id") or page_meta.get("conversation_id"),
            "set_id": state.get("set_id"),
            "artifact_id": page_meta.get("artifact_id"),
            "lead_member_id": str(lead_row.get("member_id") or ""),
            "lead_name": lead_row.get("name"),
            "lead_linkedin_url": lead_row.get("linkedin_url"),
            "mutual_member_id": str(mid),
            "mutual_name": mutual.get("name") or mutual.get("first_name"),
            "mutual_person_id": mutual.get("person_id"),
            "mutual_linkedin_url": mutual.get("linkedin_url"),
            "total_interactions": int_or_zero(mutual.get("total_interactions")),
            "source_account_ids": source_ids,
            "operators": operators,
            "first_seen_at": now,
            "last_seen_at": now,
            "times_seen": 1,
        })
    return rows


def upsert_by_key(rows: list[dict[str, Any]], incoming: list[dict[str, Any]], key_fields: list[str]) -> list[dict[str, Any]]:
    by_key = {tuple(str(row.get(k) or "") for k in key_fields): dict(row) for row in rows}
    order = [tuple(str(row.get(k) or "") for k in key_fields) for row in rows]
    for row in incoming:
        key = tuple(str(row.get(k) or "") for k in key_fields)
        if key in by_key:
            existing = by_key[key]
            existing.pop("source_account_id", None)
            for field, value in row.items():
                if field == "first_seen_at":
                    continue
                if field == "times_seen":
                    existing[field] = int(existing.get(field) or 0) + int(value or 0)
                elif field == "total_interactions":
                    existing[field] = max(int_or_zero(existing.get(field)), int_or_zero(value))
                elif field in {"source_account_ids", "operators", "mutual_member_ids"}:
                    if field == "operators":
                        existing[field] = merge_operator_lists(existing.get(field) or [], value or [])
                    else:
                        existing[field] = list(dict.fromkeys((existing.get(field) or []) + (value or [])))
                elif value not in (None, "", [], {}):
                    existing[field] = value
            existing["last_seen_at"] = row.get("last_seen_at") or now_iso()
            by_key[key] = existing
        else:
            row.pop("source_account_id", None)
            by_key[key] = row
            order.append(key)
    return [by_key[key] for key in order]


def write_manifest(state_path: Path, state: dict[str, Any]) -> None:
    paths = state_paths(state)
    leads = read_jsonl(paths["leads_jsonl"])
    mutuals = read_jsonl(paths["mutuals_jsonl"])
    member_urls = read_json(paths["member_urls_json"]) if paths["member_urls_json"].exists() else {"resolved": {}, "unresolved": []}
    state["counts"] = {
        "leads": len(leads),
        "mutual_edges": len(mutuals),
        "member_urls": len(member_urls.get("resolved") or {}),
    }
    state["updated_at"] = now_iso()
    manifest = {
        "state": str(state_path),
        "query": state.get("query"),
        "set_id": state.get("set_id"),
        "conversation_id": state.get("conversation_id"),
        "files": state.get("files"),
        "artifact_ids": state.get("artifact_ids") or [],
        "pages": state.get("pages") or [],
        "counts": state["counts"],
        "updated_at": state["updated_at"],
    }
    write_json(paths["manifest"], manifest)
    write_json(state_path, state)


def cmd_init(args: argparse.Namespace) -> None:
    print(json.dumps(init_state(args), indent=2, sort_keys=True))


def cmd_ingest_page(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = load_state(state_path)
    paths = state_paths(state)
    payload = read_json(Path(args.response))
    if not isinstance(payload, dict):
        raise SystemExit("response must be a JSON object")
    leads, page_meta = response_leads(payload, prefer_content=args.prefer_content)
    if args.artifact_id:
        page_meta["artifact_id"] = args.artifact_id
    if args.offset is not None:
        page_meta["offset"] = args.offset
    if page_meta.get("error"):
        raise SystemExit(f"sales nav response error: {page_meta['error']}")
    if page_meta.get("reconnect_required"):
        raise SystemExit("sales nav response requires reconnect")

    existing_leads = read_jsonl(paths["leads_jsonl"])
    for row in existing_leads:
        row.pop("source_account_id", None)
    by_member = {str(row.get("member_id")): row for row in existing_leads if row.get("member_id")}
    normalized_leads: list[dict[str, Any]] = []
    normalized_mutuals: list[dict[str, Any]] = []
    for idx, lead in enumerate(leads):
        mid = str(lead.get("member_id") or "")
        if not mid:
            continue
        lead_row = normalize_lead(lead, state=state, page_meta=page_meta, index=idx, existing=by_member.get(mid))
        normalized_leads.append(lead_row)
        normalized_mutuals.extend(mutual_rows_for_lead(lead, lead_row, state=state, page_meta=page_meta))

    all_leads = upsert_by_key(existing_leads, normalized_leads, ["member_id"])
    existing_mutuals = read_jsonl(paths["mutuals_jsonl"])
    for row in existing_mutuals:
        row.pop("source_account_id", None)
    all_mutuals = upsert_by_key(existing_mutuals, normalized_mutuals, ["lead_member_id", "mutual_member_id"])
    write_jsonl(paths["leads_jsonl"], all_leads)
    write_jsonl(paths["mutuals_jsonl"], all_mutuals)

    artifact_id = page_meta.get("artifact_id")
    if artifact_id and artifact_id not in state.setdefault("artifact_ids", []):
        state["artifact_ids"].append(artifact_id)
    state.setdefault("pages", []).append(page_meta)
    write_manifest(state_path, state)
    print(json.dumps({
        "state": str(state_path),
        "leads_ingested": len(normalized_leads),
        "lead_count": len(all_leads),
        "mutual_edges_ingested": len(normalized_mutuals),
        "mutual_edge_count": len(all_mutuals),
        "artifact_id": artifact_id,
        "files": state.get("files"),
    }, indent=2, sort_keys=True))


def cmd_ingest_member_urls(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = load_state(state_path)
    paths = state_paths(state)
    payload = read_json(Path(args.response))
    if not isinstance(payload, dict):
        raise SystemExit("response must be a JSON object")
    member_urls_path = paths["member_urls_json"]
    existing = read_json(member_urls_path) if member_urls_path.exists() else {"resolved": {}, "unresolved": []}
    resolved = dict(existing.get("resolved") or {})
    for mid, url in (payload.get("resolved") or {}).items():
        if url:
            resolved[str(mid)] = str(url)
    unresolved = list(dict.fromkeys([str(x) for x in (existing.get("unresolved") or []) + (payload.get("unresolved") or []) if x]))
    unresolved = [mid for mid in unresolved if mid not in resolved]
    write_json(member_urls_path, {"resolved": resolved, "unresolved": unresolved, "cache_only": payload.get("cache_only")})

    # Patch mutual rows with newly resolved URLs for easier CSV export/lookup.
    mutuals = read_jsonl(paths["mutuals_jsonl"])
    for row in mutuals:
        mid = str(row.get("mutual_member_id") or "")
        if mid in resolved and not row.get("mutual_linkedin_url"):
            row["mutual_linkedin_url"] = resolved[mid]
    write_jsonl(paths["mutuals_jsonl"], mutuals)
    write_manifest(state_path, state)
    print(json.dumps({
        "state": str(state_path),
        "resolved_count": len(resolved),
        "unresolved_count": len(unresolved),
        "member_urls_json": str(member_urls_path),
    }, indent=2, sort_keys=True))


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return value


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fields})


def cmd_export(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    state = load_state(state_path)
    paths = state_paths(state)
    leads = read_jsonl(paths["leads_jsonl"])
    mutuals = read_jsonl(paths["mutuals_jsonl"])
    for row in leads + mutuals:
        row.pop("source_account_id", None)
    leads_csv = paths.get("final_leads_csv") or paths.get("leads_csv")
    mutuals_csv = paths.get("final_mutuals_csv") or paths.get("mutuals_csv")
    if not leads_csv or not mutuals_csv:
        raise SystemExit("state is missing final CSV paths")
    write_csv(leads_csv, leads, LEAD_FIELDS)
    write_csv(mutuals_csv, mutuals, MUTUAL_FIELDS)
    write_manifest(state_path, state)
    print(json.dumps({
        "state": str(state_path),
        "leads_csv": str(leads_csv),
        "mutuals_csv": str(mutuals_csv),
        "lead_count": len(leads),
        "mutual_edge_count": len(mutuals),
    }, indent=2, sort_keys=True))


def cmd_pending_mutual_ids(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state))
    paths = state_paths(state)
    mutuals = read_jsonl(paths["mutuals_jsonl"])
    urls = read_json(paths["member_urls_json"]) if paths["member_urls_json"].exists() else {"resolved": {}, "unresolved": []}
    resolved = set(str(k) for k in (urls.get("resolved") or {}))
    known_unresolved = set(str(x) for x in (urls.get("unresolved") or []))
    ids = []
    for row in mutuals:
        mid = str(row.get("mutual_member_id") or "")
        if not mid or mid in resolved or row.get("mutual_linkedin_url"):
            continue
        if mid in known_unresolved and not args.include_unresolved:
            continue
        ids.append(mid)
    ids = list(dict.fromkeys(ids))
    if args.limit:
        ids = ids[: args.limit]
    print(json.dumps({"member_ids": [int(x) for x in ids if x.isdigit()], "count": len(ids)}, indent=2, sort_keys=True))


def cmd_lookup(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state))
    paths = state_paths(state)
    leads = read_jsonl(paths["leads_jsonl"])
    mutuals = read_jsonl(paths["mutuals_jsonl"])
    q = (args.query or "").lower().strip()
    member_ids = set(str(x) for x in args.member_id or [])
    matched = []
    for lead in leads:
        profile_blob = json.dumps({
            "summary": lead.get("summary"),
            "experiences": lead.get("experiences") or [],
            "education": lead.get("education") or [],
        }, sort_keys=True)
        haystack = " ".join(
            str(lead.get(k) or "")
            for k in ["member_id", "name", "title", "headline", "company", "location"]
        )
        haystack = f"{haystack} {profile_blob}".lower()
        if member_ids and str(lead.get("member_id")) not in member_ids:
            continue
        if q and q not in haystack:
            continue
        lead_mutuals = [m for m in mutuals if str(m.get("lead_member_id")) == str(lead.get("member_id"))]
        matched.append({
            "member_id": lead.get("member_id"),
            "name": lead.get("name"),
            "title": lead.get("title"),
            "headline": lead.get("headline"),
            "company": lead.get("company"),
            "location": lead.get("location"),
            "linkedin_url": lead.get("linkedin_url"),
            "source_account_ids": lead.get("source_account_ids") or [],
            "mutual_count": lead.get("mutual_count"),
            "total_interactions": lead.get("total_interactions"),
            "mutuals": [
                {
                    "member_id": m.get("mutual_member_id"),
                    "name": m.get("mutual_name"),
                    "linkedin_url": m.get("mutual_linkedin_url"),
                    "total_interactions": m.get("total_interactions"),
                    "operators": m.get("operators") or [],
                    "source_account_ids": m.get("source_account_ids") or [],
                }
                for m in lead_mutuals[: args.mutual_limit]
            ],
        })
        if args.limit and len(matched) >= args.limit:
            break
    print(json.dumps({"results": matched, "count": len(matched), "state": args.state}, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize Sales Nav MCP results into local file handoffs")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--query", required=True)
    init.add_argument("--set-id", required=True)
    init.add_argument("--conversation-id", required=True)
    init.add_argument("--run-id")
    init.add_argument("--out-dir")
    init.add_argument("--state")
    init.set_defaults(func=cmd_init)

    ingest = sub.add_parser("ingest-page")
    ingest.add_argument("--state", required=True)
    ingest.add_argument("--response", required=True)
    ingest.add_argument("--artifact-id")
    ingest.add_argument("--offset", type=int)
    ingest.add_argument("--prefer-content", action="store_true", help="When get_artifact include_content=true is saved, ingest content.extended_results.leads instead of the compact page")
    ingest.set_defaults(func=cmd_ingest_page)

    urls = sub.add_parser("ingest-member-urls")
    urls.add_argument("--state", required=True)
    urls.add_argument("--response", required=True)
    urls.set_defaults(func=cmd_ingest_member_urls)

    pending = sub.add_parser("pending-mutual-ids")
    pending.add_argument("--state", required=True)
    pending.add_argument("--limit", type=int)
    pending.add_argument("--include-unresolved", action="store_true", help="Retry member IDs that a previous cache-only resolution marked unresolved")
    pending.set_defaults(func=cmd_pending_mutual_ids)

    export = sub.add_parser("export")
    export.add_argument("--state", required=True)
    export.set_defaults(func=cmd_export)

    lookup = sub.add_parser("lookup")
    lookup.add_argument("--state", required=True)
    lookup.add_argument("--query")
    lookup.add_argument("--member-id", action="append")
    lookup.add_argument("--limit", type=int, default=10)
    lookup.add_argument("--mutual-limit", type=int, default=10)
    lookup.set_defaults(func=cmd_lookup)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
