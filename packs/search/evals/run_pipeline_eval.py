#!/usr/bin/env python3
"""Run recall cases through expand_search_request + search_network_pipeline.

This harness tests the actual product flow:

1. The parallel `expand_search_request` primitive produces JSON.
2. The JSON is fed to `search_network_pipeline.py run --search-only` (or
   `--execute-approved` when LLM rerank is desired).
3. Recall is checked against expected person IDs from the YAML case.

The pipeline runs in direct mode — no slicing, no strategy loop, no approval
gate. This tests filter decomposition and retrieval correctness.

Environment variables (also loadable from .env):
  POWERPACKS_PIPELINE_SKIP_LLM   Set to "true" / "1" to use --search-only and
                                  skip LLM filter/rerank. Default: true.
  POWERPACKS_PIPELINE_SET_ID     Override the set_id injected into every eval
                                  payload. Use to scope evals to an org set
                                  instead of the personal default.

This intentionally avoids harness-composed query expansion. Query expansion
quality should come from the same parallel primitive used by `search-network
prepare`.
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

import yaml


ROOT = Path(__file__).resolve().parents[3]
SEARCH_ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = SEARCH_ROOT / "primitives"
PIPELINE = PRIMITIVES / "search_network_pipeline" / "search_network_pipeline.py"
EXPAND = PRIMITIVES / "expand_search_request" / "expand_search_request.py"
DEFAULT_APP_DIR = Path(os.environ.get("POWERPACKS_APP_DIR", str(ROOT)))
DEFAULT_RECALL_DIR = Path(os.environ.get("POWERPACKS_RECALL_DIR", str(DEFAULT_APP_DIR / "tests" / "recall")))
REPORT_PATH = SEARCH_ROOT / "evals" / "pipeline_eval.md"
RESULT_LIMIT_CAP = 10000


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_env(env_file: str) -> dict[str, str]:
    """Load key=value pairs from an env file without overriding existing env."""
    env = dict(os.environ)
    for candidate in [Path(env_file), ROOT / env_file]:
        if candidate.exists():
            for line in candidate.read_text(errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k not in env and v.strip():
                    env[k] = v.strip().strip('"').strip("'")
            break
    return env


def skip_llm(env: dict[str, str]) -> bool:
    """Check POWERPACKS_PIPELINE_SKIP_LLM from env. Default True."""
    val = env.get("POWERPACKS_PIPELINE_SKIP_LLM", "true").strip().lower()
    return val in ("true", "1", "yes")


def base_uuid(value: str) -> str:
    parts = str(value).split("-")
    if len(parts) == 6 and parts[5].isdigit():
        return "-".join(parts[:5])
    return str(value)


def uuid_version(value: str) -> str | None:
    raw = base_uuid(value).lower()
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw):
        return None
    return raw.split("-")[2][0]


def bucket_for(relpath: str) -> str:
    if relpath.startswith("staging/"):
        return "staging"
    stem = Path(relpath).stem
    for prefix in [
        "date_range", "company", "education", "founders", "funding",
        "industry", "investor", "leaders", "location", "mixed", "role",
        "skills", "social",
    ]:
        if stem.startswith(prefix + "_") or stem == prefix:
            return prefix
    return stem.split("_", 1)[0]


@dataclass
class CaseMeta:
    path: Path
    relpath: str
    bucket: str
    query: str
    limit: int
    expected_count: int
    min_recall: float
    expected_ids: list[str]
    ignored_v4_ids: list[str]
    data: dict[str, Any]


def load_case(path: Path, recall_dir: Path) -> CaseMeta:
    data = yaml.safe_load(path.read_text()) or {}
    relpath = str(path.relative_to(recall_dir))
    expected_raw = [str(v) for v in data.get("expected_person_ids") or []]
    expected_ids = [base_uuid(v) for v in expected_raw if uuid_version(v) != "4"]
    ignored_v4_ids = [base_uuid(v) for v in expected_raw if uuid_version(v) == "4"]
    return CaseMeta(
        path=path,
        relpath=relpath,
        bucket=bucket_for(relpath),
        query=str(data.get("query") or Path(relpath).stem.replace("_", " ")),
        limit=RESULT_LIMIT_CAP,  # Ignore YAML limit — use limit_cap for eval consistency
        expected_count=int(data.get("expected_count") or 0),
        min_recall=float(data.get("min_recall") or 0.5),
        expected_ids=list(dict.fromkeys(expected_ids)),
        ignored_v4_ids=list(dict.fromkeys(ignored_v4_ids)),
        data=data,
    )


def select_cases(
    recall_dir: Path,
    bucket: str | None,
    case_glob: str | None,
    include_staging: bool,
) -> list[CaseMeta]:
    paths = sorted(recall_dir.rglob("*.yaml"))
    cases = [load_case(p, recall_dir) for p in paths]
    if not include_staging:
        cases = [c for c in cases if c.bucket != "staging"]
    if bucket:
        cases = [c for c in cases if c.bucket == bucket]
    if case_glob:
        rx = re.compile(case_glob)
        cases = [c for c in cases if rx.search(c.relpath)]
    return cases


def case_id(meta: CaseMeta) -> str:
    return Path(meta.relpath).with_suffix("").as_posix().replace("/", "__")


# ---------------------------------------------------------------------------
# Query extraction via expand_search_request primitive (OpenAI direct)
# ---------------------------------------------------------------------------

def expand_query(
    query: str,
    output_json: Path,
    *,
    env_file: str,
    model: str | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """Call the expand_search_request primitive to decompose a query."""
    cmd = [
        sys.executable, str(EXPAND),
        "--query", query,
        "--env-file", env_file,
        "--timeout", str(timeout),
    ]
    if model:
        cmd.extend(["--model", model])
    completed = subprocess.run(
        cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout + 30,
    )
    log_path = output_json.with_suffix(".expand.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        f"$ {' '.join(cmd)}\nexit={completed.returncode}\n\n"
        f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"expand_search_request failed ({completed.returncode}); see {log_path}"
        )
    result = json.loads(completed.stdout)
    if result.get("status") != "completed":
        raise RuntimeError(f"expand_search_request status: {result.get('status')}: {result.get('error')}")
    # Extract the decomposition (strip primitive metadata)
    decomposition = {
        k: v for k, v in result.items()
        if k in ("intent_type", "source_type", "normalized_query", "vertical", "role_search_filters", "notes")
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(decomposition, indent=2, sort_keys=True) + "\n")
    return decomposition


# ---------------------------------------------------------------------------
# Pipeline execution
# ---------------------------------------------------------------------------

def run_pipeline(
    payload_path: Path,
    query: str,
    *,
    env_file: str,
    limit: int,
    do_skip_llm: bool,
    ledger_path: Path | None = None,
    timeout: int = 900,
) -> dict[str, Any]:
    cmd = [
        sys.executable, str(PIPELINE), "run",
        "--query", query,
        "--payload-json", str(payload_path),
        "--env-file", env_file,
        "--limit", str(limit),
        "--top-k", str(max(1000, limit)),
    ]
    if ledger_path:
        cmd.extend(["--ledger", str(ledger_path)])
    if do_skip_llm:
        cmd.append("--search-only")
    else:
        cmd.append("--execute-approved")

    env = dict(os.environ)
    completed = subprocess.run(
        cmd, cwd=ROOT, env=env, text=True, capture_output=True, timeout=timeout,
    )
    # Parse all JSON objects from stdout
    objects: list[dict[str, Any]] = []
    dec = json.JSONDecoder()
    i = 0
    stdout = completed.stdout or ""
    while i < len(stdout):
        while i < len(stdout) and stdout[i].isspace():
            i += 1
        if i >= len(stdout):
            break
        try:
            obj, end = dec.raw_decode(stdout, i)
            objects.append(obj)
            i = end
        except json.JSONDecodeError:
            j = stdout.find("{", i + 1)
            if j < 0:
                break
            i = j

    result = objects[-1] if objects else {}
    if completed.returncode != 0 and not result:
        raise RuntimeError(
            f"pipeline failed rc={completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '')[-800:]}"
        )
    return result


# ---------------------------------------------------------------------------
# Recall check
# ---------------------------------------------------------------------------

def check_recall(
    pipeline_result: dict[str, Any],
    meta: CaseMeta,
    state_path: str | None,
) -> dict[str, Any]:
    # Read candidate IDs from state's execute_role_search step
    candidate_ids: set[str] = set()
    if state_path and Path(state_path).exists():
        state = json.loads(Path(state_path).read_text())
        for step in reversed(state.get("steps", [])):
            if step.get("id") == "execute_role_search":
                raw_ids = (step.get("output") or {}).get("candidate_ids") or []
                candidate_ids = {base_uuid(pid) for pid in raw_ids}
                break

    expected_ids = set(meta.expected_ids)
    hits = sorted(pid for pid in expected_ids if pid in candidate_ids)
    recall = (len(hits) / len(expected_ids)) if expected_ids else None

    if expected_ids:
        passed = recall is not None and recall >= meta.min_recall
    else:
        # Fall back to count-based check
        returned = (pipeline_result.get("summary") or {}).get("returned_people") or 0
        passed = int(returned) >= meta.expected_count

    return {
        "hit_count": len(hits),
        "recall": recall,
        "passed": passed,
        "missed_ids": sorted(expected_ids - set(hits))[:20],
        "returned_people": (pipeline_result.get("summary") or {}).get("returned_people"),
        "hydrated": (pipeline_result.get("summary") or {}).get("hydrated"),
    }


# ---------------------------------------------------------------------------
# Single case runner
# ---------------------------------------------------------------------------

def run_case(
    meta: CaseMeta,
    *,
    env_file: str,
    env: dict[str, str],
    extraction_dir: Path,
    limit_cap: int,
    do_skip_llm: bool,
    set_id: str | None = None,
    expand_model: str | None = None,
) -> dict[str, Any]:
    cid = case_id(meta)
    result: dict[str, Any] = {
        "id": cid,
        "source": meta.relpath,
        "bucket": meta.bucket,
        "query": meta.query,
        "expected_id_count": len(meta.expected_ids),
        "ignored_v4_count": len(meta.ignored_v4_ids),
        "expected_count": meta.expected_count,
        "skip_llm": do_skip_llm,
    }

    if not meta.expected_ids and not meta.expected_count:
        result.update({"status": "ignored", "reason": "no comparable expected IDs or count"})
        return result

    # 1. Query extraction through the parallel primitive.
    output_json = extraction_dir / f"{cid}.extracted.json"
    extraction_dir.mkdir(parents=True, exist_ok=True)
    decomposition = expand_query(
        meta.query, output_json, env_file=env_file, model=expand_model,
    )
    result["extraction"] = str(output_json)

    # 2. Write payload for pipeline, injecting set_id if provided
    if set_id:
        filters = decomposition.get("role_search_filters")
        if isinstance(filters, dict):
            filters["set_id"] = set_id
        else:
            decomposition["set_id"] = set_id
    payload_path = extraction_dir / f"{cid}.payload.json"
    payload_path.write_text(json.dumps(decomposition, indent=2, sort_keys=True) + "\n")

    # 3. Run pipeline (each case gets its own ledger to avoid cross-contamination)
    ledger_path = extraction_dir / f"{cid}.ledger.json"
    pipeline_out = run_pipeline(
        payload_path,
        meta.query,
        env_file=env_file,
        limit=limit_cap,
        do_skip_llm=do_skip_llm,
        ledger_path=ledger_path,
    )
    state_path = pipeline_out.get("state")
    result["state"] = state_path
    result["ledger"] = pipeline_out.get("ledger")
    result["pipeline_status"] = pipeline_out.get("status")
    result["summary"] = pipeline_out.get("summary")
    result["artifacts"] = pipeline_out.get("artifacts")

    if pipeline_out.get("status") not in ("completed",):
        result["status"] = "fail"
        result["reason"] = f"pipeline status: {pipeline_out.get('status')}"
        return result

    # 4. Recall check
    recall = check_recall(pipeline_out, meta, state_path)
    result.update(recall)
    result["status"] = "pass" if recall["passed"] else "fail"
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_report(results: list[dict[str, Any]]) -> None:
    now = now_iso()
    buckets = sorted({r["bucket"] for r in results})
    lines = [
        "# Pipeline Eval",
        "",
        f"Last run: `{now}`",
        "",
        "Scope: recall YAMLs → expand_search_request → search_network_pipeline (direct mode, no slicing).",
        "",
        f"LLM rerank skipped: `{results[0].get('skip_llm', True) if results else True}`",
        "",
        "| Bucket | Pass | Fail | Ignored | Cases |",
        "|---|---:|---:|---:|---:|",
    ]
    for bucket in buckets:
        rows = [r for r in results if r["bucket"] == bucket]
        lines.append(
            f"| {bucket} | "
            f"{sum(1 for r in rows if r['status'] == 'pass')} | "
            f"{sum(1 for r in rows if r['status'] == 'fail')} | "
            f"{sum(1 for r in rows if r['status'] == 'ignored')} | "
            f"{len(rows)} |"
        )
    lines.extend([
        "",
        "| Case | Bucket | Status | Returned | Hydrated | Hits/Expected | Recall | Notes |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ])
    for r in results:
        recall = r.get("recall")
        recall_text = "" if recall is None else f"{recall:.0%}"
        expected = r.get("expected_id_count") or 0
        hits = r.get("hit_count") or 0
        note = r.get("reason") or ""
        if r.get("missed_ids"):
            note = f"missed {len(r['missed_ids'])}+ expected"
        lines.append(
            f"| {r['source']} | {r['bucket']} | {r['status']} "
            f"| {r.get('returned_people', '')} | {r.get('hydrated', '')} "
            f"| {hits}/{expected} | {recall_text} | {note} |"
        )
    lines.append("")
    REPORT_PATH.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run recall cases through expand_search_request + search_network_pipeline"
    )
    parser.add_argument("--recall-dir", default=str(DEFAULT_RECALL_DIR))
    parser.add_argument("--bucket", help="Filter to a single bucket (founders, date_range, education, ...)")
    parser.add_argument("--case-glob", help="Regex filter on case relpath")
    parser.add_argument("--include-staging", action="store_true")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--limit-cap", type=int, default=RESULT_LIMIT_CAP)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--skip-llm",
        default=None,
        help="Skip LLM filter/rerank (true/false). Default reads POWERPACKS_PIPELINE_SKIP_LLM from env, falling back to true.",
    )
    parser.add_argument(
        "--set-id",
        default=None,
        help="Override set_id for all eval cases. Default reads POWERPACKS_PIPELINE_SET_ID from env.",
    )
    parser.add_argument(
        "--expand-model",
        default=None,
        help="Model for expand_search_request primitive. Default reads EXPAND_SEARCH_MODEL from env, falling back to gpt-5.4-mini.",
    )
    parser.add_argument("--dry-run", action="store_true", help="List selected queries without invoking expansion or pipeline.")
    parser.add_argument("--list", action="store_true", help="List matching cases and exit.")
    args = parser.parse_args()

    recall_dir = Path(args.recall_dir)
    cases = select_cases(recall_dir, args.bucket, args.case_glob, args.include_staging)
    if args.max_cases:
        cases = cases[: args.max_cases]

    if args.list:
        print(json.dumps(
            [{"id": case_id(c), "bucket": c.bucket, "query": c.query, "expected_ids": len(c.expected_ids)} for c in cases],
            indent=2,
        ))
        return

    env = load_env(args.env_file)

    # Resolve skip_llm: CLI flag > env var > default true
    if args.skip_llm is not None:
        do_skip_llm = args.skip_llm.lower() in ("true", "1", "yes")
    else:
        do_skip_llm = skip_llm(env)

    # Resolve set_id: CLI flag > env var > None (let pipeline use its default)
    eval_set_id = args.set_id or env.get("POWERPACKS_PIPELINE_SET_ID") or None

    extraction_dir = ROOT / ".powerpacks" / "pipeline-eval" / "extractions"
    extraction_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        queries: list[dict[str, Any]] = []
        for meta in cases:
            cid = case_id(meta)
            queries.append({"id": cid, "query": meta.query, "output_json": str(extraction_dir / f"{cid}.extracted.json")})
        print(json.dumps({"mode": "dry-run", "queries": queries, "skip_llm": do_skip_llm}, indent=2))
        return

    results: list[dict[str, Any]] = []
    for meta in cases:
        print(f"running {meta.relpath} ...", flush=True)
        try:
            results.append(run_case(
                meta,
                env_file=args.env_file,
                env=env,
                extraction_dir=extraction_dir,
                limit_cap=args.limit_cap,
                do_skip_llm=do_skip_llm,
                set_id=eval_set_id,
                expand_model=args.expand_model,
            ))
        except Exception as exc:
            results.append({
                "id": case_id(meta),
                "source": meta.relpath,
                "bucket": meta.bucket,
                "query": meta.query,
                "expected_id_count": len(meta.expected_ids),
                "ignored_v4_count": len(meta.ignored_v4_ids),
                "expected_count": meta.expected_count,
                "skip_llm": do_skip_llm,
                "status": "fail",
                "reason": str(exc),
            })

    write_report(results)
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    ignored = sum(1 for r in results if r["status"] == "ignored")
    print(json.dumps({
        "report": str(REPORT_PATH),
        "passed": passed,
        "failed": failed,
        "ignored": ignored,
        "total": len(results),
        "skip_llm": do_skip_llm,
        "results": results,
    }, indent=2, sort_keys=True))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
