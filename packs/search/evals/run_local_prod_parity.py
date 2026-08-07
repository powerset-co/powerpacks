#!/usr/bin/env python3
"""Compare typed local and Powerset GTM search results for equivalent scopes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packs.search.pipeline.frontier import StageResult
from packs.search.pipeline.models import (
    Backend, LocalCorpus, PersonFilters, PowersetCorpus, Profile, RoleIntent,
    SearchBounds, SearchSpec,
)
from packs.search.pipeline.search import run_search

DEFAULT_QUERY = "software engineers in sf that went to stanford"
DEFAULT_OUTPUT_ROOT = ROOT / ".powerpacks/search/local-prod-parity"
DEFAULT_OPERATORS = {
    "operator-a": {"repo": "/path/to/powerpacks-operator-a", "aliases": ["operator a", "operator-a"]},
    "operator-b": {"repo": "/path/to/powerpacks-operator-b", "aliases": ["operator b", "operator-b"]},
}


class ParityError(RuntimeError):
    pass


def now_slug() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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


def auth_token(timeout: int = 60) -> str:
    token = os.environ.get("POWERPACKS_POWERSET_TOKEN") or os.environ.get("POWERSET_TOKEN")
    if token:
        return token.removeprefix("Bearer ").strip()
    completed = subprocess.run(
        [sys.executable, str(ROOT / "packs/powerset/primitives/auth/auth.py"), "token", "--bearer-only"],
        cwd=ROOT, text=True, capture_output=True, timeout=timeout, check=False,
    )
    if completed.returncode != 0:
        raise ParityError(f"could not mint Powerset token: {(completed.stderr or completed.stdout).strip()[-500:]}")
    if not completed.stdout.strip():
        raise ParityError("auth token command returned an empty token")
    return completed.stdout.strip()


class MCPClient:
    """Read-only client retained for authenticated set discovery."""

    def __init__(self, url: str, token: str) -> None:
        self.url, self.token, self._next_id = url, token, 1

    def call_tool(self, name: str, arguments: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        self._next_id += 1
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
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
        text = content[0].get("text", "") if content and isinstance(content[0], dict) else ""
        return json.loads(text) if text else {}


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
                identifier = next((name for name in ("base_id", "person_id", "id") if name in columns), None)
                if identifier:
                    return int(conn.execute(f"select count(distinct cast({identifier} as varchar)) from {table}").fetchone()[0])
            except Exception:
                continue
        raise ParityError(f"no supported local people table found in {db}")
    finally:
        conn.close()


def parse_operator_specs(raw_specs: list[str]) -> dict[str, dict[str, Any]]:
    specs = json.loads(json.dumps(DEFAULT_OPERATORS))
    for raw in raw_specs:
        separator = "=" if "=" in raw else ":" if ":" in raw else None
        slug, repo = raw.split(separator, 1) if separator else (raw, f"/path/to/powerpacks-{raw}")
        if slug.strip():
            specs.setdefault(slug.strip(), {"aliases": [slug.strip()]})["repo"] = repo
    return specs


def parse_key_values(raw: list[str], option: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            raise ParityError(f"{option} expects slug=value, got {item!r}")
        slug, value = (part.strip() for part in item.split("=", 1))
        if not slug or not value:
            raise ParityError(f"{option} expects non-empty slug=value, got {item!r}")
        values[slug] = value
    return values


def choose_personal_set(
    sets: list[dict[str, Any]], *, slug: str, aliases: list[str], local_count: int,
    override_set_id: str | None = None,
) -> dict[str, Any]:
    if override_set_id:
        for item in sets:
            if item.get("id") == override_set_id:
                return {**item, "_selection_reason": "explicit_set_id"}
        raise ParityError(f"set override for {slug} not visible through MCP: {override_set_id}")
    candidates = [item for item in sets if item.get("is_personal")]
    if not candidates:
        raise ParityError("MCP list_sets returned no personal sets")
    aliases_lower = [alias.casefold() for alias in aliases if alias]

    def score(item: dict[str, Any]) -> tuple[int, int, int, str]:
        name, count = str(item.get("name") or "").casefold(), int(item.get("person_count") or 0)
        return (0 if any(alias in name for alias in aliases_lower) else 1, 1 if count <= 0 else 0, abs(count - local_count), name)

    selected = min(candidates, key=score)
    return {**selected, "_selection_reason": "alias_and_count" if score(selected)[0] == 0 else "closest_person_count"}


def default_intent(db: Path, query: str, *, limit: int) -> SearchSpec:
    return SearchSpec(
        "search.spec.v1", query, Profile.GTM, Backend.LOCAL, LocalCorpus(str(db)),
        role=RoleIntent(titles=("software engineer",), bm25_queries=("software engineer",)),
        person_filters=PersonFilters(metro_areas=("San Francisco Bay Area",), education_names=("Stanford University",)),
        bounds=SearchBounds(retrieval_limit=limit, output_limit=limit, semantic_rank_limit=limit),
    )


def load_intent(path: Path | None, *, db: Path, query: str, limit: int) -> SearchSpec:
    intent = SearchSpec.from_dict(json.loads(path.read_text())) if path else default_intent(db, query, limit=limit)
    if intent.profile != Profile.GTM:
        raise ParityError("local/prod parity requires a GTM SearchSpec")
    if intent.rank_mode.value != "deterministic":
        raise ParityError("local/prod parity is retrieval-only; semantic ranking is not allowed")
    return intent


def intent_dict(spec: SearchSpec) -> dict[str, Any]:
    value = spec.to_dict()
    for key in ("backend", "corpus", "sql_candidates"):
        value.pop(key, None)
    return value


def execute_search(spec: SearchSpec, *, search: Callable[[SearchSpec], StageResult] = run_search) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = search(spec)
        return {"status": "ok", "elapsed_seconds": round(time.monotonic() - started, 3), "result": result}
    except Exception as exc:
        return {"status": "failed", "elapsed_seconds": round(time.monotonic() - started, 3), "error": str(exc)}


def compare_ids(local_ids: list[str], prod_ids: list[str]) -> dict[str, Any]:
    local_set, prod_set = set(local_ids), set(prod_ids)
    overlap = local_set & prod_set
    return {
        "local_count": len(local_set), "prod_count": len(prod_set), "overlap_count": len(overlap),
        "local_precision_vs_prod": round(len(overlap) / len(local_set), 4) if local_set else (1.0 if not prod_set else 0.0),
        "local_recall_vs_prod": round(len(overlap) / len(prod_set), 4) if prod_set else (1.0 if not local_set else 0.0),
        "prod_missing_local": [value for value in prod_ids if value not in local_set],
        "local_extra": [value for value in local_ids if value not in prod_set],
    }


def compare_stage_results(local: StageResult, prod: StageResult) -> dict[str, Any]:
    comparison = compare_ids(
        [row.person_id for row in local.frontier.candidates],
        [row.person_id for row in prod.frontier.candidates],
    )
    count_keys = sorted(set(local.counts) | set(prod.counts))
    comparison.update(
        local_stage=local.stage, prod_stage=prod.stage,
        local_status=local.status, prod_status=prod.status,
        local_frontier={"input_count": local.frontier.input_count, "output_count": local.frontier.output_count, "limit": local.frontier.limit, "truncated": local.frontier.truncated},
        prod_frontier={"input_count": prod.frontier.input_count, "output_count": prod.frontier.output_count, "limit": prod.frontier.limit, "truncated": prod.frontier.truncated},
        counts={key: {"local": local.counts.get(key), "prod": prod.counts.get(key)} for key in count_keys},
        hard_filter_validation={
            "local": dict(local.hard_filter_validation), "prod": dict(prod.hard_filter_validation),
            "equal": dict(local.hard_filter_validation) == dict(prod.hard_filter_validation),
        },
    )
    return comparison


def result_payload(execution: dict[str, Any]) -> dict[str, Any]:
    result = execution.get("result")
    return {key: value for key, value in {
        "status": execution.get("status"), "elapsed_seconds": execution.get("elapsed_seconds"),
        "error": execution.get("error"), "stage_result": result.to_dict() if result else None,
    }.items() if value is not None}


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Typed Local/Powerset Search Parity", "",
        f"- Query: `{report['search_intent']['raw_request']}`", f"- Generated: `{report['generated_at']}`",
        "- Execution: canonical `run_search(SearchSpec)`; deterministic GTM only", "",
        "| Operator | Set | Local index | Powerset set | Local frontier | Powerset frontier | Overlap | Precision | Recall | Status |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in report["operators"]:
        comparison = row.get("comparison") or {}
        lines.append(
            "| {operator} | {set_name} | {local_index_count} | {set_count} | {local_count} | {prod_count} | {overlap} | {precision:.2%} | {recall:.2%} | {status} |".format(
                operator=row["operator"], set_name=(row.get("set") or {}).get("name", ""),
                local_index_count=row.get("local_index_count", 0), set_count=(row.get("set") or {}).get("person_count", 0),
                local_count=comparison.get("local_count", 0), prod_count=comparison.get("prod_count", 0),
                overlap=comparison.get("overlap_count", 0), precision=float(comparison.get("local_precision_vs_prod", 0)),
                recall=float(comparison.get("local_recall_vs_prod", 0)), status=row.get("status", ""),
            )
        )
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> dict[str, Any]:
    specs = parse_operator_specs(args.operator)
    selected_slugs = [slug.strip() for slug in args.operators.split(",") if slug.strip()]
    set_overrides = parse_key_values(args.set_id or [], "--set-id")
    operator_ids = parse_key_values(args.operator_id or [], "--operator-id")
    missing_scope = [slug for slug in selected_slugs if slug not in operator_ids]
    if missing_scope:
        raise ParityError("explicit remote scope requires --operator-id slug=operator_id for: " + ", ".join(missing_scope))
    client = MCPClient(args.mcp_url, auth_token(timeout=args.timeout))
    visible_sets = client.call_tool("list_sets", {}, timeout=args.timeout).get("sets") or []
    if not visible_sets:
        raise ParityError("MCP list_sets returned no sets")
    run_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT / now_slug()
    run_dir = run_dir if run_dir.is_absolute() else ROOT / run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    logical_intent: dict[str, Any] | None = None
    for slug in selected_slugs:
        operator = specs.get(slug)
        if not operator:
            raise ParityError(f"unknown operator slug {slug!r}")
        db = local_db_for_repo(Path(operator["repo"]).expanduser())
        if not db.exists():
            raise ParityError(f"missing local DuckDB for {slug}: {db}")
        local_count = local_person_count(db)
        selected_set = choose_personal_set(
            visible_sets, slug=slug, aliases=list(operator.get("aliases") or [slug]),
            local_count=local_count, override_set_id=set_overrides.get(slug),
        )
        intent = load_intent(Path(args.spec_json) if args.spec_json else None, db=db, query=args.query, limit=args.max_results)
        local_spec = replace(intent, backend=Backend.LOCAL, corpus=LocalCorpus(str(db)), sql_candidates=())
        remote_spec = replace(intent, backend=Backend.POWERSET, corpus=PowersetCorpus(str(selected_set["id"]), (operator_ids[slug],)), sql_candidates=())
        logical_intent = logical_intent or intent_dict(local_spec)
        if logical_intent != intent_dict(local_spec) or logical_intent != intent_dict(remote_spec):
            raise ParityError("local and Powerset SearchSpecs do not encode the same logical intent")
        local, prod = execute_search(local_spec), execute_search(remote_spec)
        row: dict[str, Any] = {
            "operator": slug, "operator_ids": [operator_ids[slug]], "db": str(db),
            "local_index_count": local_count, "set": selected_set,
            "local_spec": local_spec.to_dict(), "powerset_spec": remote_spec.to_dict(),
            "local": result_payload(local), "prod": result_payload(prod),
        }
        if local["status"] != "ok" or prod["status"] != "ok":
            row["status"] = "local_failed" if local["status"] != "ok" else "prod_failed"
        else:
            comparison = compare_stage_results(local["result"], prod["result"])
            row["comparison"] = comparison
            zero_violations = all(int(report.get("violation_count") or 0) == 0 for report in (
                comparison["hard_filter_validation"]["local"], comparison["hard_filter_validation"]["prod"],
            ))
            row["status"] = "pass" if (
                comparison["local_status"] == comparison["prod_status"]
                and comparison["local_precision_vs_prod"] >= args.min_precision
                and comparison["local_recall_vs_prod"] >= args.min_recall and zero_violations
            ) else "mismatch"
        rows.append(row)
    report = {
        "status": "ok" if rows and all(row["status"] == "pass" for row in rows) else "mismatch",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "search_intent": logical_intent or {}, "output_dir": str(run_dir),
        "thresholds": {"min_precision": args.min_precision, "min_recall": args.min_recall}, "operators": rows,
    }
    write_json(run_dir / "report.json", report)
    (run_dir / "report.md").write_text(markdown_report(report), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare equivalent typed GTM SearchSpecs on local and Powerset corpora")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Default typed intent query when --spec-json is omitted")
    parser.add_argument("--spec-json", help="Optional deterministic GTM SearchSpec; backend/corpus are replaced per side")
    parser.add_argument("--operators", default="operator-a,operator-b", help="Comma-separated operator slugs")
    parser.add_argument("--operator", action="append", default=[], help="Override local repo as slug=/path/to/repo")
    parser.add_argument("--operator-id", action="append", default=[], help="Required exact Powerset scope as slug=operator_id")
    parser.add_argument("--set-id", action="append", default=[], help="Override discovered personal set as slug=set_id")
    parser.add_argument("--mcp-url", default=os.environ.get("POWERPACKS_MCP_URL"), help="MCP URL used only for authenticated set discovery")
    parser.add_argument("--output-dir")
    parser.add_argument("--max-results", type=int, default=1000)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--min-precision", type=float, default=0.95)
    parser.add_argument("--min-recall", type=float, default=0.95)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.mcp_url:
        raise SystemExit("missing required Powerset MCP config: set POWERPACKS_MCP_URL or pass --mcp-url")
    try:
        report = run(args)
        print(json.dumps({"status": report["status"], "output_dir": report["output_dir"]}, indent=2, sort_keys=True))
        raise SystemExit(0 if report["status"] == "ok" else 2)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2, sort_keys=True))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
