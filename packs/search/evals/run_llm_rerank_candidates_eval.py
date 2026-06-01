#!/usr/bin/env python3
"""Live eval for the Powerpacks llm_rerank_candidates primitive.

This is intentionally a behavior eval, not a prompt-text unit test. It invokes
`packs/search/primitives/llm_rerank_candidates/llm_rerank_candidates.py` against
an OpenAI-compatible endpoint with synthetic profiles and checks that explicit
seniority prompt semantics hold for Senior SWE-style searches.

Requires OPENAI_API_KEY unless --api-key is provided. Writes a JSON summary to
stdout and exits non-zero on failed expectations.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
PRIMITIVE = ROOT / "packs" / "search" / "primitives" / "llm_rerank_candidates" / "llm_rerank_candidates.py"


def position(title: str, band: str, track: str, text: str, *, current: bool = True) -> dict[str, Any]:
    return {
        "position_title": title,
        "seniority_band": band,
        "role_track": track,
        "dense_text": text,
        "company_name": "ExampleAI" if current else "OldCo",
        "is_current": current,
    }


def candidate(person_id: str, title: str, band: str, track: str, text: str, *, past: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": person_id,
        "headline": f"{title} at ExampleAI",
        "location": "San Francisco Bay Area",
        "positions": [position(title, band, track, text), *(past or [])],
    }


def fixtures() -> dict[str, dict[str, Any]]:
    return {
        "senior_swe": candidate(
            "senior_swe",
            "Senior Software Engineer",
            "senior",
            "individual_contributor",
            "Hands-on senior IC building distributed backend systems and production APIs.",
        ),
        "staff_swe": candidate(
            "staff_swe",
            "Staff Software Engineer",
            "staff",
            "individual_contributor",
            "Hands-on staff IC setting technical direction and building distributed systems.",
        ),
        "principal_swe": candidate(
            "principal_swe",
            "Principal Software Engineer",
            "principal",
            "individual_contributor",
            "Hands-on principal IC setting architecture and building distributed systems.",
        ),
        "engineering_manager": candidate(
            "engineering_manager",
            "Engineering Manager",
            "manager",
            "manager",
            "Manages a team of software engineers, owns planning, hiring, and delivery.",
        ),
        "cto_advisor": candidate(
            "cto_advisor",
            "CTO / Tech Advisor",
            "c-suite",
            "executive",
            "Executive technology leader and advisor; owns company technical strategy.",
            past=[
                position(
                    "Senior Software Engineer",
                    "senior",
                    "individual_contributor",
                    "Previously built backend systems as a senior IC.",
                    current=False,
                )
            ],
        ),
        "senior_consultant": candidate(
            "senior_consultant",
            "Senior Tech Consultant",
            "senior",
            "consultant",
            "Consults with engineering organizations on architecture and technical strategy.",
        ),
    }


def run_case(
    *,
    case_id: str,
    query: str,
    traits: list[str],
    candidate_ids: list[str],
    model: str,
    api_base: str,
    api_key: str,
    concurrency: int,
    timeout: int,
) -> dict[str, Any]:
    people = fixtures()
    tmp_parent = Path(os.environ.get("TMPDIR", "/var/tmp"))
    with tempfile.TemporaryDirectory(prefix="powerpacks-rerank-eval-", dir=tmp_parent) as tmp:
        input_path = Path(tmp) / f"{case_id}.jsonl"
        input_path.write_text("\n".join(json.dumps(people[pid], sort_keys=True) for pid in candidate_ids) + "\n")
        cmd = [
            sys.executable,
            str(PRIMITIVE),
            "--in",
            str(input_path),
            "--query",
            query,
            "--model",
            model,
            "--api-base",
            api_base,
            "--api-key",
            api_key,
            "--concurrency",
            str(concurrency),
            "--timeout",
            str(timeout),
            "--out",
            "-",
        ]
        for trait in traits:
            cmd.extend(["--traits", trait])
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout * max(2, len(candidate_ids)))
    if proc.returncode != 0:
        raise RuntimeError(f"{case_id}: primitive failed with {proc.returncode}\nSTDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}")
    rows = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    scores = {row["id"]: float(row["score"]) for row in rows}
    verdicts = {row["id"]: row.get("verdict") for row in rows}
    return {"case_id": case_id, "query": query, "traits": traits, "scores": scores, "verdicts": verdicts, "stderr": proc.stderr}


def check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def evaluate(results: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    by_case = {case["case_id"]: case for case in results}

    senior = by_case["senior_swe"]
    s = senior["scores"]
    check(s["senior_swe"] >= 0.80, f"senior_swe score too low for Senior SWE case: {s}", failures)
    check(s["senior_swe"] > s["staff_swe"], f"Senior SWE should outrank Staff for plain Senior: {s}", failures)
    check(s["senior_swe"] > s["principal_swe"], f"Senior SWE should outrank Principal for plain Senior: {s}", failures)
    check(s["senior_swe"] > s["engineering_manager"] + 0.20, f"Senior SWE should materially outrank Engineering Manager: {s}", failures)
    check(s["senior_swe"] > s["cto_advisor"] + 0.30, f"Senior SWE should materially outrank CTO/advisor: {s}", failures)
    check(s["senior_swe"] > s["senior_consultant"] + 0.30, f"Senior SWE should materially outrank Senior Tech Consultant: {s}", failures)

    senior_plus = by_case["senior_plus"]
    sp = senior_plus["scores"]
    check(sp["staff_swe"] >= 0.70, f"Senior+ should allow Staff IC: {sp}", failures)
    check(sp["principal_swe"] >= 0.70, f"Senior+ should allow Principal IC: {sp}", failures)
    check(sp["staff_swe"] > sp["cto_advisor"] + 0.25, f"Senior+ Staff should outrank CTO/advisor: {sp}", failures)
    check(sp["principal_swe"] > sp["cto_advisor"] + 0.25, f"Senior+ Principal should outrank CTO/advisor: {sp}", failures)

    generic = by_case["generic_software_engineer"]
    g = generic["scores"]
    check(g["senior_swe"] >= 0.80, f"Generic SWE should keep Senior IC as strong match: {g}", failures)
    check(g["staff_swe"] >= 0.80, f"Generic SWE should keep Staff IC as strong match: {g}", failures)
    check(g["principal_swe"] >= 0.80, f"Generic SWE should keep Principal IC as strong match: {g}", failures)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=os.environ.get("LLM_RERANK_EVAL_MODEL", os.environ.get("LLM_RERANK_MODEL", "gpt-4o-mini")))
    parser.add_argument("--api-base", default=os.environ.get("OPENAI_API_BASE", "https://api.openai.com"))
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if not args.api_key:
        raise SystemExit("OPENAI_API_KEY is required (or pass --api-key)")

    cases = [
        {
            "case_id": "senior_swe",
            "query": "Senior SWE in SF",
            "traits": ["senior software engineer"],
            "candidate_ids": ["senior_swe", "staff_swe", "principal_swe", "engineering_manager", "cto_advisor", "senior_consultant"],
        },
        {
            "case_id": "senior_plus",
            "query": "Senior+ software engineers in SF",
            "traits": ["senior+ software engineer"],
            "candidate_ids": ["senior_swe", "staff_swe", "principal_swe", "cto_advisor"],
        },
        {
            "case_id": "generic_software_engineer",
            "query": "software engineers in SF",
            "traits": ["software engineer"],
            "candidate_ids": ["senior_swe", "staff_swe", "principal_swe"],
        },
    ]
    results = [
        run_case(model=args.model, api_base=args.api_base, api_key=args.api_key, concurrency=args.concurrency, timeout=args.timeout, **case)
        for case in cases
    ]
    failures = evaluate(results)
    summary = {"model": args.model, "passed": not failures, "failures": failures, "results": results}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
