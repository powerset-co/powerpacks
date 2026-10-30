#!/usr/bin/env python3
"""Bring the school parent mapping up to date with the schools index.

Adds schools the mapping has not seen, rolls same-named schools together for
free, and with --llm asks the model about the schools nobody has reviewed. Each
batch is written back to the mapping, so a stopped run resumes where it ended.
Without --llm nothing is spent; the printed estimate covers what --llm would do.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

import turbopuffer  # noqa: E402
from openai import OpenAI  # noqa: E402

from packs.indexing.lib.llm_config import CHAT_MODEL_PRICES_PER_1K_USD, DEFAULT_IDENTITY_MODEL, api_call_kwargs  # noqa: E402
from packs.indexing.lib.school_parents import (  # noqa: E402
    MAPPING_PATH,
    add_schools,
    apply_canonical_names,
    apply_same_names,
    flatten,
    read_rows,
    write_rows,
)

SCHOOLS_NAMESPACE = "aleph_education_v3"
BATCH_SIZE = 50
INPUT_TOKENS_PER_SCHOOL = 15
OUTPUT_TOKENS_PER_SCHOOL = 30

PROMPT = """For each school below, give its canonical institution name.

Rules:
1. A sub-school or program gets its parent university ("Harvard Business School" -> "Harvard University").
2. A translation, abbreviation or spelling variant gets the common English name ("Kungliga tekniska högskolan" -> "KTH Royal Institute of Technology", "TU München" -> "Technical University of Munich").
3. Leave out any school whose name is already canonical, and any you are unsure about.
4. Campuses that are separate institutions stay separate ("University of California, Berkeley" stays itself).

Schools:
{schools}

Return a JSON object mapping school name to canonical name, listing only the schools that change."""


def turbopuffer_schools(namespace: str) -> list[dict[str, Any]]:
    client = turbopuffer.Turbopuffer(api_key=os.environ["TURBOPUFFER_API_KEY"], region=os.getenv("TURBOPUFFER_REGION", "gcp-us-central1"))
    schools: list[dict[str, Any]] = []
    while True:
        page = client.namespace(namespace).query(
            rank_by=("id", "asc"),
            top_k=1000,
            include_attributes=["school_name", "canonical_key", "person_count"],
            filters=("id", "Gt", schools[-1]["id"]) if schools else None,
        )
        schools += [row.to_dict() for row in page.rows]
        if len(page.rows) < 1000:
            return schools


def read_schools(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def estimate_usd(unreviewed: int, model: str) -> float:
    prices = CHAT_MODEL_PRICES_PER_1K_USD[model]
    return unreviewed * (INPUT_TOKENS_PER_SCHOOL * prices["input"] + OUTPUT_TOKENS_PER_SCHOOL * prices["output"]) / 1000


def canonical_names(client: OpenAI, names: list[str], model: str) -> tuple[dict[str, str], float]:
    prompt = PROMPT.format(schools="\n".join(f"- {name}" for name in names))
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        **{**api_call_kwargs(model), "max_completion_tokens": 8000},
    )
    prices = CHAT_MODEL_PRICES_PER_1K_USD[model]
    usd = (response.usage.prompt_tokens * prices["input"] + response.usage.completion_tokens * prices["output"]) / 1000
    return json.loads(response.choices[0].message.content), usd


def review_with_llm(rows: dict[str, dict[str, Any]], counts: dict[str, int], model: str, output: Path, limit: int | None) -> dict[str, Any]:
    unreviewed = [row for row in rows.values() if row["source"] is None and not row["parent_id"]]
    unreviewed.sort(key=lambda row: -counts.get(row["id"], 0))
    unreviewed = unreviewed[:limit] if limit else unreviewed
    batches = [unreviewed[i : i + BATCH_SIZE] for i in range(0, len(unreviewed), BATCH_SIZE)]
    client = OpenAI()
    failed = 0
    spent = 0.0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(canonical_names, client, [row["school_name"] for row in batch], model): batch for batch in batches}
        for future in as_completed(futures):
            batch = futures[future]
            try:
                names, usd = future.result()
            except Exception as e:
                print(f"batch failed, stays unreviewed: {e}", file=sys.stderr)
                failed += len(batch)
                continue
            spent += usd
            apply_canonical_names(rows, [row["id"] for row in batch], names, counts)
            flatten(rows)
            write_rows(rows, output)
    return {"reviewed": len(unreviewed) - failed, "failed": failed, "spent_usd": round(spent, 4)}


def run(schools: list[dict[str, Any]], mapping: Path, llm: bool, model: str, limit: int | None) -> dict[str, Any]:
    rows = read_rows(mapping)
    counts = {school["id"]: int(school.get("person_count") or 0) for school in schools}
    added = add_schools(rows, schools)
    named = apply_same_names(rows, counts)
    flatten(rows)
    write_rows(rows, mapping)
    report: dict[str, Any] = {"schools": len(schools), "added": added, "same_name": named}
    if llm:
        report |= review_with_llm(rows, counts, model, mapping, limit)
    unreviewed = sum(1 for row in rows.values() if row["source"] is None and not row["parent_id"])
    report |= {"unreviewed": unreviewed, "llm_estimate_usd": round(estimate_usd(unreviewed, model), 2), "with_parent": sum(1 for row in rows.values() if row["parent_id"])}
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schools", type=Path, help="JSONL with id, school_name, canonical_key, person_count; default reads the live namespace")
    parser.add_argument("--namespace", default=SCHOOLS_NAMESPACE)
    parser.add_argument("--mapping", type=Path, default=MAPPING_PATH)
    parser.add_argument("--llm", action="store_true", help="Spend: ask the model about unreviewed schools")
    parser.add_argument("--limit", type=int, help="With --llm, review only the largest N schools")
    parser.add_argument("--model", default=DEFAULT_IDENTITY_MODEL)
    args = parser.parse_args()
    schools = read_schools(args.schools) if args.schools else turbopuffer_schools(args.namespace)
    print(json.dumps(run(schools, args.mapping, args.llm, args.model, args.limit), sort_keys=True))


if __name__ == "__main__":
    main()
