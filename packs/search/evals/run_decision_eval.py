"""Agent-in-the-loop eval for the $search Step-1 routing decision.

Feeds each labeled query to a REAL agent (codex or claude CLI) together with the
decision rules extracted verbatim from the production SKILL.md (between the
`<!-- decision-rules:start/end -->` markers, so the eval can never drift from
what agents actually read), captures the decision JSON it returns, and scores
surface / backend / depth against the labels in decision/cases.json.

This replaces the deleted offline classifier eval (run_routing_eval.py /
route_query.py): the model IS the router now, so the eval must run the model.

Spawn patterns follow the in-repo precedents:
- codex: `codex exec -s read-only --skip-git-repo-check --ephemeral -o <tmp>`
  with the prompt on stdin (packs/search/primitives/deep_search/codex_judge.py)
- claude / custom: a `--command-template` with a `{prompt_path}` placeholder
  (packs/messages/primitives/harness_retarget_research/harness_retarget_research.py)

Usage:
  uv run --project . python packs/search/evals/run_decision_eval.py --harness codex
  uv run --project . python packs/search/evals/run_decision_eval.py \
      --harness template --command-template 'claude -p "Follow {prompt_path}; output only the JSON."'

codex uses subscription auth (no per-call cash); ~68 cases at concurrency 8
takes a few minutes. Results land in packs/search/evals/decision/report.json.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILL_PATH = ROOT / "packs/search/skills/search/SKILL.md"
CASES_PATH = Path(__file__).resolve().parent / "decision/cases.json"
REPORT_PATH = Path(__file__).resolve().parent / "decision/report.json"

RULES_START = "<!-- decision-rules:start -->"
RULES_END = "<!-- decision-rules:end -->"

FIELDS = ("surface", "backend", "depth")
ENUMS = {
    "surface": {"people", "company", "sql", "contacts"},
    "backend": {"powerset", "local"},
    "depth": {"fast", "deep"},
}
DEFAULT_ENV = {"local_db": True, "remote_creds": True}

CLAUDE_TEMPLATE = (
    'claude -p "Follow the instructions in the file at {prompt_path} exactly. '
    'Output ONLY the decision JSON object, nothing else."'
)


def extract_rules(skill_path: Path) -> str:
    text = skill_path.read_text(encoding="utf-8")
    if RULES_START not in text or RULES_END not in text:
        raise SystemExit(f"decision-rules markers not found in {skill_path}")
    return text.split(RULES_START, 1)[1].split(RULES_END, 1)[0].strip()


def build_prompt(rules: str, case: dict) -> str:
    env = {**DEFAULT_ENV, **(case.get("env") or {})}
    env_lines = (
        f"- POWERPACKS_LOCAL_SEARCH_DB / local DuckDB search index: {'present' if env['local_db'] else 'absent'}\n"
        f"- TurboPuffer/Powerset remote credentials: {'present' if env['remote_creds'] else 'absent'}"
    )
    return (
        "You are the $search router for Powerpacks. Apply the decision rules below to the query "
        "and output ONLY a JSON object of the shape "
        '{"surface": ..., "backend": ..., "depth": ..., "reason": "..."} — no prose, no markdown fence. '
        "If surface is not people, output depth as \"fast\".\n\n"
        f"Decision rules (verbatim from the $search skill):\n\n{rules}\n\n"
        f"Environment assumptions:\n{env_lines}\n\n"
        f"Query:\n<<<\n{case['query']}\n>>>"
    )


def extract_json(text: str) -> dict:
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    best: dict = {}
    for match in re.finditer(r"\{", text):
        depth = 0
        for end in range(match.start(), len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = json.loads(text[match.start() : end + 1])
                        if isinstance(candidate, dict) and len(candidate) > len(best):
                            best = candidate
                    except json.JSONDecodeError:
                        pass
                    break
    return best


def run_codex(prompt: str, effort: str, timeout: int) -> tuple[dict, str | None]:
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=True) as out:
        cmd = [
            "codex", "exec", "-s", "read-only", "--skip-git-repo-check", "--ephemeral",
            "-o", out.name, "-c", f'model_reasoning_effort="{effort}"',
        ]
        try:
            cp = subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ({}, "timeout")
        except OSError as e:
            return ({}, f"spawn_failed: {e}")
        if cp.returncode != 0:
            return ({}, f"codex_exit_{cp.returncode}: {(cp.stderr or cp.stdout or '').strip()[-500:]}")
        out.seek(0)
        parsed = extract_json(out.read())
        return (parsed, None if parsed else "empty_or_unparsable")


def run_template(prompt: str, template: str, workdir: Path, case_id: str, timeout: int) -> tuple[dict, str | None]:
    prompt_path = workdir / f"{case_id}.prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    # .replace, not .format: templates may carry literal braces (e.g. a stub echoing JSON)
    cmd = shlex.split(template.replace("{prompt_path}", shlex.quote(str(prompt_path))))
    try:
        cp = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ({}, "timeout")
    except OSError as e:
        return ({}, f"spawn_failed: {e}")
    if cp.returncode != 0:
        return ({}, f"exit_{cp.returncode}: {(cp.stderr or cp.stdout or '').strip()[-500:]}")
    parsed = extract_json(cp.stdout or "")
    return (parsed, None if parsed else "empty_or_unparsable")


def score(cases: list[dict], results: dict[str, tuple[dict, str | None]]) -> dict:
    strict_hits = 0
    lenient_hits = 0
    errors = 0
    field_totals = {f: 0 for f in FIELDS}
    field_hits = {f: 0 for f in FIELDS}
    confusion: dict[str, dict[str, dict[str, int]]] = {f: {} for f in FIELDS}
    misses = []
    for case in cases:
        decision, err = results[case["id"]]
        labeled = [f for f in FIELDS if case.get(f) is not None]
        strict_ok = err is None
        lenient_ok = err is None
        if err is not None:
            errors += 1
        for field in labeled:
            field_totals[field] += 1
            expected = case[field]
            got = str(decision.get(field, "")).lower() if err is None else "<error>"
            confusion[field].setdefault(expected, {})
            confusion[field][expected][got] = confusion[field][expected].get(got, 0) + 1
            if got == expected:
                field_hits[field] += 1
            else:
                strict_ok = False
                if got not in set(case.get(f"acceptable_{field}") or []):
                    lenient_ok = False
        if strict_ok:
            strict_hits += 1
        if lenient_ok:
            lenient_hits += 1
        else:
            misses.append({
                "id": case["id"],
                "query": case["query"][:120],
                "expected": {f: case.get(f) for f in FIELDS},
                "got": {f: decision.get(f) for f in FIELDS} if err is None else None,
                "reason": decision.get("reason") if err is None else None,
                "error": err,
            })
    n = len(cases)
    return {
        "cases": n,
        "errors": errors,
        "strict_accuracy": round(strict_hits / n, 4) if n else 0.0,
        "lenient_accuracy": round(lenient_hits / n, 4) if n else 0.0,
        "field_accuracy": {
            f: (round(field_hits[f] / field_totals[f], 4) if field_totals[f] else None) for f in FIELDS
        },
        "confusion": confusion,
        "misses": misses,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the $search decision eval against a real agent.")
    ap.add_argument("--harness", choices=("codex", "claude", "template", "auto"), default="auto")
    ap.add_argument("--command-template", default=None,
                    help="Command with a {prompt_path} placeholder (implies --harness template)")
    ap.add_argument("--cases", type=Path, default=CASES_PATH)
    ap.add_argument("--skill", type=Path, default=SKILL_PATH)
    ap.add_argument("--report", type=Path, default=REPORT_PATH)
    ap.add_argument("--reasoning-effort", default="low", help="codex model_reasoning_effort")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=180, help="seconds per case")
    ap.add_argument("--min-accuracy", type=float, default=0.0,
                    help="exit non-zero if strict accuracy falls below this")
    ap.add_argument("--only", action="append", default=None, help="run only these case ids (repeatable)")
    args = ap.parse_args()

    harness = "template" if args.command_template else args.harness
    if harness == "auto":
        harness = "codex" if shutil.which("codex") else "claude" if shutil.which("claude") else "template"
    if harness == "claude":
        args.command_template = CLAUDE_TEMPLATE
        harness = "template"
    if harness == "codex" and not shutil.which("codex"):
        raise SystemExit("codex CLI not found; use --harness claude or --command-template")
    if harness == "template" and not args.command_template:
        raise SystemExit("no agent CLI found; pass --command-template")

    rules = extract_rules(args.skill)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]
    if not cases:
        raise SystemExit("no cases selected")

    workdir = Path(tempfile.mkdtemp(prefix="decision-eval-"))

    def run_one(case: dict) -> tuple[str, tuple[dict, str | None]]:
        prompt = build_prompt(rules, case)
        if harness == "codex":
            return (case["id"], run_codex(prompt, args.reasoning_effort, args.timeout))
        return (case["id"], run_template(prompt, args.command_template, workdir, case["id"], args.timeout))

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        results = dict(ex.map(run_one, cases))

    report = score(cases, results)
    report["harness"] = "codex" if harness == "codex" else (args.command_template or "template")
    report["reasoning_effort"] = args.reasoning_effort if harness == "codex" else None
    report["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({k: report[k] for k in ("cases", "errors", "strict_accuracy", "lenient_accuracy", "field_accuracy")}, indent=2))
    for miss in report["misses"]:
        print(f"MISS {miss['id']}: expected={miss['expected']} got={miss['got']} error={miss['error']}")
    print(f"report: {args.report}")
    if report["strict_accuracy"] < args.min_accuracy:
        raise SystemExit(f"strict_accuracy {report['strict_accuracy']} < min {args.min_accuracy}")


if __name__ == "__main__":
    main()
