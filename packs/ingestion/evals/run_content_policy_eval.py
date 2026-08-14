"""Content-policy eval for deep-context dossier synthesis.

Runs synthetic personas through the REAL synthesis call shape — the production
SYSTEM_PROMPT + render_chunk + FACT_SCHEMA via the responses API — then scans
each structured output for personal-content leaks while asserting the
professional facts and the celebrated milestone survived.

Flow:
  load content_policy/cases.json -> one synthesis call per persona ->
  scan the JSON blob against the leak-category regexes + per-case keep
  regexes -> print a JSON report and write it (plus raw per-person facts)
  to the out dir -> exit 0 only if every case is clean and kept both.

The synthesis prompt is deliberately allowlist-phrased (it names what belongs
in a dossier, never the categories it displaces); the deny-side regexes live
HERE, in the eval, where they score outputs instead of steering the model.

Spend: one real OpenAI synthesis call per case (~cents for the shipped three).
All personas are synthetic — never add real contact PII to cases.json.

Usage:
  uv run --project . python packs/ingestion/evals/run_content_policy_eval.py
  ... [--cases <path>] [--out-dir <dir>] [--effort medium]

Changelog:
  2026-08-02: extracted from the PR #398 scratch harness into a committed eval.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from packs.ingestion.primitives.deep_context import synthesize_person_context as synth
from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.indexing.lib.openai_responses import (
    make_async_client,
    parse_json_response,
    responses_kwargs,
)

EVAL_DIR = Path(__file__).resolve().parent / "content_policy"
DEFAULT_CASES = EVAL_DIR / "cases.json"


@dataclass(frozen=True)
class Case:
    full_name: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    source_channels: tuple[str, ...]
    messages: tuple[dict, ...]
    keep_professional: re.Pattern
    keep_milestone: re.Pattern


def load_cases(path: Path) -> tuple[dict[str, re.Pattern], list[Case]]:
    raw = json.loads(path.read_text())
    categories = {name: re.compile(pattern) for name, pattern in raw["leak_categories"].items()}
    cases = [
        Case(
            full_name=entry["full_name"],
            emails=tuple(entry["emails"]),
            phones=tuple(entry["phones"]),
            source_channels=tuple(entry["source_channels"]),
            messages=tuple(entry["messages"]),
            keep_professional=re.compile(entry["keep_professional"]),
            keep_milestone=re.compile(entry["keep_milestone"]),
        )
        for entry in raw["cases"]
    ]
    return categories, cases


def scan_facts(facts: dict, categories: dict[str, re.Pattern], case: Case) -> dict:
    blob = json.dumps(facts).lower()
    leaks = {
        name: sorted(set(pattern.findall(blob)))
        for name, pattern in categories.items()
        if pattern.search(blob)
    }
    worth = facts.get("network_worth")
    return {
        "leaks": leaks,
        "kept_professional": bool(case.keep_professional.search(blob)),
        "kept_milestone": bool(case.keep_milestone.search(blob)),
        "worth": worth if isinstance(worth, (dict, str)) else None,
        "passed": not leaks
        and bool(case.keep_professional.search(blob))
        and bool(case.keep_milestone.search(blob)),
    }


async def run_eval(cases_path: Path, out_dir: Path, effort: str) -> dict:
    categories, cases = load_cases(cases_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    synth.load_env()
    client = make_async_client()
    kwargs = responses_kwargs(
        DEFAULT_MODEL, effort=effort, schema=synth.FACT_SCHEMA, schema_name="person_facts"
    )
    results = {}
    for case in cases:
        print(f"content-policy-eval: synthesizing {case.full_name}", file=sys.stderr)
        person = {
            "full_name": case.full_name,
            "emails": list(case.emails),
            "phones": list(case.phones),
            "source_channels": list(case.source_channels),
        }
        response = await client.responses.create(
            model=DEFAULT_MODEL,
            input=[
                {"role": "system", "content": synth.SYSTEM_PROMPT},
                {"role": "user", "content": synth.render_chunk(person, list(case.messages))},
            ],
            **kwargs,
        )
        facts = parse_json_response(response)
        slug = case.full_name.replace(" ", "-").lower()
        (out_dir / f"{slug}.json").write_text(json.dumps(facts, indent=2))
        results[case.full_name] = scan_facts(facts, categories, case)
    report = {
        "contract_version": synth.SYNTHESIS_CONTRACT_VERSION,
        "model": DEFAULT_MODEL,
        "cases": len(cases),
        "passed": sum(1 for r in results.values() if r["passed"]),
        "results": results,
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--out-dir", type=Path, default=EVAL_DIR / "out")
    parser.add_argument("--effort", default="medium")
    args = parser.parse_args()
    report = asyncio.run(run_eval(args.cases, args.out_dir, args.effort))
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["passed"] == report["cases"] else 1)


if __name__ == "__main__":
    main()
