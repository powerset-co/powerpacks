#!/usr/bin/env python3
"""Per-extractor evals using CSV datasets from network-search-api.

Runs each parallel extractor against its ground-truth CSV dataset and reports
Jaccard similarity, key-field accuracy, and per-item results.

No Langfuse dependency — runs locally with CSV files.

Usage:
  uv run --env-file .env --project . python packs/search/evals/extractor_eval.py \
    --extractor education --csv ../network-search-api/tests/evals/query_expansion/datasets/education_extraction.csv

  # All extractors:
  uv run --env-file .env --project . python packs/search/evals/extractor_eval.py --all

  # Dry run (no API calls):
  uv run --env-file .env --project . python packs/search/evals/extractor_eval.py --all --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SEARCH_ROOT = Path(__file__).resolve().parents[1]
EXTRACTORS_DIR = SEARCH_ROOT / "primitives" / "expand_search_request"
DEFAULT_DATASET_DIR = ROOT.parent / "network-search-api" / "tests" / "evals" / "query_expansion" / "datasets"
REPORT_DIR = SEARCH_ROOT / "evals"

sys.path.insert(0, str(EXTRACTORS_DIR))
sys.path.insert(0, str(ROOT))

import openai  # noqa: E402
from parallel_extractors import _extract, _load_prompt, EXTRACTOR_MODELS, ROLE_EXTRACTION_PROMPT  # noqa: E402

from packs.shared.csv_io import CsvIO  # noqa: E402


# ---------------------------------------------------------------------------
# Evaluators (ported from network-search-api/tests/evals/evaluators.py)
# ---------------------------------------------------------------------------

def jaccard(pred: set, exp: set) -> float:
    if not pred and not exp:
        return 1.0
    if not pred or not exp:
        return 0.0
    return len(pred & exp) / len(pred | exp)


def case_insensitive_set(values: list) -> set[str]:
    return {str(v).lower().strip() for v in values if v}


def parse_list(value: str) -> list[str]:
    """Parse a CSV list field like '["a", "b"]' or nested '[["a"]]' or empty string."""
    if not value or value.strip() in ("", "[]", "[[]]"):
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            # Flatten nested lists: [["a", "b"], ["c"]] → ["a", "b", "c"]
            flat: list[str] = []
            for item in parsed:
                if isinstance(item, list):
                    flat.extend(str(v) for v in item)
                else:
                    flat.append(str(item))
            return flat
    except (json.JSONDecodeError, TypeError):
        pass
    return [v.strip().strip('"').strip("'") for v in value.split(",") if v.strip()]


# ---------------------------------------------------------------------------
# Extractor configs: CSV column → extractor output key mapping
# ---------------------------------------------------------------------------

EXTRACTOR_CONFIGS: dict[str, dict[str, Any]] = {
    "education": {
        "csv": "education_extraction.csv",
        "prompt_name": "education",
        "model": "gpt-4.1",
        "fields": {
            "schools": {"type": "list", "key_field": True},
            "degree_levels": {"type": "list"},
            "fields_of_study": {"type": "list"},
        },
    },
    "company": {
        "csv": "company_extraction.csv",
        "prompt_name": "company",
        "model": "gpt-5.4",
        "fields": {
            "company_names": {"type": "list", "key_field": True},
            "company_semantic_queries": {"type": "list"},
            "investors": {"type": "list", "key_field": True},
            "entity_types": {"type": "list"},
            "sector_types": {"type": "list"},
        },
    },
    "location": {
        "csv": "location_extraction.csv",
        "prompt_name": "location",
        "model": "gpt-4.1",
        "fields": {
            "cities": {"type": "list", "key_field": True},
            "states": {"type": "list"},
            "metro_areas": {"type": "list"},
            "countries": {"type": "list"},
            "macro_regions": {"type": "list"},
            "company_cities": {"type": "list"},
            "company_states": {"type": "list"},
            "company_metro_areas": {"type": "list"},
            "company_countries": {"type": "list"},
            "company_macro_regions": {"type": "list"},
        },
    },
    "seniority": {
        "csv": "seniority_extraction.csv",
        "prompt_name": "seniority",
        "model": "gpt-4.1",
        "fields": {
            "seniority_bands": {"type": "list", "key_field": True},
        },
    },
    "role": {
        "csv": "role_extraction.csv",
        "prompt_name": "role",
        "model": "gpt-4.1",
        "fields": {
            "expected_bm25_queries": {"type": "list", "output_key": "bm25_queries", "key_field": True},
        },
    },
}


# ---------------------------------------------------------------------------
# Run one extractor eval
# ---------------------------------------------------------------------------

async def run_extractor_eval(
    name: str,
    config: dict[str, Any],
    dataset_dir: Path,
    *,
    api_key: str,
    api_base: str,
    max_cases: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    csv_path = dataset_dir / config["csv"]
    if not csv_path.exists():
        return {"extractor": name, "status": "skipped", "reason": f"CSV not found: {csv_path}"}

    # Load CSV
    with open(csv_path) as f:
        reader = CsvIO.dict_reader(f)
        rows = list(reader)
    if max_cases:
        rows = rows[:max_cases]

    if dry_run:
        return {"extractor": name, "status": "dry-run", "cases": len(rows)}

    # Load prompt
    prompt_name = config["prompt_name"]
    prompt = _load_prompt(prompt_name) if prompt_name != "role" else ROLE_EXTRACTION_PROMPT
    model = config.get("model") or EXTRACTOR_MODELS.get(prompt_name, "gpt-4.1")

    client = openai.AsyncOpenAI(api_key=api_key, base_url=api_base)
    fields = config["fields"]
    key_fields = [f for f, cfg in fields.items() if cfg.get("key_field")]

    results: list[dict[str, Any]] = []
    started = time.monotonic()

    for row in rows:
        query = row.get("query", "")
        if not query:
            continue

        # Run extractor
        t0 = time.monotonic()
        output = await _extract(client, name, prompt, query, model=model)
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # Evaluate each field
        field_scores: dict[str, float] = {}
        field_details: dict[str, dict] = {}
        for field_name, field_cfg in fields.items():
            expected = parse_list(row.get(field_name, ""))
            output_key = field_cfg.get("output_key", field_name)
            predicted = output.get(output_key, []) or []
            if not isinstance(predicted, list):
                predicted = [predicted] if predicted else []

            exp_set = case_insensitive_set(expected)
            pred_set = case_insensitive_set(predicted)
            score = jaccard(pred_set, exp_set)
            field_scores[field_name] = score
            field_details[field_name] = {
                "score": score,
                "expected": sorted(exp_set),
                "predicted": sorted(pred_set),
            }

        # Key fields accuracy
        key_correct = all(
            field_scores.get(f, 0.0) >= 1.0 for f in key_fields
        ) if key_fields else True

        results.append({
            "query_id": row.get("query_id", ""),
            "category": row.get("category", ""),
            "query": query,
            "key_correct": key_correct,
            "field_scores": field_scores,
            "field_details": field_details,
            "elapsed_ms": elapsed_ms,
        })

    total_ms = int((time.monotonic() - started) * 1000)

    # Aggregate scores
    n = len(results)
    accuracy = sum(1 for r in results if r["key_correct"]) / n if n else 0.0
    field_means: dict[str, float] = {}
    for field_name in fields:
        vals = [r["field_scores"].get(field_name, 0.0) for r in results]
        field_means[field_name] = sum(vals) / len(vals) if vals else 0.0

    return {
        "extractor": name,
        "status": "completed",
        "cases": n,
        "accuracy": round(accuracy, 3),
        "field_means": {k: round(v, 3) for k, v in field_means.items()},
        "total_ms": total_ms,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_report(all_results: list[dict[str, Any]]) -> Path:
    report_path = REPORT_DIR / "extractor_eval.md"
    lines = [
        "# Extractor Eval",
        "",
        f"Dataset: `{DEFAULT_DATASET_DIR}`",
        "",
        "| Extractor | Cases | Accuracy | Key Fields | Time |",
        "|---|---:|---:|---|---:|",
    ]
    for r in all_results:
        if r["status"] != "completed":
            lines.append(f"| {r['extractor']} | — | — | {r['status']} | — |")
            continue
        field_str = ", ".join(f"{k}={v:.0%}" for k, v in r["field_means"].items())
        lines.append(
            f"| {r['extractor']} | {r['cases']} | {r['accuracy']:.0%} | {field_str} | {r['total_ms']}ms |"
        )

    # Per-extractor failures
    for r in all_results:
        if r["status"] != "completed":
            continue
        failures = [item for item in r["results"] if not item["key_correct"]]
        if failures:
            lines.extend([
                "",
                f"### {r['extractor']} failures ({len(failures)}/{r['cases']})",
                "",
            ])
            for item in failures[:10]:
                lines.append(f"- **{item['query']}**")
                for field, detail in item["field_details"].items():
                    if detail["score"] < 1.0:
                        lines.append(f"  - {field}: expected={detail['expected']}, got={detail['predicted']}")

    lines.append("")
    report_path.write_text("\n".join(lines))
    return report_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Run per-extractor evals against CSV datasets")
    parser.add_argument("--extractor", choices=sorted(EXTRACTOR_CONFIGS.keys()), help="Run a single extractor")
    parser.add_argument("--all", action="store_true", help="Run all extractors")
    parser.add_argument("--dataset-dir", default=str(DEFAULT_DATASET_DIR))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--api-base", default=os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1"))
    args = parser.parse_args()

    if not args.extractor and not args.all:
        parser.error("--extractor or --all is required")

    # Load env
    env_path = Path(args.env_file)
    if env_path.exists():
        for line in env_path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() not in os.environ and v.strip():
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

    api_key = os.environ.get("OPENAI_API_KEY", "")
    api_base = args.api_base
    if not api_base.endswith("/v1"):
        api_base = api_base.rstrip("/") + "/v1"

    dataset_dir = Path(args.dataset_dir)
    extractors = list(EXTRACTOR_CONFIGS.keys()) if args.all else [args.extractor]

    all_results: list[dict[str, Any]] = []
    for name in extractors:
        config = EXTRACTOR_CONFIGS[name]
        print(f"running {name}...", flush=True)
        result = asyncio.run(run_extractor_eval(
            name, config, dataset_dir,
            api_key=api_key,
            api_base=api_base,
            max_cases=args.max_cases,
            dry_run=args.dry_run,
        ))
        all_results.append(result)
        if result["status"] == "completed":
            print(f"  {name}: accuracy={result['accuracy']:.0%} ({result['cases']} cases, {result['total_ms']}ms)")

    report_path = write_report(all_results)
    print(json.dumps({
        "report": str(report_path),
        "summary": [{k: v for k, v in r.items() if k != "results"} for r in all_results],
    }, indent=2))

    if any(r.get("accuracy", 1.0) < 0.85 for r in all_results if r["status"] == "completed"):
        print("\n⚠️  Some extractors below 85% accuracy threshold")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
