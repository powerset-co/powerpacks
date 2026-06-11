#!/usr/bin/env python3
"""Compare local DuckDB search recall against prod Powerset MCP search.

This harness is for local/prod retrieval parity. It intentionally skips LLM
reranking and compares aggregate candidate identity overlap, since local
brute-force cosine and prod TurboPuffer may order the same frontier differently.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_QUERY = "software engineers in sf that went to stanford"
DEFAULT_PAYLOAD = ROOT / "packs/search/evals/cases/stanford_sf_engineers.payload.json"
DEFAULT_OUTPUT_ROOT = ROOT / ".powerpacks/search/local-prod-parity"
DEFAULT_MCP_URL = "https://search-api-7wk4uhe77q-uw.a.run.app/mcp/"
DEFAULT_OPERATORS = {
    "arthur": {
        "repo": "/Users/arthur/workspace/powerpacks-arthur",
        "aliases": ["personal connections", "arthur chen", "arthur c"],
    },
    "jake": {
        "repo": "/Users/arthur/workspace/powerpacks-jake",
        "aliases": ["jake zeller", "jake"],
    },
    "jonathan": {
        "repo": "/Users/arthur/workspace/powerpacks-jonathan",
        "aliases": ["jonathan swanson", "jonathan"],
    },
    "patrick": {
        "repo": "/Users/arthur/workspace/powerpacks-patrick",
        "aliases": ["patrick devivo", "patrick"],
    },
}
FILTER_KEY_MAP = {
    "semantic_query": "role_semantic_query",
    "bm25_queries": "role_bm25_queries",
}
PROD_DROP_FILTER_KEYS = {
    "school_names",
    "role_core_patterns",
    "role_adjacent_patterns",
}


class ParityError(RuntimeError):
    pass


def now_slug() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y%m%dT%H%M%SZ")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


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
            value, end = decoder.raw_decode(raw, index)
            out.append(value)
            index = end
        except json.JSONDecodeError:
            next_start = raw.find("{", index + 1)
            if next_start < 0:
                break
            index = next_start
    return out


def parse_sse_json(raw: str) -> dict[str, Any]:
    payloads: list[str] = []
    current: list[str] = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            current.append(line[5:].strip())
        elif not line.strip() and current:
            payloads.append("\n".join(current))
            current = []
    if current:
        payloads.append("\n".join(current))
    if not payloads:
        raise ParityError(f"MCP response did not contain JSON-RPC data: {raw[:500]}")
    return json.loads(payloads[-1])


def run_cmd(args: list[str], *, timeout: int, cwd: Path = ROOT, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=False)


def auth_token(timeout: int = 60) -> str:
    env_token = os.environ.get("POWERPACKS_POWERSET_TOKEN") or os.environ.get("POWERSET_TOKEN")
    if env_token:
        return env_token.removeprefix("Bearer ").strip()
    proc = run_cmd(
        [
            sys.executable,
            str(ROOT / "packs/powerset/primitives/auth/auth.py"),
            "token",
            "--bearer-only",
        ],
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise ParityError(f"could not mint Powerset token: {(proc.stderr or proc.stdout).strip()[-500:]}")
    token = proc.stdout.strip()
    if not token:
        raise ParityError("auth token command returned an empty token")
    return token


class MCPClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self._next_id = 1

    def call_tool(self, name: str, arguments: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        self._next_id += 1
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ParityError(f"MCP {name} failed HTTP {exc.code}: {body[:1000]}") from exc
        outer = parse_sse_json(raw)
        if outer.get("error"):
            raise ParityError(f"MCP {name} JSON-RPC error: {outer['error']}")
        result = outer.get("result") or {}
        if isinstance(result.get("structuredContent"), dict) and result["structuredContent"]:
            return result["structuredContent"]
        content = result.get("content") or []
        if not content:
            return {}
        text = content[0].get("text", "") if isinstance(content[0], dict) else ""
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ParityError(f"MCP {name} returned non-JSON text: {text[:500]}") from exc
        return parsed


def local_db_for_repo(repo: Path) -> Path:
    return repo / ".powerpacks/search-index/local-search.duckdb"


def local_person_count(db: Path) -> int:
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as exc:
        raise ParityError("duckdb package is required to count local rows") from exc
    conn = duckdb.connect(str(db), read_only=True)
    try:
        for table in ("local_people_positions", "local_people", "local_summaries"):
            try:
                columns = {row[1] for row in conn.execute(f"pragma table_info('{table}')").fetchall()}
                id_column = next((column for column in ("base_id", "person_id", "id") if column in columns), None)
                if not id_column:
                    continue
                return int(conn.execute(f"select count(distinct cast({id_column} as varchar)) from {table}").fetchone()[0])
            except Exception:
                continue
        raise ParityError(f"no supported local people table found in {db}")
    finally:
        conn.close()


def parse_operator_specs(raw_specs: list[str]) -> dict[str, dict[str, Any]]:
    specs = json.loads(json.dumps(DEFAULT_OPERATORS))
    for raw in raw_specs:
        if "=" in raw:
            slug, repo = raw.split("=", 1)
        elif ":" in raw:
            slug, repo = raw.split(":", 1)
        else:
            slug, repo = raw, f"/Users/arthur/workspace/powerpacks-{raw}"
        slug = slug.strip()
        if not slug:
            continue
        specs.setdefault(slug, {"aliases": [slug]})
        specs[slug]["repo"] = repo
    return specs


def parse_set_overrides(raw: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            raise ParityError(f"--set-id expects slug=set_id, got {item!r}")
        slug, set_id = item.split("=", 1)
        out[slug.strip()] = set_id.strip()
    return out


def choose_personal_set(
    sets: list[dict[str, Any]],
    *,
    slug: str,
    aliases: list[str],
    local_count: int,
    override_set_id: str | None = None,
) -> dict[str, Any]:
    if override_set_id:
        for item in sets:
            if item.get("id") == override_set_id:
                return {**item, "_selection_reason": "explicit_set_id"}
        raise ParityError(f"set override for {slug} not visible through MCP: {override_set_id}")

    lowered_aliases = [alias.lower() for alias in aliases if alias]
    candidates = [item for item in sets if item.get("is_personal")]
    if not candidates:
        raise ParityError("MCP list_sets returned no personal sets")

    def score(item: dict[str, Any]) -> tuple[int, int, int, str]:
        name = str(item.get("name") or "").lower()
        person_count = int(item.get("person_count") or 0)
        alias_miss = 0 if any(alias in name for alias in lowered_aliases) else 1
        zero_penalty = 1 if person_count <= 0 else 0
        return (alias_miss, zero_penalty, abs(person_count - local_count), name)

    selected = min(candidates, key=score)
    reason = "alias_and_count" if score(selected)[0] == 0 else "closest_person_count"
    return {**selected, "_selection_reason": reason}


def role_filters(payload: dict[str, Any]) -> dict[str, Any]:
    filters = payload.get("role_search_filters")
    if not isinstance(filters, dict):
        raise ParityError("payload must contain role_search_filters")
    return filters


def list_ids(values: Any) -> list[str]:
    out: list[str] = []
    for value in values or []:
        if isinstance(value, dict):
            value = value.get("id")
        if value:
            out.append(str(value))
    return list(dict.fromkeys(out))


def extract_prod_education_ids(expanded: dict[str, Any], education_names: list[str]) -> list[str]:
    filters = expanded.get("filters") or {}
    ids = list_ids(filters.get("education_ids"))
    if ids:
        return ids
    return []


def compact_school_name(value: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", value).strip() or value


def entity_ids(values: Any) -> list[str]:
    return list_ids(values)


def entity_display_values(values: Any) -> list[str]:
    out: list[str] = []
    for value in values or []:
        if isinstance(value, dict):
            raw = value.get("display_value") or value.get("id")
        else:
            raw = value
        if raw:
            out.append(str(raw))
    return list(dict.fromkeys(out))


def clean_prod_expanded_filters(filters: dict[str, Any]) -> dict[str, Any]:
    prod: dict[str, Any] = {}
    for key, value in filters.items():
        if value in (None, "", [], {}):
            continue
        if key in PROD_DROP_FILTER_KEYS:
            continue
        if isinstance(value, list) and value and isinstance(value[0], dict):
            prod[key] = entity_ids(value)
        else:
            prod[key] = value
    return prod


def prod_expansion_to_local_payload(expanded: dict[str, Any], *, fallback_payload: dict[str, Any], query: str) -> dict[str, Any]:
    filters = expanded.get("filters") or {}
    fallback_filters = role_filters(fallback_payload)
    local: dict[str, Any] = {}
    for key, value in filters.items():
        if value in (None, "", [], {}):
            continue
        if key in PROD_DROP_FILTER_KEYS:
            continue
        if key == "role_semantic_query":
            local["semantic_query"] = value
        elif key == "role_bm25_queries":
            local["bm25_queries"] = value
        elif key == "education_ids":
            fallback_names = [str(name) for name in fallback_filters.get("education_names") or [] if str(name).strip()]
            names = fallback_names or [compact_school_name(name) for name in entity_display_values(value)]
            if names:
                local["education_names"] = list(dict.fromkeys(names))
        elif key == "company_ids":
            # Prod company ids are harmonic URNs that do not exist in the
            # local index. Hand local the display names so the local pipeline
            # resolves them against local_companies itself.
            names = entity_display_values(value)
            if names:
                local["company_names"] = list(dict.fromkeys(names))
        elif key == "investors":
            names = entity_display_values(value)
            if names:
                local["investor_names"] = list(dict.fromkeys(names))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            local[key] = entity_ids(value)
        else:
            local[key] = value
    return {
        "intent_type": "role_search",
        "source_type": "prod_mcp_expand_query",
        "normalized_query": expanded.get("original_query") or query,
        "vertical": "people",
        "traits": expanded.get("traits") or fallback_payload.get("traits") or [],
        "role_search_filters": {key: value for key, value in local.items() if value not in (None, "", [], {})},
        "notes": [
            "Generated from prod MCP expand_query for local/prod retrieval parity.",
            "No LLM reranking. Local DuckDB retrieval/hydration only.",
        ],
    }


def local_filters_to_prod_filters(
    local_filters: dict[str, Any],
    *,
    prod_expansion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prod: dict[str, Any] = {}
    for key, value in local_filters.items():
        if value in (None, "", [], {}):
            continue
        if key in PROD_DROP_FILTER_KEYS:
            continue
        if key == "education_names":
            continue
        prod[FILTER_KEY_MAP.get(key, key)] = value

    education_names = [str(name) for name in local_filters.get("education_names") or [] if str(name).strip()]
    if education_names and not prod.get("education_ids"):
        education_ids = extract_prod_education_ids(prod_expansion or {}, education_names)
        if education_ids:
            prod["education_ids"] = education_ids
            prod.setdefault("education_op", local_filters.get("education_op") or "or")
        else:
            raise ParityError(f"could not resolve prod education IDs for: {', '.join(education_names)}")

    return prod


def run_local_search(
    *,
    slug: str,
    db: Path,
    query: str,
    payload_json: Path,
    run_dir: Path,
    limit: int,
    top_k: int,
    timeout: int,
) -> dict[str, Any]:
    ledger = run_dir / slug / "local-search.pipeline.json"
    cmd = [
        sys.executable,
        str(ROOT / "packs/search/primitives/local_search_pipeline/local_search_pipeline.py"),
        "run",
        "--db",
        str(db),
        "--ledger",
        str(ledger),
        "--query",
        query,
        "--payload-json",
        str(payload_json),
        "--limit",
        str(limit),
        "--top-k",
        str(top_k),
        "--timeout",
        str(timeout),
        "--force",
    ]
    started = time.monotonic()
    proc = run_cmd(cmd, timeout=timeout + 60)
    elapsed = round(time.monotonic() - started, 3)
    log_path = run_dir / slug / "local-search.log"
    write_text(
        log_path,
        "$ " + " ".join(cmd) + "\n\nSTDOUT:\n" + proc.stdout + "\nSTDERR:\n" + proc.stderr,
    )
    parsed = parse_jsons(proc.stdout or "")
    output = parsed[-1] if parsed else {}
    if proc.returncode != 0 or output.get("status") == "failed":
        return {
            "status": "failed",
            "elapsed_seconds": elapsed,
            "error": output.get("error") or (proc.stderr or proc.stdout).strip()[-1000:],
            "log": str(log_path),
        }
    retrieval_path = output.get("artifacts", {}).get("retrieval_artifact")
    candidates: list[dict[str, Any]] = []
    if retrieval_path:
        path = Path(retrieval_path)
        if not path.is_absolute():
            path = ROOT / path
        retrieval = read_json(path)
        candidates = retrieval.get("candidates") or []
    ids = [str(row.get("person_id")) for row in candidates if row.get("person_id")]
    return {
        "status": "ok",
        "elapsed_seconds": elapsed,
        "summary": output.get("summary") or {},
        "artifacts": output.get("artifacts") or {},
        "ids": list(dict.fromkeys(ids)),
        "results": candidates,
        "log": str(log_path),
    }


def run_prod_search(
    client: MCPClient,
    *,
    query: str,
    set_id: str,
    filters: dict[str, Any],
    max_results: int,
    page_size: int,
    persist: bool,
    timeout: int,
) -> dict[str, Any]:
    started = time.monotonic()
    first = client.call_tool(
        "search",
        {
            "query": query,
            "set_id": set_id,
            "filters": filters,
            "rerank": False,
            "max_results": max_results,
            "page_size": min(page_size, max_results),
            "persist": persist,
        },
        timeout=timeout,
    )
    if first.get("error"):
        return {"status": "failed", "elapsed_seconds": round(time.monotonic() - started, 3), "error": first["error"]}

    page = first.get("page") or {}
    results = list(page.get("results") or [])
    search_id = first.get("search_id") or page.get("search_id")
    conversation_id = first.get("conversation_id")
    returned_by_search = int(first.get("returned_by_search") or page.get("total_returned_by_search") or len(results))
    target = min(returned_by_search, max_results)
    offset = len(results)
    while (search_id or conversation_id) and offset < target:
        query_args: dict[str, Any] = {
            "offset": offset,
            "limit": min(page_size, target - offset),
        }
        if conversation_id:
            query_args["conversation_id"] = conversation_id
        else:
            query_args["search_id"] = search_id
        chunk = client.call_tool(
            "query_results",
            query_args,
            timeout=timeout,
        )
        if chunk.get("total_returned_by_search") is not None:
            target = min(target, int(chunk.get("total_returned_by_search") or target), max_results)
        if chunk.get("error"):
            return {
                "status": "failed",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "error": chunk["error"],
                "partial_results": results,
            }
        rows = list(chunk.get("results") or [])
        if not rows:
            break
        results.extend(rows)
        offset += len(rows)

    ids = [str(row.get("person_id")) for row in results if row.get("person_id")]
    return {
        "status": "ok",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "total_count": first.get("total_count"),
        "returned_by_search": returned_by_search,
        "search_id": search_id,
        "conversation_id": conversation_id,
        "persisted": persist,
        "ids": list(dict.fromkeys(ids)),
        "results": results,
    }


def compare_ids(local_ids: list[str], prod_ids: list[str]) -> dict[str, Any]:
    local_set = set(local_ids)
    prod_set = set(prod_ids)
    overlap = [person_id for person_id in local_ids if person_id in prod_set]
    prod_missing_local = [person_id for person_id in prod_ids if person_id not in local_set]
    local_extra = [person_id for person_id in local_ids if person_id not in prod_set]
    overlap_count = len(set(overlap))
    local_count = len(local_set)
    prod_count = len(prod_set)
    return {
        "local_count": local_count,
        "prod_count": prod_count,
        "overlap_count": overlap_count,
        "local_precision_vs_prod": round(overlap_count / local_count, 4) if local_count else (1.0 if not prod_count else 0.0),
        "local_recall_vs_prod": round(overlap_count / prod_count, 4) if prod_count else (1.0 if not local_count else 0.0),
        "prod_missing_local": prod_missing_local,
        "local_extra": local_extra,
    }


def result_names(rows: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows:
        person_id = str(row.get("person_id") or "")
        if not person_id:
            continue
        label = row.get("name") or row.get("headline") or row.get("current_title") or row.get("position_title") or person_id
        out[person_id] = str(label)
    return out


def sample_labels(ids: list[str], names: dict[str, str], limit: int = 5) -> list[str]:
    return [f"{person_id} ({names.get(person_id, '').strip()})".rstrip() for person_id in ids[:limit]]


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Local/Prod Search Parity",
        "",
        f"- Query: `{report['query']}`",
        f"- Filter source: `{report['filter_source']}`",
        f"- Input payload: `{report['payload_json']}`",
        f"- Local payload: `{report['local_payload_json']}`",
        f"- Generated: `{report['generated_at']}`",
        f"- Prod rerank: `false`",
        f"- Prod persist: `{str(report.get('prod_persist')).lower()}`",
    ]
    if report.get("prod_mcp_unforwarded_filter_keys"):
        lines.append(f"- Known prod MCP forwarding gaps for this payload: `{', '.join(report['prod_mcp_unforwarded_filter_keys'])}`")
    lines.extend([
        "",
        "| Operator | Set | Local index | Prod set | Local results | Prod results | Overlap | Precision | Recall | Status |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ])
    for row in report["operators"]:
        cmp = row.get("comparison") or {}
        local_count = cmp.get("local_count")
        if local_count is None:
            local_count = len(((row.get("local") or {}).get("ids") or []))
        prod_count = cmp.get("prod_count")
        if prod_count is None:
            prod_count = len(((row.get("prod") or {}).get("ids") or (row.get("prod") or {}).get("partial_results") or []))
        lines.append(
            "| {operator} | {set_name} | {local_index_count} | {prod_set_count} | {local_count} | {prod_count} | {overlap_count} | {precision:.2%} | {recall:.2%} | {status} |".format(
                operator=row["operator"],
                set_name=(row.get("set") or {}).get("name", ""),
                local_index_count=row.get("local_index_count", 0),
                prod_set_count=(row.get("set") or {}).get("person_count", 0),
                local_count=local_count,
                prod_count=prod_count,
                overlap_count=cmp.get("overlap_count", 0),
                precision=float(cmp.get("local_precision_vs_prod", 0.0)),
                recall=float(cmp.get("local_recall_vs_prod", 0.0)),
                status=row.get("status", ""),
            )
        )
    lines.append("")
    for row in report["operators"]:
        cmp = row.get("comparison") or {}
        if not cmp:
            continue
        prod_names = result_names((row.get("prod") or {}).get("results") or [])
        local_names = result_names((row.get("local") or {}).get("results") or [])
        lines.append(f"## {row['operator']}")
        lines.append("")
        lines.append(f"- Set ID: `{(row.get('set') or {}).get('id', '')}`")
        lines.append(f"- Set selection: `{(row.get('set') or {}).get('_selection_reason', '')}`")
        lines.append(f"- Missing locally from prod sample: {sample_labels(cmp.get('prod_missing_local') or [], prod_names) or []}")
        lines.append(f"- Local-only sample: {sample_labels(cmp.get('local_extra') or [], local_names) or []}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run(args: argparse.Namespace) -> dict[str, Any]:
    payload_path = Path(args.payload_json).resolve()
    payload = read_json(payload_path)
    local_filters = role_filters(payload)
    specs = parse_operator_specs(args.operator)
    selected_slugs = args.operators.split(",") if args.operators else list(DEFAULT_OPERATORS.keys())
    set_overrides = parse_set_overrides(args.set_id or [])

    client = MCPClient(args.mcp_url, auth_token(timeout=args.timeout))
    sets_payload = client.call_tool("list_sets", {}, timeout=args.timeout)
    visible_sets = sets_payload.get("sets") or []
    if not visible_sets:
        raise ParityError("MCP list_sets returned no sets")

    run_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT / now_slug()
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    first_set_id = None
    operator_rows: list[dict[str, Any]] = []
    for slug in selected_slugs:
        slug = slug.strip()
        if not slug:
            continue
        spec = specs.get(slug)
        if not spec:
            raise ParityError(f"unknown operator slug {slug!r}")
        repo = Path(spec["repo"]).expanduser()
        db = local_db_for_repo(repo)
        if not db.exists():
            raise ParityError(f"missing local DuckDB for {slug}: {db}")
        count = local_person_count(db)
        selected_set = choose_personal_set(
            visible_sets,
            slug=slug,
            aliases=list(spec.get("aliases") or [slug]),
            local_count=count,
            override_set_id=set_overrides.get(slug),
        )
        first_set_id = first_set_id or selected_set["id"]
        operator_rows.append({
            "operator": slug,
            "repo": str(repo),
            "db": str(db),
            "local_index_count": count,
            "set": selected_set,
        })

    prod_expansion = None
    if args.filter_source == "prod-expand" or args.resolve_prod_education:
        prod_expansion = client.call_tool("expand_query", {"query": args.query, "set_id": first_set_id}, timeout=args.timeout)
        write_json(run_dir / "prod-expand-query.json", prod_expansion)

    local_payload_path = payload_path
    if args.filter_source == "prod-expand":
        if not prod_expansion:
            raise ParityError("prod expansion was not available")
        local_payload = prod_expansion_to_local_payload(prod_expansion, fallback_payload=payload, query=args.query)
        local_payload_path = run_dir / "local-payload.from-prod-expand.json"
        write_json(local_payload_path, local_payload)
        prod_filters = clean_prod_expanded_filters(prod_expansion.get("filters") or {})
    else:
        prod_filters = local_filters_to_prod_filters(local_filters, prod_expansion=prod_expansion)
    source_traits = (prod_expansion or {}).get("traits") if isinstance(prod_expansion, dict) else payload.get("traits")
    if isinstance(source_traits, list) and source_traits:
        prod_filters.setdefault("traits", source_traits)
    write_json(run_dir / "prod-filters.json", prod_filters)
    prod_mcp_forwarded_keys = {
        "company_ids",
        "company_semantic_queries",
        "role_ids",
        "education_ids",
        "education_op",
        "degree_levels",
        "fields_of_study",
        "graduation_year_min",
        "graduation_year_max",
        "cities",
        "states",
        "countries",
        "metro_areas",
        "macro_regions",
        "company_cities",
        "company_states",
        "company_countries",
        "company_metro_areas",
        "company_macro_regions",
        "sector_types",
        "entity_types",
        "funding_stage_min",
        "funding_stage_max",
        "funding_amount_min",
        "funding_amount_max",
        "headcount_min",
        "headcount_max",
        "last_funding_before",
        "last_funding_after",
        "valuation_min",
        "valuation_max",
        "founded_year_min",
        "founded_year_max",
        "investors",
        "yc_batches",
        "technology_types",
        "customer_types",
        "seniority_bands",
        "seniority_intent",
        "role_function",
        "role_tracks",
        "adjacent_role_ids",
        "adjacent_departments",
        "adjacent_seniority",
        "years_experience_min",
        "years_experience_max",
        "age_min",
        "age_max",
        "x_followers_min",
        "x_followers_max",
        "li_followers_min",
        "li_followers_max",
        "li_connections_min",
        "li_connections_max",
        "ig_followers_min",
        "ig_followers_max",
        "operator_interaction_min",
        "operator_interaction_max",
        "set_interaction_min",
        "set_interaction_max",
        "tech_skills",
        "position_after_date",
        "position_before_date",
        "role_semantic_query",
        "role_bm25_queries",
        "role_core_patterns",
        "role_adjacent_patterns",
        "company_adjacency_queries",
        "has_domain_intent",
        "traits",
    }
    prod_mcp_unforwarded_filter_keys = sorted(
        key for key, value in prod_filters.items()
        if value not in (None, "", [], {}) and key not in prod_mcp_forwarded_keys
    )

    for row in operator_rows:
        local = run_local_search(
            slug=row["operator"],
            db=Path(row["db"]),
            query=args.query,
            payload_json=local_payload_path,
            run_dir=run_dir,
            limit=args.max_results,
            top_k=args.top_k,
            timeout=args.local_timeout,
        )
        row["local"] = local
        if local.get("status") != "ok":
            row["status"] = "local_failed"
            continue

        prod = run_prod_search(
            client,
            query=args.query,
            set_id=row["set"]["id"],
            filters=prod_filters,
            max_results=args.max_results,
            page_size=args.page_size,
            persist=args.persist_prod,
            timeout=args.timeout,
        )
        row["prod"] = prod
        if prod.get("status") != "ok":
            row["status"] = "prod_failed"
            continue

        comparison = compare_ids(local.get("ids") or [], prod.get("ids") or [])
        row["comparison"] = comparison
        row["status"] = (
            "pass"
            if comparison["local_precision_vs_prod"] >= args.min_precision and comparison["local_recall_vs_prod"] >= args.min_recall
            else "mismatch"
        )

    report = {
        "status": "ok" if all(row.get("status") == "pass" for row in operator_rows) else "mismatch",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "query": args.query,
        "payload_json": str(payload_path),
        "local_payload_json": str(local_payload_path),
        "filter_source": args.filter_source,
        "output_dir": str(run_dir),
        "prod_filters": prod_filters,
        "thresholds": {"min_precision": args.min_precision, "min_recall": args.min_recall},
        "prod_persist": bool(args.persist_prod),
        "prod_mcp_unforwarded_filter_keys": prod_mcp_unforwarded_filter_keys,
        "operators": operator_rows,
    }
    write_json(run_dir / "report.json", report)
    write_text(run_dir / "report.md", markdown_report(report))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare local DuckDB search to prod Powerset MCP search")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--payload-json", default=str(DEFAULT_PAYLOAD))
    parser.add_argument("--filter-source", choices=["prod-expand", "payload"], default="payload",
                        help="Use prod MCP expand_query as the canonical filter source, or use --payload-json")
    parser.add_argument("--operators", default="arthur,jake,jonathan,patrick", help="Comma-separated operator slugs")
    parser.add_argument("--operator", action="append", default=[], help="Override operator repo as slug=/path/to/repo")
    parser.add_argument("--set-id", action="append", default=[], help="Override prod set as slug=set_id")
    parser.add_argument("--mcp-url", default=os.environ.get("POWERPACKS_MCP_URL", DEFAULT_MCP_URL))
    parser.add_argument("--output-dir")
    parser.add_argument("--max-results", type=int, default=1000)
    parser.add_argument("--top-k", type=int, default=1000)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--persist-prod", action=argparse.BooleanOptionalAction, default=True,
                        help="Persist prod MCP search results so pagination can use conversation_id across Cloud Run instances")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--local-timeout", type=int, default=900)
    parser.add_argument("--min-precision", type=float, default=0.95)
    parser.add_argument("--min-recall", type=float, default=0.95)
    parser.add_argument("--resolve-prod-education", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    try:
        report = run(args)
        print(json.dumps({
            "status": report["status"],
            "query": report["query"],
            "output_dir": report["output_dir"],
            "operators": [
                {
                    "operator": row["operator"],
                    "status": row.get("status"),
                    "set_id": (row.get("set") or {}).get("id"),
                    "set_name": (row.get("set") or {}).get("name"),
                    "local_results": (row.get("comparison") or {}).get("local_count", len(((row.get("local") or {}).get("ids") or []))),
                    "prod_results": (row.get("comparison") or {}).get("prod_count", len(((row.get("prod") or {}).get("ids") or (row.get("prod") or {}).get("partial_results") or []))),
                    "overlap": (row.get("comparison") or {}).get("overlap_count"),
                    "precision": (row.get("comparison") or {}).get("local_precision_vs_prod"),
                    "recall": (row.get("comparison") or {}).get("local_recall_vs_prod"),
                    "error": (row.get("local") or {}).get("error") or (row.get("prod") or {}).get("error"),
                }
                for row in report["operators"]
            ],
        }, indent=2, sort_keys=True))
        raise SystemExit(0 if report["status"] == "ok" else 2)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2, sort_keys=True))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
