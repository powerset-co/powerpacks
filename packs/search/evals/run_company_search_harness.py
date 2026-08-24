#!/usr/bin/env python3
"""Validate typed GTM company-source cases and optionally resolve them live."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from packs.search.backends.turbopuffer.runner import TurboPufferSearchRunner
from packs.search.pipeline.models import (
    Backend, CompanyFilters, EvidenceCriterion, PowersetCorpus, Profile,
    RankMode, SearchSpec,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals/company-search/cases.json"
REPORT_PATH = ROOT / "evals/company_search.md"
COMPANY_FIELDS = set(CompanyFilters.__dataclass_fields__)
SEMANTIC_FIELD = "company_semantic_queries"
LEGACY_POLICY_FIELDS = {"company_sector_strategy", "company_sector_min_results"}


@dataclass(frozen=True)
class CompanyCase:
    id: str
    query: str
    payload: dict[str, Any]
    expected: dict[str, Any]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_cases(path: Path) -> list[CompanyCase]:
    return [
        CompanyCase(str(item["id"]), str(item["query"]), dict(item["payload"]), dict(item.get("expected") or {}))
        for item in json.loads(path.read_text())
    ]


def build_spec(
    case: CompanyCase, *, set_id: str, operator_ids: tuple[str, ...],
) -> tuple[SearchSpec, tuple[str, ...]]:
    payload = case.payload
    company_values = {name: payload[name] for name in COMPANY_FIELDS if name in payload}
    semantic_queries = tuple(str(value).strip() for value in payload.get(SEMANTIC_FIELD) or () if str(value).strip())
    unsupported = tuple(sorted(set(payload) - COMPANY_FIELDS - {SEMANTIC_FIELD} - LEGACY_POLICY_FIELDS))
    criteria = tuple(
        EvidenceCriterion(f"company_archetype_{index}", query)
        for index, query in enumerate(semantic_queries, start=1)
    )
    return SearchSpec(
        "search.spec.v1", case.query, Profile.GTM, Backend.POWERSET,
        PowersetCorpus(set_id, operator_ids),
        company_filters=CompanyFilters(**company_values),
        soft_criteria=criteria,
        rank_mode=RankMode.SEMANTIC if criteria else RankMode.DETERMINISTIC,
    ), unsupported


def source_constraints(spec: SearchSpec) -> tuple[str, ...]:
    return tuple(
        name for name, value in asdict(spec.company_filters).items()
        if value not in (None, (), [], "")
    )


def classify_case(
    spec: SearchSpec, unsupported_fields: tuple[str, ...], supported: set[str],
) -> tuple[str, list[str]]:
    errors: list[str] = []
    constraints = source_constraints(spec)
    unsupported_hard = tuple(name for name in constraints if name not in supported and name != "company_names")
    if unsupported_fields:
        errors.append("unrepresentable required fields: " + ", ".join(unsupported_fields))
    if unsupported_hard:
        errors.append("runner does not support required company filters: " + ", ".join(unsupported_hard))
    if spec.soft_criteria and not constraints:
        errors.append("semantic-only company source resolution is unsupported by typed SearchSpec")
    return ("unsupported", errors) if errors else ("pass", [])


def capability_payload(capabilities: Any) -> dict[str, Any]:
    value = asdict(capabilities)
    value["backend"] = capabilities.backend.value
    return value


def dry_run_case(case: CompanyCase) -> dict[str, Any]:
    spec, unsupported_fields = build_spec(case, set_id="dry-run-set", operator_ids=("dry-run-operator",))
    runner = TurboPufferSearchRunner(spec.corpus)
    capabilities = runner.capabilities(spec)
    status, errors = classify_case(spec, unsupported_fields, set(capabilities.supported_hard_filters))
    return {
        "id": case.id, "query": case.query, "status": status, "mode": "dry_run",
        "search_spec": spec.to_dict(), "capabilities": capability_payload(capabilities),
        "source_constraints": list(source_constraints(spec)),
        "semantic_criteria_count": len(spec.soft_criteria),
        "semantic_resolution": "rank_only_not_applied_to_source_resolution" if spec.soft_criteria else "not_requested",
        "unsupported_fields": list(unsupported_fields), "errors": errors,
    }


def live_case(case: CompanyCase, *, set_id: str, operator_ids: tuple[str, ...]) -> dict[str, Any]:
    spec, unsupported_fields = build_spec(case, set_id=set_id, operator_ids=operator_ids)
    runner = TurboPufferSearchRunner(spec.corpus)
    capabilities = runner.capabilities(spec)
    status, errors = classify_case(spec, unsupported_fields, set(capabilities.supported_hard_filters))
    base = {
        "id": case.id, "query": case.query, "status": status, "mode": "live",
        "search_spec": spec.to_dict(), "source_constraints": list(source_constraints(spec)),
        "semantic_criteria_count": len(spec.soft_criteria), "errors": errors,
        "semantic_resolution": "rank_only_not_applied_to_source_resolution" if spec.soft_criteria else "not_requested",
    }
    if status == "unsupported":
        return base
    sources = runner.resolve_sources(spec)
    unresolved = list(sources.unresolved_required_inputs)
    return {
        **base,
        "status": "pass" if not unresolved else "fail",
        "resolved_count": len(sources.company_ids),
        "resolved_sources": [dict(record) for record in sources.records],
        "investor_urns": list(sources.investor_urns),
        "errors": ["required sources unresolved: " + ", ".join(unresolved)] if unresolved else [],
    }


def write_report(results: list[dict[str, Any]], *, mode: str, cases_path: Path) -> None:
    lines = [
        "# Typed Company Source Harness", "", f"Last run: `{now_iso()}`",
        f"Mode: `{mode}`", f"Cases: `{cases_path}`", "",
        "| Case | Status | Structured constraints | Semantic criteria | Resolved | Notes |",
        "|---|---|---|---:|---:|---|",
    ]
    for row in results:
        lines.append(
            "| {id} | {status} | {constraints} | {semantic} | {resolved} | {notes} |".format(
                id=row["id"], status=row["status"],
                constraints=", ".join(row.get("source_constraints") or []),
                semantic=row.get("semantic_criteria_count", ""), resolved=row.get("resolved_count", ""),
                notes="; ".join(row.get("errors") or []).replace("|", "\\|"),
            )
        )
    REPORT_PATH.write_text("\n".join(lines) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate typed GTM company source-resolution cases")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--case-glob")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--live", action="store_true", help="Call TurboPufferSearchRunner.resolve_sources for supported cases")
    parser.add_argument("--set-id", help="Required exact Powerset set scope for --live")
    parser.add_argument("--operator-id", action="append", default=[], help="Required exact Powerset operator ID; repeat as needed")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.live and (not args.set_id or not args.operator_id):
        raise SystemExit("--live requires --set-id and at least one --operator-id for exact remote scope")
    cases_path = Path(args.cases)
    cases = load_cases(cases_path)
    if args.case_glob:
        pattern = re.compile(args.case_glob)
        cases = [case for case in cases if pattern.search(case.id) or pattern.search(case.query)]
    if args.max_cases:
        cases = cases[: args.max_cases]
    results: list[dict[str, Any]] = []
    for case in cases:
        try:
            result = live_case(case, set_id=args.set_id, operator_ids=tuple(args.operator_id)) if args.live else dry_run_case(case)
        except Exception as exc:
            result = {
                "id": case.id, "query": case.query, "status": "fail",
                "mode": "live" if args.live else "dry_run", "errors": [str(exc)],
            }
        results.append(result)
    write_report(results, mode="live" if args.live else "dry_run", cases_path=cases_path)
    print(json.dumps({"report": str(REPORT_PATH), "results": results}, indent=2, sort_keys=True))
    if any(row["status"] == "fail" for row in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
