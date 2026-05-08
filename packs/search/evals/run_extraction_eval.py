#!/usr/bin/env python3
"""Evaluate extract-search-query skill outputs without running retrieval.

Dry-run writes prompts only. Live mode invokes a host agent command that must
print one JSON object or write it to {output_json}.

Example live command:
  python packs/search/evals/run_extraction_eval.py --live \
    --agent-command 'codex exec < {prompt_file}'
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
CASES = ROOT / "packs/search/evals/extract-search-query/cases.json"
OUT_DIR = ROOT / ".powerpacks/skill-evals/extract-search-query"
SKILL = ROOT / "packs/search/skills/extract-search-query/SKILL.md"
SCHEMA = ROOT / "packs/search/schemas/role-search-filters.schema.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def select_cases(cases: list[dict[str, Any]], pattern: str | None, max_cases: int | None) -> list[dict[str, Any]]:
    out = cases
    if pattern:
        rx = re.compile(pattern)
        out = [case for case in out if rx.search(str(case.get("id", "")))]
    if max_cases:
        out = out[:max_cases]
    return out


def build_prompt(case: dict[str, Any]) -> str:
    return f"""Use the Powerpacks `extract-search-query` skill to decompose this search query.

Skill file: {SKILL}
Schema: {SCHEMA}

Return only one JSON object. Do not include markdown fences, commentary, retrieval results, candidate IDs, or tool logs.

Query: {case['query']}

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
        raise ValueError("empty agent output")
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise TypeError("expected JSON object")
    return value


def get_path(value: dict[str, Any], dotted: str) -> tuple[bool, Any]:
    cur: Any = value
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def norm(value: Any) -> str:
    return str(value).strip().lower()


def match_condition(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if expected.get("exists") is True:
            return actual is not None
        if expected.get("exists") is False:
            return actual is None
        if expected.get("non_empty") is True:
            return bool(actual)
        if "contains" in expected:
            needle = expected["contains"]
            if isinstance(actual, list):
                return needle in actual
            return str(needle) in str(actual)
        if "contains_case_insensitive" in expected:
            needle = norm(expected["contains_case_insensitive"])
            if isinstance(actual, list):
                return any(norm(item) == needle or needle in norm(item) for item in actual)
            return needle in norm(actual)
        if "one_of" in expected:
            return actual in expected["one_of"]
    return actual == expected


def validate(case: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    missing: list[str] = []
    forbidden: list[str] = []
    if not isinstance(output.get("role_search_filters"), dict):
        missing.append("role_search_filters")
    for path, expected in (case.get("expect") or {}).items():
        exists, actual = get_path(output, path)
        if not exists or not match_condition(actual, expected):
            missing.append(f"{path} expected {expected!r} got {actual!r}")
    for path, rejected in (case.get("reject") or {}).items():
        exists, actual = get_path(output, path)
        if exists and match_condition(actual, rejected):
            forbidden.append(f"{path} rejected {rejected!r} got {actual!r}")
    return {"ok": not missing and not forbidden, "missing": missing, "forbidden": forbidden}


def invoke_agent(command_template: str, prompt_file: Path, output_json: Path, case: dict[str, Any], timeout: int) -> dict[str, Any]:
    command = command_template.format(
        prompt_file=shlex.quote(str(prompt_file)),
        output_json=shlex.quote(str(output_json)),
        case_id=shlex.quote(str(case["id"])),
        query=shlex.quote(str(case["query"])),
    )
    completed = subprocess.run(command, shell=True, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    raw_log = output_json.with_suffix(".raw.log")
    raw_log.write_text(
        "$ " + command + "\n\n"
        + f"exit={completed.returncode}\n\nSTDOUT:\n{completed.stdout}\n\nSTDERR:\n{completed.stderr}\n"
    )
    if completed.returncode != 0:
        raise RuntimeError(f"agent failed rc={completed.returncode}; see {raw_log}")
    if output_json.exists() and output_json.stat().st_size:
        return load_json(output_json)
    output = extract_json(completed.stdout)
    write_json(output_json, output)
    return output


def report_markdown(results: list[dict[str, Any]], live: bool) -> str:
    passed = sum(1 for row in results if row.get("validation", {}).get("ok"))
    lines = [
        "# Extraction eval: `extract-search-query`",
        "",
        f"- generated: `{now_iso()}`",
        f"- mode: `{'live' if live else 'dry-run'}`",
        f"- cases: {passed}/{len(results)} passed" if live else f"- prompts: {len(results)} written",
        "",
        "| case | ok | notes |",
        "| --- | --- | --- |",
    ]
    for row in results:
        val = row.get("validation") or {}
        ok = "✅" if val.get("ok") else ("—" if not live else "❌")
        notes = "; ".join((val.get("missing") or []) + (val.get("forbidden") or [])) or row.get("prompt", "")
        notes = notes.replace("|", "\\|")
        lines.append(f"| `{row['id']}` | {ok} | {notes} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate extract-search-query outputs")
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--case", help="Regex over case ids")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--live", action="store_true", help="Invoke agent command; costs model spend")
    parser.add_argument("--agent-command", help="Shell template with {prompt_file}, {output_json}, {case_id}, {query}")
    parser.add_argument("--timeout-sec", type=int, default=300)
    args = parser.parse_args()

    cases = select_cases(load_json(args.cases), args.case, args.max_cases)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for case in cases:
        cid = str(case["id"])
        prompt_file = args.out_dir / f"{cid}.prompt.txt"
        output_json = args.out_dir / f"{cid}.output.json"
        prompt_file.write_text(build_prompt(case))
        row: dict[str, Any] = {"id": cid, "query": case["query"], "prompt": str(prompt_file)}
        if args.live:
            if not args.agent_command:
                parser.error("--agent-command is required with --live")
            try:
                output = invoke_agent(args.agent_command, prompt_file, output_json, case, args.timeout_sec)
                row["output"] = str(output_json)
                row["validation"] = validate(case, output)
            except Exception as exc:
                row["validation"] = {"ok": False, "missing": [str(exc)], "forbidden": []}
        results.append(row)

    write_json(args.out_dir / "results.json", {"live": args.live, "results": results})
    report = args.out_dir / "report.md"
    report.write_text(report_markdown(results, args.live))
    print(json.dumps({"report": str(report), "results": results}, indent=2, sort_keys=True))
    return 0 if not args.live or all(row.get("validation", {}).get("ok") for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
