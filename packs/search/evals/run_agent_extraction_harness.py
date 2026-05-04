#!/usr/bin/env python3
"""Run recall cases through a host agent extraction step, then primitives.

This harness evaluates the boundary the product actually cares about:

1. A host agent uses the `extract-search-query` skill to produce JSON.
2. The JSON is saved as an artifact.
3. The existing primitive recall harness consumes that JSON.

Pass `--agent-command` as a shell command template. Supported placeholders:

- `{prompt_file}`: file containing the extraction prompt
- `{output_json}`: expected JSON artifact path
- `{case_id}`: stable recall case id
- `{query}`: shell-quoted query text

The command must print the extracted JSON to stdout, or write it to
`{output_json}`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from run_recall_parity import (
    DEFAULT_APP_DIR,
    DEFAULT_RECALL_DIR,
    REPORT_PATH,
    ROOT,
    CaseMeta,
    run_case,
    select_cases,
    write_report,
)


EXTRACTION_DIRNAME = "agent-extractions"


def case_id(meta: CaseMeta) -> str:
    return Path(meta.relpath).with_suffix("").as_posix().replace("/", "__")


def build_prompt(meta: CaseMeta) -> str:
    skill_path = ROOT / "skills" / "extract-search-query" / "SKILL.md"
    return f"""Use the Powerpacks `extract-search-query` skill to decompose this recall query.

Skill file: {skill_path}
Schemas:
- {ROOT / "schemas" / "decomposed-query.schema.json"}
- {ROOT / "schemas" / "role-search-filters.schema.json"}

Return only one JSON object. Do not include markdown fences, commentary, retrieval results, or candidate IDs.

Recall case: {meta.relpath}
Query: {meta.query}

Expected output shape:
{{
  "intent_type": "role_search",
  "source_type": "query",
  "normalized_query": "...",
  "vertical": "people_by_role",
  "role_search_filters": {{
    "semantic_query": "...",
    "bm25_queries": []
  }},
  "notes": []
}}
"""


def extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("agent returned empty output")
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise TypeError("agent extraction output must be a JSON object")
    return value


def validate_decomposition(value: dict[str, Any]) -> None:
    payload = value.get("role_search_filters")
    if not isinstance(payload, dict):
        raise ValueError("missing role_search_filters object")
    semantic_query = payload.get("semantic_query")
    if not isinstance(semantic_query, str) or len(semantic_query) < 80:
        raise ValueError("role_search_filters.semantic_query must be prose with at least 80 characters")
    if value.get("intent_type") not in {None, "role_search"}:
        raise ValueError("intent_type must be role_search")
    if value.get("vertical") not in {None, "people_by_role"}:
        raise ValueError("vertical must be people_by_role")


def invoke_agent(command_template: str, prompt_file: Path, output_json: Path, meta: CaseMeta) -> dict[str, Any]:
    command = command_template.format(
        prompt_file=shlex.quote(str(prompt_file)),
        output_json=shlex.quote(str(output_json)),
        case_id=shlex.quote(case_id(meta)),
        query=shlex.quote(meta.query),
    )
    completed = subprocess.run(command, shell=True, text=True, capture_output=True, cwd=ROOT)
    raw_log = output_json.with_suffix(".raw.log")
    raw_log.write_text(
        "$ " + command + "\n\n"
        + f"exit={completed.returncode}\n\n"
        + "STDOUT:\n" + completed.stdout
        + "\nSTDERR:\n" + completed.stderr
    )
    if completed.returncode != 0:
        raise RuntimeError(f"agent command failed ({completed.returncode}); see {raw_log}")

    if output_json.exists() and output_json.stat().st_size:
        extracted = json.loads(output_json.read_text())
    else:
        extracted = extract_json(completed.stdout)
        output_json.write_text(json.dumps(extracted, indent=2, sort_keys=True) + "\n")
    validate_decomposition(extracted)
    return extracted


def main() -> None:
    parser = argparse.ArgumentParser(description="Run recall cases through agent extraction plus Powerpacks primitives")
    parser.add_argument("--agent-command", help="Shell command template that runs the host agent extraction.")
    parser.add_argument("--app-dir", default=str(DEFAULT_APP_DIR))
    parser.add_argument("--recall-dir", default=str(DEFAULT_RECALL_DIR))
    parser.add_argument("--bucket")
    parser.add_argument("--case-glob")
    parser.add_argument("--include-staging", action="store_true")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--limit-cap", type=int, default=1000)
    parser.add_argument("--env-file", default=".env", help="Env file for retrieval primitives, relative to app-dir unless absolute.")
    parser.add_argument("--dry-run", action="store_true", help="Write prompts but do not invoke the agent or primitives.")
    args = parser.parse_args()

    if not args.agent_command and not args.dry_run:
        parser.error("--agent-command is required unless --dry-run is set")

    app_dir = Path(args.app_dir)
    recall_dir = Path(args.recall_dir)
    cases = select_cases(recall_dir, args.bucket, args.case_glob, args.include_staging)
    if args.max_cases:
        cases = cases[: args.max_cases]

    run_dir = app_dir / ".powerpacks" / "runs" / "agent-recall-parity"
    log_dir = app_dir / ".powerpacks" / "runs" / "agent-recall-parity-logs"
    extraction_dir = run_dir / EXTRACTION_DIRNAME
    extraction_dir.mkdir(parents=True, exist_ok=True)

    prompts: list[str] = []
    results: list[dict[str, Any]] = []
    env = os.environ.copy()
    for meta in cases:
        cid = case_id(meta)
        prompt_file = extraction_dir / f"{cid}.prompt.txt"
        output_json = extraction_dir / f"{cid}.extracted.json"
        prompt_file.write_text(build_prompt(meta))
        prompts.append(str(prompt_file))
        if args.dry_run:
            continue

        print(f"extracting {meta.relpath}...", flush=True)
        try:
            decomposition = invoke_agent(args.agent_command, prompt_file, output_json, meta)
            results.append(
                run_case(
                    app_dir,
                    meta,
                    env,
                    run_dir,
                    log_dir,
                    args.limit_cap,
                    args.env_file,
                    decomposition=decomposition,
                    decomposition_reason="Agent extraction harness uses extract-search-query output, then primitive count, retrieval, hydration, and export.",
                )
            )
        except Exception as exc:
            results.append({
                "id": cid,
                "source": meta.relpath,
                "bucket": meta.bucket,
                "query": meta.query,
                "expected_id_count": len(meta.expected_ids),
                "ignored_v4_count": len(meta.ignored_v4_ids),
                "expected_count": meta.expected_count,
                "status": "fail",
                "reason": str(exc),
                "extraction": str(output_json),
            })

    if args.dry_run:
        print(json.dumps({"prompts": prompts}, indent=2))
        return

    write_report(results, app_dir, run_dir, log_dir)
    print(json.dumps({"report": str(REPORT_PATH), "results": results}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
