#!/usr/bin/env python3
"""Config-gated Codex heartbeat runner.

This script is intentionally cheap when no processing is due: it reads local JSON
config/state and exits without invoking Codex until a configured task is due.
Both Docker and launchd wrappers use it for the actual scheduling gate.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUTH_ERROR_MARKERS = (
    "401 Unauthorized",
    "Missing bearer",
    "authentication",
    "not authenticated",
    "API key",
)

DEFAULT_PROMPT = (
    "Powerpacks scheduled heartbeat: inspect the local Powerpacks checkout and "
    "report one terse line with current status. Do not run spend-bearing searches, "
    "uploads, or external workflows unless the prompt explicitly asks for them."
)
PENDING_PREP_EXIT_CODE = 125


def now_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).isoformat()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_config_path(root: Path) -> Path:
    local = root / ".powerpacks" / "codex-heartbeat.json"
    if local.exists():
        return local
    return root / "config" / "codex-heartbeat.example.json"


def default_init_config_path(root: Path) -> Path:
    return root / ".powerpacks" / "codex-heartbeat.json"


def default_state_path() -> Path:
    env_state = os.environ.get("POWERPACKS_HEARTBEAT_STATE")
    if env_state:
        return Path(env_state).expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state).expanduser() / "powerpacks" / "codex-heartbeat-state.json"
    return Path.home() / ".local" / "state" / "powerpacks" / "codex-heartbeat-state.json"


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def default_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "interval_seconds": 3600,
        "retry_interval_seconds": 900,
        "run_on_start": True,
        "prompt": DEFAULT_PROMPT,
    }


def load_config(path: Path) -> dict[str, Any]:
    config = read_json(path, default_config())
    defaults = default_config()
    for key, value in defaults.items():
        config.setdefault(key, value)
    return config


def normalize_tasks(config: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    raw_tasks = config.get("tasks")
    if raw_tasks is None:
        task = dict(config)
        task["id"] = str(task.get("id") or "default")
        return [task], True
    if not isinstance(raw_tasks, list):
        raise ValueError("tasks must be a list of task objects")

    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    inherited_keys = (
        "enabled",
        "interval_seconds",
        "retry_interval_seconds",
        "run_on_start",
        "timeout_seconds",
        "codex_args",
    )
    for idx, raw_task in enumerate(raw_tasks):
        if not isinstance(raw_task, dict):
            raise ValueError(f"tasks[{idx}] must be an object")
        task_id = str(raw_task.get("id") or "").strip()
        if not task_id:
            raise ValueError(f"tasks[{idx}].id is required")
        if task_id in seen:
            raise ValueError(f"duplicate heartbeat task id: {task_id}")
        seen.add(task_id)
        task = {key: config[key] for key in inherited_keys if key in config}
        task.update(raw_task)
        task.setdefault("enabled", config.get("enabled", True))
        task.setdefault("interval_seconds", config.get("interval_seconds", 3600))
        task.setdefault("retry_interval_seconds", config.get("retry_interval_seconds", 900))
        task.setdefault("run_on_start", config.get("run_on_start", True))
        task.setdefault("prompt", DEFAULT_PROMPT)
        tasks.append(task)
    return tasks, False


def task_state(state: dict[str, Any], task: dict[str, Any], legacy_state: bool) -> dict[str, Any]:
    if legacy_state:
        return state
    tasks_state = state.setdefault("tasks", {})
    if not isinstance(tasks_state, dict):
        raise ValueError("state.tasks must be an object")
    task_id = str(task["id"])
    entry = tasks_state.setdefault(task_id, {})
    if not isinstance(entry, dict):
        raise ValueError(f"state.tasks.{task_id} must be an object")
    return entry


def due_status(
    task: dict[str, Any],
    state: dict[str, Any],
    now: float,
    force: bool,
    include_pending: bool = False,
) -> tuple[bool, str, int]:
    task_id = task.get("id", "default")
    if not task.get("enabled", True):
        return False, f"{task_id}: disabled", 0
    if force:
        return True, f"{task_id}: forced", 0

    interval = max(0, int(task.get("interval_seconds", 3600)))
    retry_interval = max(0, int(task.get("retry_interval_seconds", 900)))
    last_attempt = state.get("last_attempt_epoch")
    last_exit_code = state.get("last_exit_code")
    if last_attempt is not None and last_exit_code not in (None, 0, "0"):
        if include_pending and last_exit_code == PENDING_PREP_EXIT_CODE:
            return True, f"{task_id}: pending prep attempt", 0
        retry_elapsed = int(now - float(last_attempt))
        if retry_elapsed < retry_interval:
            return (
                False,
                f"{task_id}: retry backoff: elapsed {retry_elapsed}s < retry interval {retry_interval}s",
                retry_interval - retry_elapsed,
            )

    last_success = state.get("last_success_epoch")
    if last_success is None:
        if task.get("run_on_start", True):
            return True, f"{task_id}: no previous successful run", 0
        return False, f"{task_id}: no previous successful run and run_on_start=false", interval

    elapsed = int(now - float(last_success))
    if elapsed >= interval:
        return True, f"{task_id}: due: elapsed {elapsed}s >= interval {interval}s", 0
    return False, f"{task_id}: not due: elapsed {elapsed}s < interval {interval}s", interval - elapsed


def due_tasks(
    tasks: list[dict[str, Any]],
    state: dict[str, Any],
    legacy_state: bool,
    now: float,
    force: bool = False,
    include_pending: bool = False,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any], str]], list[tuple[str, int]]]:
    due: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    not_due: list[tuple[str, int]] = []
    for task in tasks:
        entry = task_state(state, task, legacy_state)
        is_due, reason, due_in = due_status(task, entry, now, force, include_pending)
        if is_due:
            due.append((task, entry, reason))
        else:
            not_due.append((reason, due_in))
    return due, not_due


def codex_command(task: dict[str, Any], prompt: str) -> list[str]:
    extra_args = task.get("codex_args", [])
    if extra_args is None:
        extra_args = []
    if not isinstance(extra_args, list) or not all(isinstance(arg, str) for arg in extra_args):
        raise ValueError("codex_args must be a list of strings")
    session = task.get("session", {})
    if session is None:
        session = {}
    if isinstance(session, str):
        session = {"mode": session}
    if not isinstance(session, dict):
        raise ValueError("session must be an object or string")

    mode = str(session.get("mode") or task.get("session_mode") or "new")
    if mode in ("new", "fresh", "none"):
        return ["codex", "exec", *extra_args, prompt]
    if mode == "resume-last":
        cmd = ["codex", "exec", *extra_args, "resume", "--last"]
        if session.get("all"):
            cmd.append("--all")
        cmd.append(prompt)
        return cmd
    if mode == "resume-id":
        session_id = str(session.get("id") or task.get("session_id") or "").strip()
        if not session_id:
            raise ValueError(f"task {task.get('id', 'default')} session.mode=resume-id requires session.id")
        return ["codex", "exec", *extra_args, "resume", session_id, prompt]
    raise ValueError(f"unknown session mode for task {task.get('id', 'default')}: {mode}")


def run_codex(task: dict[str, Any], prompt: str, timeout: int | None) -> int:
    cmd = codex_command(task, prompt)
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if any(marker in proc.stdout for marker in AUTH_ERROR_MARKERS):
        print(f"[{now_iso()}] error: Codex heartbeat task {task['id']} appears unauthenticated", flush=True)
        return 1
    return proc.returncode


def init_config(path: Path, overwrite: bool) -> int:
    if path.exists() and not overwrite:
        print(f"config already exists: {path}")
        return 0
    write_json(path, default_config())
    print(f"wrote config: {path}")
    return 0


def mark_attempt(entry: dict[str, Any], now: float, reason: str, exit_code: int = PENDING_PREP_EXIT_CODE) -> None:
    entry["last_attempt_epoch"] = now
    entry["last_attempt_at"] = now_iso(now)
    entry["last_due_reason"] = reason
    entry["last_exit_code"] = exit_code
    entry["last_finished_epoch"] = now
    entry["last_finished_at"] = now_iso(now)


def mark_result(entry: dict[str, Any], now: float, status: int, reason: str | None = None) -> None:
    entry["last_exit_code"] = status
    entry["last_finished_epoch"] = now
    entry["last_finished_at"] = now_iso(now)
    if reason is not None:
        entry["last_due_reason"] = reason
    if status == 0:
        entry["last_success_epoch"] = now
        entry["last_success_at"] = now_iso(now)


def record_attempt(config: dict[str, Any], state_path: Path, state: dict[str, Any], reason: str) -> int:
    tasks, legacy_state = normalize_tasks(config)
    current = time.time()
    due, _ = due_tasks(tasks, state, legacy_state, current)
    if config.get("max_tasks_per_tick") is not None:
        due = due[:max(0, int(config["max_tasks_per_tick"]))]
    for task, entry, due_reason in due:
        mark_attempt(entry, current, f"{reason}: {due_reason}")
        print(f"[{now_iso(current)}] recorded heartbeat attempt for task {task['id']}: {due_reason}")
    write_json(state_path, state)
    if not due:
        print(f"[{now_iso(current)}] no due tasks to record")
    return 0


def record_failure(config: dict[str, Any], state_path: Path, state: dict[str, Any], exit_code: int, reason: str) -> int:
    tasks, legacy_state = normalize_tasks(config)
    current = time.time()
    recorded = False
    for task in tasks:
        entry = task_state(state, task, legacy_state)
        if entry.get("last_exit_code") == PENDING_PREP_EXIT_CODE:
            mark_result(entry, current, exit_code, reason)
            print(f"[{now_iso(current)}] recorded heartbeat failure for task {task['id']} ({exit_code}): {reason}")
            recorded = True
    if not recorded:
        # Fallback for direct/manual calls without a preceding record-attempt.
        entry = task_state(state, tasks[0], legacy_state)
        mark_attempt(entry, current, reason, exit_code)
        print(f"[{now_iso(current)}] recorded heartbeat failure {exit_code}: {reason}")
    write_json(state_path, state)
    return 0


def task_prompt(task: dict[str, Any]) -> str:
    return os.environ.get("CODEX_HEARTBEAT_PROMPT") or str(task.get("prompt") or DEFAULT_PROMPT)


def run_due_tasks(
    tasks: list[dict[str, Any]],
    state_path: Path,
    state: dict[str, Any],
    legacy_state: bool,
    force: bool,
    include_pending: bool,
    max_tasks: int | None,
) -> int:
    current = time.time()
    due, not_due = due_tasks(tasks, state, legacy_state, current, force=force, include_pending=include_pending)
    for reason, due_in in not_due:
        print(f"[{now_iso(current)}] heartbeat check: {reason}")
        if due_in > 0:
            print(f"[{now_iso(current)}] next heartbeat due in {due_in}s")
    if max_tasks is not None:
        due = due[:max(0, max_tasks)]
    if not due:
        return 0

    aggregate_status = 0
    for task, entry, reason in due:
        started = time.time()
        mark_attempt(entry, started, reason)
        write_json(state_path, state)
        print(f"[{now_iso(started)}] starting Codex heartbeat task {task['id']}: {reason}")

        timeout_value = task.get("timeout_seconds")
        timeout = int(timeout_value) if timeout_value is not None else None
        try:
            status = run_codex(task, task_prompt(task), timeout)
        except FileNotFoundError:
            print(f"[{now_iso()}] error: codex CLI is not installed or not on PATH")
            status = 127
        except subprocess.TimeoutExpired:
            print(f"[{now_iso()}] heartbeat task {task['id']} timed out after {timeout}s")
            status = 124

        finished = time.time()
        mark_result(entry, finished, status)
        if status == 0:
            print(f"[{now_iso(finished)}] finished Codex heartbeat task {task['id']}")
        else:
            print(f"[{now_iso(finished)}] heartbeat task {task['id']} failed with exit code {status}")
            aggregate_status = aggregate_status or status
        write_json(state_path, state)
    return aggregate_status


def main(argv: list[str] | None = None) -> int:
    root = repo_root()
    parser = argparse.ArgumentParser(description="Run config-gated Codex heartbeat tasks")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--state", type=Path, default=default_state_path())
    parser.add_argument("--init-config", action="store_true", help="create a local editable config and exit")
    parser.add_argument("--overwrite", action="store_true", help="overwrite an existing config with --init-config")
    parser.add_argument("--force", action="store_true", help="run enabled task(s) even when the schedule is not due")
    parser.add_argument("--include-pending", action="store_true", help="ignore retry backoff for tasks marked as preparing")
    parser.add_argument("--check-due", action="store_true", help="exit 10 when any task is due, 0 when none are due, without invoking Codex")
    parser.add_argument("--dry-run", action="store_true", help="evaluate due status without invoking Codex or writing state")
    parser.add_argument("--record-attempt", action="store_true", help="record in-progress/prep attempts for currently due tasks and exit")
    parser.add_argument("--attempt-reason", default="preparing due heartbeat run")
    parser.add_argument("--record-failure", type=int, default=None, help="record a non-Codex prep failure exit code and exit")
    parser.add_argument("--failure-reason", default="heartbeat preparation failed")
    parser.add_argument("--max-tasks", type=int, default=None, help="maximum due tasks to run in this tick")
    args = parser.parse_args(argv)

    config_path = args.config
    if config_path is None:
        env_config = os.environ.get("POWERPACKS_HEARTBEAT_CONFIG")
        config_path = Path(env_config).expanduser() if env_config else (
            default_init_config_path(root) if args.init_config else default_config_path(root)
        )

    if args.init_config:
        return init_config(config_path, args.overwrite)

    config = load_config(config_path)
    state = read_json(args.state, {})
    if args.record_attempt:
        return record_attempt(config, args.state, state, args.attempt_reason)
    if args.record_failure is not None:
        return record_failure(config, args.state, state, args.record_failure, args.failure_reason)

    tasks, legacy_state = normalize_tasks(config)
    current = time.time()
    due, not_due = due_tasks(
        tasks,
        state,
        legacy_state,
        current,
        force=args.force,
        include_pending=args.include_pending,
    )
    for task, _, reason in due:
        print(f"[{now_iso(current)}] heartbeat check: {reason}; task={task['id']}; config={config_path}; state={args.state}")
    for reason, due_in in not_due:
        print(f"[{now_iso(current)}] heartbeat check: {reason}; config={config_path}; state={args.state}")
        if due_in > 0:
            print(f"[{now_iso(current)}] next heartbeat due in {due_in}s")
    if args.check_due:
        return 10 if due else 0
    if args.dry_run:
        if due:
            print(f"[{now_iso(current)}] dry-run: would invoke Codex for {len(due)} task(s)")
        return 0

    max_tasks = args.max_tasks
    if max_tasks is None and config.get("max_tasks_per_tick") is not None:
        max_tasks = int(config["max_tasks_per_tick"])

    return run_due_tasks(
        tasks,
        args.state,
        state,
        legacy_state,
        force=args.force,
        include_pending=args.include_pending,
        max_tasks=max_tasks,
    )


if __name__ == "__main__":
    raise SystemExit(main())
