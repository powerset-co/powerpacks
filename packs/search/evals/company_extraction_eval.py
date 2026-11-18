#!/usr/bin/env python3
"""Does the company extractor ask for companies when a query names an industry, and only then?

`extractor_eval.py` scores the network-search-api CSV on company names and investors. These cases
cover what it does not: an industry named as the person's specialty ("propulsion experience",
"AI skills") must produce a company query; a plain job function, a tool, or an investor's check size
must leave the company fields empty. Each case runs `--runs` times because answers vary.

Case keys: company (true = a company query or sector is required, false = every company field is
empty), no_sector (no sector tag fits), names, investors, no_funding (no funding or stage field).

Usage:
  uv run --env-file .env --project . python packs/search/evals/company_extraction_eval.py \
    --model gpt-5.6-luna --reasoning-effort medium

Created: 2026-11-17
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

SEARCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SEARCH_ROOT / "primitives" / "expand_search_request"))

import openai  # noqa: E402
from parallel_extractors import _extract, _load_prompt  # noqa: E402

CASES = Path(__file__).resolve().parent / "company-extraction" / "cases.json"
MAX_IN_FLIGHT = 12


def filled(output: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in output.items() if value not in (None, "", [], {}, False)}


def passed(case: dict[str, Any], output: dict[str, Any]) -> bool:
    got = filled(output)
    has_company = bool(got.get("company_semantic_queries") or got.get("sector_types"))
    checks = []
    if "company" in case:
        checks.append(has_company if case["company"] else not got)
    if case.get("no_sector"):
        checks.append(not got.get("sector_types"))
    if case.get("names"):
        checks.append(bool(got.get("company_names")))
    if case.get("investors"):
        checks.append(bool(got.get("investors")))
    if case.get("no_funding"):
        checks.append(not any(key.startswith("funding") for key in got))
    return all(checks)


async def run(args: argparse.Namespace) -> int:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    prompt = _load_prompt("company")
    client = openai.AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=os.environ.get("OPENAI_API_BASE"))
    slots = asyncio.Semaphore(MAX_IN_FLIGHT)

    async def one(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        async with slots:
            return case, await _extract(client, "company", prompt, case["query"], model=args.model,
                                        reasoning_effort=args.reasoning_effort)

    results = await asyncio.gather(*(one(case) for case in cases for _ in range(args.runs)))
    totals: dict[str, list[int]] = {}
    for case, output in results:
        ok = passed(case, output)
        tally = totals.setdefault(case["category"], [0, 0])
        tally[0] += ok
        tally[1] += 1
        if not ok:
            print(f"FAIL [{case['category']}] {case['query']}: {json.dumps(filled(output))[:300]}")
    for category, (ok, total) in totals.items():
        print(f"{category}: {ok}/{total}")
    failed = sum(total - ok for ok, total in totals.values())
    print(f"total: {len(results) - failed}/{len(results)}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--runs", type=int, default=2)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
