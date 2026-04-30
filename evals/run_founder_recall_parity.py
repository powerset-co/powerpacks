#!/usr/bin/env python3
"""Run Powerpacks search primitives against aleph founder recall cases.

This is a harness for parity, not a replacement for aleph's recall suite. It
uses the same packaged primitives the search-network skill calls and records a
compact markdown ledger.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / "primitives"
TASK_STATE = PRIMITIVES / "task_state" / "task_state.py"
REPORT_PATH = ROOT / "evals" / "harness_parity.md"

FOUNDER_SEMANTIC = (
    "People who founded or co-founded companies and hold founder, cofounder, founding CEO, founding CTO, "
    "or similar founding operator roles. Their profiles should show responsibility for starting, building, "
    "fundraising for, or leading a startup or technology company, not merely advising, investing, or working "
    "at a company founded by someone else."
)
FOUNDER_BM25 = [
    "founder",
    "co-founder",
    "cofounder",
    "founding CEO",
    "founding CTO",
    "founder CEO",
    "founder CTO",
]
RESULT_LIMIT_CAP = 1000


CASES: list[dict[str, Any]] = [
    {
        "id": "founders_argentina",
        "source": "tests/recall/founders_argentina.yaml",
        "payload": {"countries": ["Argentina"]},
    },
    {
        "id": "founders_devtools_infra",
        "source": "tests/recall/founders_devtools_infra.yaml",
        "payload": {
            "company_semantic_queries": [
                "Companies building developer tooling, infrastructure software, cloud infrastructure, observability, CI/CD, data infrastructure, databases, APIs, or technical platforms used by engineering teams."
            ],
            "sector_types": ["infra_devtools"],
            "company_sector_strategy": "soft_union",
            "is_current": True,
        },
        "company_prefilter": True,
    },
    {
        "id": "founders_fintech_california",
        "source": "tests/recall/founders_fintech_california.yaml",
        "payload": {
            "company_semantic_queries": [
                "Financial technology startups building payments, lending, banking, credit, investing, insurance, payroll, accounting, crypto finance, or other software products for financial services."
            ],
            "states": ["California"],
            "sector_types": ["fintech"],
            "company_sector_strategy": "soft_union",
            "entity_types": ["venture_backed_startup"],
            "is_current": True,
        },
        "company_prefilter": True,
    },
    {
        "id": "founders_ai_ml_data_large_pool",
        "source": "tests/recall/founders_ai_ml_data_large_pool.yaml",
        "payload": {
            "company_semantic_queries": [
                "Companies building AI and machine learning platforms, data infrastructure, developer tooling, model infrastructure, analytics systems, databases, or software infrastructure for technical teams."
            ],
            "sector_types": ["ai_ml", "data", "infra_devtools"],
            "company_sector_strategy": "soft_union",
            "is_current": True,
        },
        "company_prefilter": True,
    },
    {
        "id": "founders_backed_by_amplify",
        "source": "tests/recall/founders_backed_by_amplify.yaml",
        "payload": {"investor_names": ["Amplify Partners"], "is_current": True},
        "company_prefilter": True,
    },
    {
        "id": "founders_backed_by_elad_gil",
        "source": "tests/recall/founders_backed_by_elad_gil.yaml",
        "payload": {"investor_names": ["Elad Gil"], "is_current": True},
        "company_prefilter": True,
    },
    {
        "id": "founders_backed_by_naval_ravikant",
        "source": "tests/recall/founders_backed_by_naval_ravikant.yaml",
        "payload": {"investor_names": ["Naval Ravikant"], "is_current": True},
        "company_prefilter": True,
    },
    {
        "id": "founders_backed_by_peter_thiel",
        "source": "tests/recall/founders_backed_by_peter_thiel.yaml",
        "payload": {"investor_names": ["Peter Thiel"], "is_current": True},
        "company_prefilter": True,
    },
    {
        "id": "founders_backed_by_sam_altman",
        "source": "tests/recall/founders_backed_by_sam_altman.yaml",
        "payload": {"investor_names": ["Sam Altman"], "is_current": True},
        "company_prefilter": True,
    },
    {
        "id": "founders_backed_by_sequoia",
        "source": "tests/recall/founders_backed_by_sequoia.yaml",
        "payload": {"investor_names": ["Sequoia Capital"], "is_current": True},
        "company_prefilter": True,
    },
    {
        "id": "founders_database_companies",
        "source": "tests/recall/founders_database_companies.yaml",
        "payload": {
            "company_semantic_queries": [
                "Companies building database systems, hosted databases, data storage engines, database infrastructure, SQL or NoSQL databases, or developer platforms for managing application data."
            ],
            "sector_types": ["data"],
            "company_sector_strategy": "soft_union",
            "is_current": True,
        },
        "company_prefilter": True,
    },
    {
        "id": "date_range_founders_since_2018",
        "source": "tests/recall/date_range_founders_since_2018.yaml",
        "payload": {"cities": ["San Francisco"], "states": ["California"], "position_after_date": "2022"},
    },
    {
        "id": "staging_founders_basic",
        "source": "tests/recall/staging/founders_basic.yaml",
        "payload": {"is_current": True},
    },
    {
        "id": "staging_founders_in_san_francisco",
        "source": "tests/recall/staging/founders_in_san_francisco.yaml",
        "payload": {"cities": ["San Francisco"], "states": ["California"], "is_current": True},
    },
    {
        "id": "staging_founders_sequoia",
        "source": "tests/recall/staging/founders_sequoia.yaml",
        "payload": {"investor_names": ["Sequoia Capital"], "is_current": True},
        "company_prefilter": True,
    },
]


def sh(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)
    log_path.write_text(
        "$ " + " ".join(args) + "\n\n"
        + "STDOUT:\n" + completed.stdout
        + "\nSTDERR:\n" + completed.stderr
    )
    if check and completed.returncode != 0:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(args)}\nsee {log_path}")
    return completed


def parse_scalar(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*[\"']?([^\"'\n#]+)", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def parse_expected_ids(text: str) -> list[str]:
    ids: list[str] = []
    in_expected = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("expected_person_ids:"):
            in_expected = True
            continue
        if in_expected and line and not line.startswith("-") and re.match(r"^[a-zA-Z_]+:", line):
            break
        if in_expected and line.startswith("-"):
            value = line[1:].strip().split("#", 1)[0].strip().strip("\"'")
            if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value):
                ids.append(value)
    return ids


def load_case_metadata(app_dir: Path, case: dict[str, Any]) -> dict[str, Any]:
    source_path = app_dir / case["source"]
    text = source_path.read_text()
    return {
        "query": parse_scalar(text, "query") or case["id"].replace("_", " "),
        "expected_count": int(parse_scalar(text, "expected_count") or 0),
        "limit": int(parse_scalar(text, "limit") or 100),
        "min_recall": float(parse_scalar(text, "min_recall") or 0.5),
        "expected_ids": parse_expected_ids(text),
    }


def latest_step(state: dict[str, Any], step_id: str) -> dict[str, Any]:
    for step in reversed(state.get("steps", [])):
        if step.get("id") == step_id:
            return step.get("output", {}) or {}
    return {}


def record_step(app_dir: Path, state: Path, step_id: str, output: dict[str, Any], env: dict[str, str], log_dir: Path) -> None:
    sh(
        [
            sys.executable,
            str(TASK_STATE),
            "record-step",
            "--state",
            str(state),
            "--step-id",
            step_id,
            "--output-json",
            json.dumps(output, separators=(",", ":")),
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{state.stem}-{step_id}.log",
    )


def role_payload(case: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "semantic_query": FOUNDER_SEMANTIC,
        "bm25_queries": FOUNDER_BM25,
        "role_ids": ["founder"],
        **case.get("payload", {}),
    }
    if case.get("company_prefilter"):
        payload["prefilters"] = {
            "stages": [
                {
                    "stage": "large_company_intersection",
                    "mode": "intersect_base_ids",
                    "output": "base_candidate_ids",
                    "reason": "Resolve company/investor/sector constraints to company IDs before founder role retrieval.",
                }
            ]
        }
        payload["has_domain_intent"] = True
    else:
        payload["has_domain_intent"] = False
    payload["adjacency_mode"] = "off"
    return payload


def run_case(app_dir: Path, case: dict[str, Any], env: dict[str, str], run_dir: Path, log_dir: Path) -> dict[str, Any]:
    meta = load_case_metadata(app_dir, case)
    result: dict[str, Any] = {
        "id": case["id"],
        "source": case["source"],
        **meta,
        "status": "pending",
    }
    if case.get("unsupported"):
        result.update({"status": "unsupported", "reason": case["unsupported"]})
        return result
    result_limit = min(meta["limit"], RESULT_LIMIT_CAP)

    init = sh(
        [
            sys.executable,
            str(TASK_STATE),
            "init",
            "--query",
            meta["query"],
            "--out-dir",
            str(run_dir),
            "--task-id",
            f"harness-founder-{case['id']}",
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{case['id']}-init.log",
    )
    state_path = Path(json.loads(init.stdout)["state"])
    result["state"] = str(state_path)

    payload = role_payload(case)
    record_step(app_dir, state_path, "expand_search_request", {"role_search_filters": payload}, env, log_dir)
    record_step(
        app_dir,
        state_path,
        "decide_search_strategy",
        {
            "strategy": "count_then_execute",
            "reason": "Founder recall parity harness uses count, optional company prefilter, retrieval, hydration, and export.",
            "candidate_limit": result_limit,
            "hydrate_limit": result_limit,
        },
        env,
        log_dir,
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
            "Founder parity harness.",
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{case['id']}-approve.log",
    )

    if case.get("company_prefilter"):
        payload_for_resolution = payload
        if payload_for_resolution.get("investor_names") or payload_for_resolution.get("investors"):
            sh(
                [
                    sys.executable,
                    str(PRIMITIVES / "resolve_investors" / "resolve_investors.py"),
                    "--state",
                    str(state_path),
                    "--env-file",
                    ".env",
                    "--write-state",
                ],
                cwd=app_dir,
                env=env,
                log_path=log_dir / f"{case['id']}-resolve-investors.log",
            )
        sh(
            [
                sys.executable,
                str(PRIMITIVES / "resolve_companies" / "resolve_companies.py"),
                "--state",
                str(state_path),
                "--env-file",
                ".env",
                "--write-state",
            ],
            cwd=app_dir,
            env=env,
            log_path=log_dir / f"{case['id']}-resolve-companies.log",
        )
        sh(
            [
                sys.executable,
                str(PRIMITIVES / "apply_prefilters" / "apply_prefilters.py"),
                "--state",
                str(state_path),
                "--env-file",
                ".env",
                "--write-state",
            ],
            cwd=app_dir,
            env=env,
            log_path=log_dir / f"{case['id']}-apply-prefilters.log",
        )

    sh(
        [
            sys.executable,
            str(PRIMITIVES / "count_candidates" / "count_candidates.py"),
            "--state",
            str(state_path),
            "--env-file",
            ".env",
            "--write-state",
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{case['id']}-count.log",
    )
    sh(
        [
            sys.executable,
            str(PRIMITIVES / "execute_role_search" / "execute_role_search.py"),
            "--state",
            str(state_path),
            "--env-file",
            ".env",
            "--limit",
            str(result_limit),
            "--write-state",
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{case['id']}-execute.log",
    )
    sh(
        [
            sys.executable,
            str(PRIMITIVES / "hydrate_people" / "hydrate_people.py"),
            "--state",
            str(state_path),
            "--env-file",
            ".env",
            "--write-state",
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{case['id']}-hydrate.log",
    )
    export = sh(
        [
            sys.executable,
            str(PRIMITIVES / "persist_search_results" / "results_io.py"),
            "export",
            "--state",
            str(state_path),
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{case['id']}-export.log",
    )

    state = json.loads(state_path.read_text())
    count = latest_step(state, "count_candidates")
    retrieval = latest_step(state, "execute_role_search")
    hydration = latest_step(state, "hydrate_people")
    artifact = json.loads(export.stdout)
    candidates = retrieval.get("candidate_ids") or []
    expected_ids = meta["expected_ids"]
    hits = [pid for pid in expected_ids if pid in set(candidates)]
    recall = (len(hits) / len(expected_ids)) if expected_ids else None

    if expected_ids:
        passed = bool(recall is not None and recall >= meta["min_recall"])
    elif meta["expected_count"]:
        passed = int(retrieval.get("returned_people") or 0) >= meta["expected_count"]
    else:
        passed = int(retrieval.get("returned_people") or 0) > 0

    result.update({
        "status": "pass" if passed else "fail",
        "unique_people_count": count.get("unique_people"),
        "position_rows_count": count.get("position_rows"),
        "returned_people": retrieval.get("returned_people"),
        "hydrated": hydration.get("hydrated"),
        "expected_id_count": len(expected_ids),
        "hit_count": len(hits),
        "recall": recall,
        "missed_ids": [pid for pid in expected_ids if pid not in set(candidates)][:20],
        "csv": artifact.get("csv"),
        "jsonl": artifact.get("jsonl"),
        "manifest": artifact.get("manifest"),
    })
    return result


def write_report(results: list[dict[str, Any]], app_dir: Path, run_dir: Path, log_dir: Path) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    passed = sum(1 for row in results if row["status"] == "pass")
    failed = sum(1 for row in results if row["status"] == "fail")
    unsupported = sum(1 for row in results if row["status"] == "unsupported")
    lines = [
        "# Harness Parity",
        "",
        f"Last run: `{now}`",
        "",
        "Scope: founder recall YAMLs from aleph-mvp, executed through Powerpacks `search-network` primitives.",
        "",
        f"App dir: `{app_dir}`",
        f"Run dir: `{run_dir}`",
        f"Log dir: `{log_dir}`",
        "",
        "Execution notes:",
        "",
        "- Uses packaged Powerpacks primitives, not aleph-mvp application code.",
        f"- Retrieval and hydration respect recall case limits up to `{RESULT_LIMIT_CAP}` people.",
        "- LLM scoring/filtering and company signal summaries are disabled.",
        "- `resolve_investors` resolves firm and person investors from the Powerpacks TurboPuffer investors namespace.",
        "",
        f"Summary: `{passed}` pass, `{failed}` fail, `{unsupported}` unsupported.",
        "",
        "| Case | Query | Status | Count | Returned | Hydrated | Expected Hits | Recall | Artifact | Notes |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in results:
        expected = row.get("expected_id_count") or 0
        hits = row.get("hit_count") or 0
        recall = row.get("recall")
        recall_text = "" if recall is None else f"{recall:.0%}"
        artifact = row.get("csv") or row.get("state") or ""
        note = row.get("reason") or ""
        if row.get("missed_ids"):
            note = f"missed {len(row['missed_ids'])}+ expected ids"
        lines.append(
            "| {id} | {query} | {status} | {count} | {returned} | {hydrated} | {hits}/{expected} | {recall} | `{artifact}` | {note} |".format(
                id=row["id"],
                query=str(row["query"]).replace("|", "\\|"),
                status=row["status"],
                count=row.get("unique_people_count", ""),
                returned=row.get("returned_people", ""),
                hydrated=row.get("hydrated", ""),
                hits=hits,
                expected=expected,
                recall=recall_text,
                artifact=artifact,
                note=note.replace("|", "\\|"),
            )
        )
    lines.extend([
        "",
        "Open gaps:",
        "",
        "- Keep the TurboPuffer investors namespace fresh as investor source data changes.",
        "- Improve broad company-domain recall for devtools/infra and fintech with better company semantic queries or sliced company search.",
        "- Add people-side slicing for broad founder/date/location pools instead of relying on one ranked frontier.",
        "- Reconcile staging recall files that appear to use a different person ID namespace.",
        "- Persist applied filters for every case in a compact report row.",
        "",
    ])
    REPORT_PATH.write_text("\n".join(lines))


def main() -> None:
    app_dir = Path(os.environ.get("POWERPACKS_APP_DIR", "/Users/arthur/workspace/aleph-mvp"))
    run_dir = app_dir / ".powerpacks" / "runs" / "founder-parity"
    log_dir = app_dir / ".powerpacks" / "runs" / "founder-parity-logs"
    env = os.environ.copy()

    results = []
    for case in CASES:
        print(f"running {case['id']}...", flush=True)
        try:
            results.append(run_case(app_dir, case, env, run_dir, log_dir))
        except Exception as exc:  # Keep the batch running so the report shows all blockers.
            meta = load_case_metadata(app_dir, case)
            results.append({
                "id": case["id"],
                "source": case["source"],
                **meta,
                "status": "fail",
                "reason": str(exc),
            })
    write_report(results, app_dir, run_dir, log_dir)
    print(json.dumps({"report": str(REPORT_PATH), "results": results}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
