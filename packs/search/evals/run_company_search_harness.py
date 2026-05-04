#!/usr/bin/env python3
"""Run search-company contract cases.

Dry-run mode validates payload shape, resolver planning, alias expansion, and
filter construction without hitting TurboPuffer. Live mode also invokes
resolve_investors/resolve_companies and records results in a task state file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
PRIMITIVES = ROOT / "primitives"
TASK_STATE = PRIMITIVES / "task_state" / "task_state.py"
DEFAULT_CASES = ROOT / "evals" / "company-search" / "cases.json"
DEFAULT_APP_DIR = Path("/Users/arthur/workspace/aleph-mvp")
REPORT_PATH = ROOT / "evals" / "company_search.md"

sys.path.insert(0, str(PRIMITIVES / "lib"))
sys.path.insert(0, str(PRIMITIVES / "resolve_companies"))
import resolve_companies  # noqa: E402


@dataclass
class CompanyCase:
    id: str
    query: str
    payload: dict[str, Any]
    expected: dict[str, Any]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_cases(path: Path) -> list[CompanyCase]:
    raw = json.loads(path.read_text())
    return [
        CompanyCase(
            id=str(item["id"]),
            query=str(item["query"]),
            payload=dict(item["payload"]),
            expected=dict(item.get("expected") or {}),
        )
        for item in raw
    ]


def sh(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)
    log_path.write_text(
        "$ " + " ".join(args) + "\n\n"
        + "STDOUT:\n" + completed.stdout
        + "\nSTDERR:\n" + completed.stderr
    )
    if completed.returncode != 0:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(args)}\nsee {log_path}")
    return completed


def planned_steps_for(payload: dict[str, Any]) -> list[str]:
    steps = []
    if payload.get("investor_names"):
        steps.append("resolve_investors")
    steps.append("resolve_companies")
    return steps


def dry_run_case(case: CompanyCase) -> dict[str, Any]:
    payload = dict(case.payload)
    names = resolve_companies.expanded_company_names([str(name) for name in payload.get("company_names") or []])
    filters = resolve_companies.company_attribute_filters(payload)
    hard_filters = resolve_companies.company_attribute_filters(payload, include_soft=False)
    soft_filters = resolve_companies.company_attribute_filters(payload, only_soft=True)
    strategy = resolve_companies.sector_strategy(payload, "staged")
    planned_steps = planned_steps_for(payload)

    errors: list[str] = []
    expected = case.expected
    if bool(payload.get("investor_names")) != bool(expected.get("requires_investor_resolution")):
        errors.append("investor resolution expectation mismatch")
    if "resolve_companies" not in planned_steps and expected.get("requires_company_resolution", True):
        errors.append("resolve_companies missing from planned steps")
    if expected.get("expected_strategy") and strategy != expected["expected_strategy"]:
        errors.append(f"expected strategy {expected['expected_strategy']}, got {strategy}")
    for alias in expected.get("expected_name_aliases") or []:
        if alias not in names:
            errors.append(f"missing expected alias expansion: {alias}")

    return {
        "id": case.id,
        "query": case.query,
        "status": "pass" if not errors else "fail",
        "mode": "dry_run",
        "planned_steps": planned_steps,
        "expanded_company_names": names,
        "company_sector_strategy": strategy,
        "has_attribute_filters": filters is not None,
        "has_hard_filters": hard_filters is not None,
        "has_soft_filters": soft_filters is not None,
        "errors": errors,
    }


def live_case(
    case: CompanyCase,
    *,
    app_dir: Path,
    env_file: str,
    env: dict[str, str],
    run_dir: Path,
    log_dir: Path,
) -> dict[str, Any]:
    init = sh(
        [
            sys.executable,
            str(TASK_STATE),
            "init",
            "--query",
            case.query,
            "--out-dir",
            str(run_dir),
            "--task-id",
            f"company-search-{case.id}",
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{case.id}-init.log",
    )
    state_path = Path(json.loads(init.stdout)["state"])
    planned_steps = planned_steps_for(case.payload)
    sh(
        [
            sys.executable,
            str(TASK_STATE),
            "request-approval",
            "--state",
            str(state_path),
            "--reason",
            "Company search harness live run.",
            "--proposed-next-step",
            ", ".join(planned_steps),
            "--plan-json",
            json.dumps({"planned_steps": planned_steps}),
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{case.id}-approval.log",
    )
    sh(
        [
            sys.executable,
            str(TASK_STATE),
            "approve",
            "--state",
            str(state_path),
            "--execution-mode",
            "search_only",
            "--note",
            "Company search harness.",
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{case.id}-approve.log",
    )

    payload = dict(case.payload)
    if payload.get("investor_names"):
        investor = sh(
            [
                sys.executable,
                str(PRIMITIVES / "resolve_investors" / "resolve_investors.py"),
                "--state",
                str(state_path),
                "--payload-json",
                json.dumps(payload),
                "--env-file",
                env_file,
                "--write-state",
            ],
            cwd=app_dir,
            env=env,
            log_path=log_dir / f"{case.id}-resolve-investors.log",
        )
        investor_output = json.loads(investor.stdout)
        payload["investors"] = investor_output.get("investor_urns") or investor_output.get("investors") or []

    company = sh(
        [
            sys.executable,
            str(PRIMITIVES / "resolve_companies" / "resolve_companies.py"),
            "--state",
            str(state_path),
            "--payload-json",
            json.dumps(payload),
            "--env-file",
            env_file,
            "--write-state",
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{case.id}-resolve-companies.log",
    )
    output = json.loads(company.stdout)
    status = "pass" if int(output.get("resolved_count") or 0) > 0 else "fail"
    return {
        "id": case.id,
        "query": case.query,
        "status": status,
        "mode": "live",
        "state": str(state_path),
        "planned_steps": planned_steps,
        "resolved_count": output.get("resolved_count"),
        "truncated": output.get("truncated"),
        "company_sector_strategy": output.get("company_sector_strategy"),
        "sector_strategy_broadened": output.get("sector_strategy_broadened"),
        "sample_companies": [row.get("company_name") for row in output.get("sample_companies", [])[:5]],
        "errors": [] if status == "pass" else ["resolved_count was zero"],
    }


def write_report(results: list[dict[str, Any]], *, mode: str, cases_path: Path) -> None:
    lines = [
        "# Company Search Harness",
        "",
        f"Last run: `{now_iso()}`",
        f"Mode: `{mode}`",
        f"Cases: `{cases_path}`",
        "",
        "| Case | Status | Planned Steps | Strategy | Resolved | Notes |",
        "|---|---|---|---|---:|---|",
    ]
    for row in results:
        notes = "; ".join(row.get("errors") or row.get("sample_companies") or [])
        lines.append(
            "| {id} | {status} | {steps} | {strategy} | {resolved} | {notes} |".format(
                id=row["id"],
                status=row["status"],
                steps=", ".join(row.get("planned_steps") or []),
                strategy=row.get("company_sector_strategy") or "",
                resolved=row.get("resolved_count") if row.get("resolved_count") is not None else "",
                notes=notes.replace("|", "\\|"),
            )
        )
    lines.append("")
    REPORT_PATH.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Powerpacks company-search harness")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--app-dir", default=str(DEFAULT_APP_DIR))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--case-glob")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    cases_path = Path(args.cases)
    cases = load_cases(cases_path)
    if args.case_glob:
        pattern = re.compile(args.case_glob)
        cases = [case for case in cases if pattern.search(case.id) or pattern.search(case.query)]
    if args.max_cases:
        cases = cases[: args.max_cases]

    app_dir = Path(args.app_dir)
    run_dir = app_dir / ".powerpacks" / "runs" / "company-search"
    log_dir = app_dir / ".powerpacks" / "runs" / "company-search-logs"
    env = os.environ.copy()
    results: list[dict[str, Any]] = []
    for case in cases:
        print(f"running {case.id}...", flush=True)
        try:
            if args.live:
                results.append(
                    live_case(
                        case,
                        app_dir=app_dir,
                        env_file=args.env_file,
                        env=env,
                        run_dir=run_dir,
                        log_dir=log_dir,
                    )
                )
            else:
                results.append(dry_run_case(case))
        except Exception as exc:
            results.append({
                "id": case.id,
                "query": case.query,
                "status": "fail",
                "mode": "live" if args.live else "dry_run",
                "planned_steps": planned_steps_for(case.payload),
                "errors": [str(exc)],
            })

    write_report(results, mode="live" if args.live else "dry_run", cases_path=cases_path)
    print(json.dumps({"report": str(REPORT_PATH), "results": results}, indent=2, sort_keys=True))
    if any(row.get("status") != "pass" for row in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
