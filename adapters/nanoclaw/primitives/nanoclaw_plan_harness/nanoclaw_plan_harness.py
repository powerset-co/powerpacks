#!/usr/bin/env python3
"""Plan-only eval harness for NanoClaw + Powerpacks."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def default_powerpacks_root() -> Path:
    configured = os.environ.get("POWERPACKS_ROOT")
    if configured:
        return Path(configured)
    for parent in Path(__file__).resolve().parents:
        if (parent / "schemas").is_dir() and (parent / "contracts").is_dir():
            return parent
    return Path(__file__).resolve().parents[2]


POWERPACKS_ROOT = default_powerpacks_root()
DEFAULT_CASES = POWERPACKS_ROOT / "evals" / "search-network-plan" / "cases.json"
DEFAULT_OUT_DIR = POWERPACKS_ROOT / ".powerpacks" / "plan-evals"
CONTAINER_POWERPACKS_ROOT = Path("/workspace/extra/powerpacks")
PLAN_PRIMITIVES = [
    "task_state",
    "expand_search_request",
    "plan_adjacency_search",
    "decide_search_strategy",
    "count_candidates",
    "execute_role_search",
    "generate_search_slices",
    "execute_search_slice",
    "merge_candidate_frontier",
    "assess_frontier",
    "plan_candidate_review",
    "hydrate_people",
    "llm_filter_candidates",
    "persist_search_results",
    "refine_search_results",
]


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "case"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def container_path_for(host_path: Path, nanoclaw_dir: Path) -> Path:
    try:
        relative = host_path.resolve().relative_to((nanoclaw_dir / "powerpacks").resolve())
    except ValueError:
        return host_path
    return CONTAINER_POWERPACKS_ROOT / relative


def build_prompt(case: dict[str, Any], out_dir: Path, nanoclaw_dir: Path) -> str:
    query = str(case["query"])
    state_out = out_dir / f"{case['id']}.task.json"
    container_state_out = container_path_for(state_out, nanoclaw_dir)
    primitive_list = "\n".join(f"- `{name}`" for name in PLAN_PRIMITIVES)
    required_terms = ", ".join(f"`{term}`" for term in case.get("must_include", []))
    forbidden_terms = ", ".join(f"`{term}`" for term in case.get("must_not_include", []))
    return f"""Plan-only Powerpacks eval.

User query:
/search-network {query}

Powerpacks source of truth inside NanoClaw runtime:
- skills: {CONTAINER_POWERPACKS_ROOT}/skills/search-network/SKILL.md
- contracts: {CONTAINER_POWERPACKS_ROOT}/contracts/
- schemas: {CONTAINER_POWERPACKS_ROOT}/schemas/
- task template: {CONTAINER_POWERPACKS_ROOT}/tasks/search-network.task.json

Available Powerpacks primitive names. Use these exact names, not invented helper names:
{primitive_list}

Instructions:
- Do not execute TurboPuffer queries.
- Do not hydrate people.
- Do not call OpenAI/LLM filtering.
- Do not run expensive scoring.
- Do not do company-signal or summary generation.
- Do inspect Powerpacks docs/contracts if useful.
- Produce a concrete search plan and decision trace only.
- If you create task state, create it at this in-runtime path: {container_state_out}
- If you record task state, record only plan/decomposition/strategy placeholder steps. Do not record retrieval results.
- If the runtime path is not writable, still provide the exact task_state command you would run.
- Every hard filter/prefilter must use exact fields from `{CONTAINER_POWERPACKS_ROOT}/contracts/turbopuffer/people.namespace.json`.
- Do not use invented filter fields such as `current_role_category`, `employment_status`, `company_size`, or `profile updated`.
- End the plan by requesting approval for the next real step. Do not continue
  execution in this eval unless approval/yolo is explicitly provided in a later
  message.
- Mention the exact approval choices: approve, yolo, or request changes.
- The eval expects these terms to appear when applicable: {required_terms or "(none)"}
- The eval fails if these terms appear: {forbidden_terms or "(none)"}

Required response shape:
1. State whether this is direct, count-first, or sliced.
2. List primitives you would call in order.
3. List hard filters and prefilters.
4. List slice knobs if slicing might be needed.
5. List what feedback would cause the next step to change.
6. List files/artifacts created, if any.
7. State the proposed next step and approval choices.
"""


def load_cases(path: Path, selected: str | None) -> list[dict[str, Any]]:
    cases = read_json(path)
    if not isinstance(cases, list):
        raise SystemExit(f"cases file must be a JSON array: {path}")
    if selected:
        cases = [case for case in cases if case.get("id") == selected]
        if not cases:
            raise SystemExit(f"case not found: {selected}")
    for case in cases:
        if not case.get("id") or not case.get("query"):
            raise SystemExit("each case requires id and query")
    return cases


def send_to_nanoclaw(nanoclaw_dir: Path, thread_id: str, prompt: str) -> subprocess.CompletedProcess[str]:
    command = [
        "pnpm",
        "--silent",
        "-C",
        str(nanoclaw_dir),
        "exec",
        "tsx",
        "scripts/chat-threaded.ts",
        "--thread",
        thread_id,
        prompt,
    ]
    env = os.environ.copy()
    env["POWERPACKS_CHAT_SEND_ONLY"] = "1"
    env.setdefault("POWERPACKS_CHAT_TOTAL_TIMEOUT_MS", "60000")
    return subprocess.run(command, text=True, capture_output=True, timeout=75, check=False, env=env)


def db_rows(path: Path, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    if not path.exists():
        return []
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return list(con.execute(query, params))
    finally:
        con.close()


def latest_session(nanoclaw_dir: Path, thread_id: str) -> dict[str, Any] | None:
    rows = db_rows(
        nanoclaw_dir / "data" / "v2.db",
        """
        SELECT id, agent_group_id, messaging_group_id, thread_id, status, container_status, created_at, last_active
        FROM sessions
        WHERE thread_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (thread_id,),
    )
    return dict(rows[0]) if rows else None


def heartbeat_age_seconds(nanoclaw_dir: Path, session: dict[str, Any]) -> float | None:
    heartbeat = (
        nanoclaw_dir
        / "data"
        / "v2-sessions"
        / str(session["agent_group_id"])
        / str(session["id"])
        / ".heartbeat"
    )
    if not heartbeat.exists():
        return None
    return max(0.0, time.time() - heartbeat.stat().st_mtime)


def container_state(nanoclaw_dir: Path, session: dict[str, Any]) -> dict[str, Any]:
    db_path = (
        nanoclaw_dir
        / "data"
        / "v2-sessions"
        / str(session["agent_group_id"])
        / str(session["id"])
        / "outbound.db"
    )
    rows = db_rows(db_path, "SELECT current_tool, tool_declared_timeout_ms, tool_started_at, updated_at FROM container_state LIMIT 1")
    return dict(rows[0]) if rows else {}


def outbound_response(nanoclaw_dir: Path, session: dict[str, Any], thread_id: str) -> str | None:
    db_path = (
        nanoclaw_dir
        / "data"
        / "v2-sessions"
        / str(session["agent_group_id"])
        / str(session["id"])
        / "outbound.db"
    )
    rows = db_rows(
        db_path,
        """
        SELECT content
        FROM messages_out
        WHERE thread_id = ?
        ORDER BY seq DESC
        LIMIT 1
        """,
        (thread_id,),
    )
    if not rows:
        return None
    content = rows[0]["content"]
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return str(content)
    return str(parsed.get("text") or content)


def poll_nanoclaw_response(
    nanoclaw_dir: Path,
    thread_id: str,
    max_seconds: int,
    idle_seconds: int,
    poll_seconds: float,
    status_seconds: int,
) -> tuple[str, dict[str, Any]]:
    started = time.time()
    last_healthy = time.time()
    last_status = 0.0
    last_snapshot: dict[str, Any] = {}

    while True:
        elapsed = time.time() - started
        if max_seconds > 0 and elapsed > max_seconds:
            raise TimeoutError(f"max wall-clock timeout exceeded after {int(elapsed)}s")

        session = latest_session(nanoclaw_dir, thread_id)
        if session:
            response = outbound_response(nanoclaw_dir, session, thread_id)
            heartbeat_age = heartbeat_age_seconds(nanoclaw_dir, session)
            state = container_state(nanoclaw_dir, session)
            healthy = (
                session.get("container_status") == "running"
                and (heartbeat_age is None or heartbeat_age <= idle_seconds)
            )
            if healthy:
                last_healthy = time.time()
            last_snapshot = {
                "session": session,
                "heartbeat_age_seconds": heartbeat_age,
                "container_state": state,
                "elapsed_seconds": int(elapsed),
                "seconds_since_healthy": int(time.time() - last_healthy),
            }
            if response:
                return response, last_snapshot
        else:
            last_snapshot = {
                "session": None,
                "elapsed_seconds": int(elapsed),
                "seconds_since_healthy": int(time.time() - last_healthy),
            }

        if time.time() - last_healthy > idle_seconds:
            raise TimeoutError(f"NanoClaw runtime unhealthy/idle for >{idle_seconds}s: {last_snapshot}")

        if status_seconds > 0 and time.time() - last_status > status_seconds:
            print(json.dumps({"event": "poll_status", **last_snapshot}, sort_keys=True), flush=True)
            last_status = time.time()

        time.sleep(poll_seconds)


def check_response(case: dict[str, Any], response: str) -> dict[str, Any]:
    lowered = response.lower()
    must_include = [str(item).lower() for item in case.get("must_include", [])]
    must_not_include = [str(item).lower() for item in case.get("must_not_include", [])]
    missing = [item for item in must_include if item not in lowered]
    banned = [item for item in must_not_include if item in lowered]
    has_plan_shape = all(item in lowered for item in ["primitive", "filter"]) and any(
        item in lowered for item in ["direct", "count-first", "count first", "sliced", "slice"]
    )
    return {
        "ok": not missing and not banned and has_plan_shape,
        "missing_required_terms": missing,
        "banned_terms_present": banned,
        "has_plan_shape": has_plan_shape,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run plan-only Powerpacks evals through NanoClaw")
    parser.add_argument("--nanoclaw-dir", default=os.getenv("NANOCLAW_DIR", str(POWERPACKS_ROOT.parent / "nanoclaw-powerpacks")))
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--case")
    parser.add_argument("--out-dir")
    parser.add_argument("--thread-prefix", default="powerpacks-plan-eval")
    parser.add_argument("--timeout", type=int, default=3600, help="Max wall-clock seconds. Use 0 for no wall-clock ceiling.")
    parser.add_argument("--idle-timeout", type=int, default=300, help="Fail when NanoClaw is not healthy for this many seconds.")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--status-seconds", type=int, default=30)
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args()

    cases_path = Path(args.cases)
    nanoclaw_dir = Path(args.nanoclaw_dir)
    out_base = Path(args.out_dir) if args.out_dir else (
        DEFAULT_OUT_DIR if args.render_only else nanoclaw_dir / "powerpacks" / ".powerpacks" / "plan-evals"
    )
    run_dir = out_base / now_stamp()
    cases = load_cases(cases_path, args.case)
    results: list[dict[str, Any]] = []

    for case in cases:
        case_id = str(case["id"])
        case_dir = run_dir / slugify(case_id)
        prompt = build_prompt(case, case_dir, nanoclaw_dir)
        write_json(case_dir / "case.json", case)
        (case_dir / "prompt.txt").parent.mkdir(parents=True, exist_ok=True)
        (case_dir / "prompt.txt").write_text(prompt)

        if args.render_only:
            result = {
                "case_id": case_id,
                "query": case["query"],
                "render_only": True,
                "prompt_path": str(case_dir / "prompt.txt"),
            }
            results.append(result)
            continue

        thread_id = f"{args.thread_prefix}-{slugify(case_id)}-{uuid4()}"
        started = time.time()
        sent = send_to_nanoclaw(nanoclaw_dir, thread_id, prompt)
        send_stdout = (sent.stdout or "").strip()
        send_stderr = (sent.stderr or "").strip()
        response = ""
        poll_snapshot: dict[str, Any] = {}
        poll_error = ""
        if sent.returncode == 0:
            try:
                response, poll_snapshot = poll_nanoclaw_response(
                    nanoclaw_dir,
                    thread_id,
                    max_seconds=args.timeout,
                    idle_seconds=args.idle_timeout,
                    poll_seconds=args.poll_seconds,
                    status_seconds=args.status_seconds,
                )
            except TimeoutError as exc:
                poll_error = str(exc)
        elapsed_ms = int((time.time() - started) * 1000)
        if not response and send_stdout:
            response = send_stdout
        stderr = "\n".join(part for part in [send_stderr, poll_error] if part)
        (case_dir / "response.txt").write_text(response)
        if stderr:
            (case_dir / "stderr.txt").write_text(stderr)
        checks = check_response(case, response)
        result = {
            "case_id": case_id,
            "query": case["query"],
            "thread_id": thread_id,
            "returncode": sent.returncode,
            "elapsed_ms": elapsed_ms,
            "prompt_path": str(case_dir / "prompt.txt"),
            "response_path": str(case_dir / "response.txt"),
            "stderr_path": str(case_dir / "stderr.txt") if stderr else None,
            "poll_snapshot": poll_snapshot,
            "checks": checks,
            "ok": sent.returncode == 0 and not poll_error and checks["ok"],
        }
        write_json(case_dir / "result.json", result)
        results.append(result)

    summary = {
        "run_dir": str(run_dir),
        "cases_path": str(cases_path),
        "render_only": args.render_only,
        "case_count": len(results),
        "passed": sum(1 for result in results if result.get("ok") or result.get("render_only")),
        "results": results,
    }
    write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not args.render_only and any(not result.get("ok") for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
