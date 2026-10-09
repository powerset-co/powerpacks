#!/usr/bin/env python3
"""The scheduled refresh task: install it into Codex or Claude, list its runs.

Both harnesses run the same prompt (refresh sources, answer asks, report),
defaulting to daily at 06:00. Both support weekdays and weekly schedules:

    codex   install creates and verifies a persisted chat through app-server,
            writes its native heartbeat config, and opens that chat. Each run
            resumes it. The App imports the config before installation is
            reported complete. Remove does what the App's own
            delete does: drop its codex-dev.db `automations` row. The folder is
            backed up and the chat stays open. Runs come from heartbeat turns in
            the target chat's transcript; older standalone runs use their
            `Automation ID: <id>` title.
    claude  claude-task.md → ~/.claude/scheduled-tasks/<id>/SKILL.md plus an entry
            in Claude Desktop's claude-code-sessions/<account>/<org>/
            scheduled-tasks.json. Desktop loads that file at launch and writes it
            back from memory, so install and remove quit Desktop, edit the file,
            and reopen it. Runs: the entry's lastRunAt, and Claude Code sessions in
            ~/.claude/projects/<repo slug>/ whose first prompt carries the marker.

Changelog:
  2026-10-08: run the ask worker after the scheduled source refresh.
  2026-09-28: created for the local Scheduled tasks page; Codex install uses the
    PR #365 template, now quiet unless a source needs attention.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import select
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from string import Template
from typing import Iterator, Literal

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit  # noqa: E402

Runner = Literal["codex", "claude"]
RUNNERS: tuple[Runner, ...] = ("codex", "claude")
CADENCES = ("daily", "weekdays", "weekly")
DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
CODEX_TIMEOUT_SECONDS = 30

TASK_ID = "refresh-message-sources"
TASK_NAME = "Refresh message sources"
REFRESH_COMMAND = "uv run --project . python packs/ingestion/primitives/refresh/refresh_sources.py run"
ATTENTION = "NEEDS ATTENTION"
PROMPT = (
    f"Task: {TASK_ID}\n"
    f"From the repository root, run exactly this command:\n\n{REFRESH_COMMAND}\n\n"
    "After the refresh, run `uv run --no-sync --project . python packs/ingestion/primitives/ask_worker/ask_worker.py run`. "
    "Then reply in at most four lines: the run's status, then one line per source with its outcome "
    "and note from the JSON. Do not retry, fix, sign in, or open anything. "
    f"If the command exits non-zero or any source is failed or needs_you, make your first line {ATTENTION}."
)
MAX_RUNS = 30
TEMPLATES = _REPO_ROOT / "automations" / TASK_ID

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CODEX_APP_DB = CODEX_HOME / "sqlite" / "codex-dev.db"
CODEX_THREADS_DB = CODEX_HOME / "state_5.sqlite"

CLAUDE_APP = "Claude"
CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_TASK = Path.home() / ".claude" / "scheduled-tasks" / TASK_ID / "SKILL.md"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CLAUDE_SESSIONS = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
QUIT_WAIT_SECONDS = 20
# A session's first lines hold its prompt; the marker is on the prompt's first line.
CLAUDE_HEAD_LINES = 12


def _local_timezone() -> str:
    return str(Path("/etc/localtime").resolve()).split("/zoneinfo/", 1)[1]


@dataclass(frozen=True)
class Schedule:
    cadence: Literal["daily", "weekdays", "weekly"] = "daily"
    time: str = "06:00"
    day: str = "MO"
    timezone: str = ""

    def __post_init__(self) -> None:
        if self.cadence not in CADENCES or self.day not in DAYS:
            raise ValueError("Choose daily, weekdays, or weekly, with a valid weekday.")
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.time) is None:
            raise ValueError("Time must be HH:MM in 24-hour time.")
        local = _local_timezone()
        if self.timezone and self.timezone != local:
            raise ValueError(f"Schedules use this computer's timezone: {local}.")
        object.__setattr__(self, "timezone", local)

    def rrule(self) -> str:
        hour, minute = (int(part) for part in self.time.split(":"))
        days = "MO,TU,WE,TH,FR" if self.cadence == "weekdays" else self.day
        rule = f"RRULE:FREQ={'DAILY' if self.cadence == 'daily' else 'WEEKLY'};BYHOUR={hour};BYMINUTE={minute}"
        return rule if self.cadence == "daily" else f"{rule};BYDAY={days}"

    def cron(self) -> str:
        hour, minute = (int(part) for part in self.time.split(":"))
        day = "*" if self.cadence == "daily" else "1-5" if self.cadence == "weekdays" else str((DAYS.index(self.day) + 1) % 7)
        return f"{minute} {hour} * * {day}"

    def label(self) -> str:
        hour, minute = (int(part) for part in self.time.split(":"))
        weekday = dict(zip(DAYS, ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")))[self.day]
        cadence = {"daily": "Daily", "weekdays": "Weekdays", "weekly": f"Every {weekday}"}[self.cadence]
        return f"{cadence} at {hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"

    @classmethod
    def from_rrule(cls, rule: str) -> Schedule | None:
        try:
            parts = dict(part.split("=", 1) for part in rule.removeprefix("RRULE:").split(";"))
        except ValueError:
            return None
        if set(parts) - {"FREQ", "INTERVAL", "BYHOUR", "BYMINUTE", "BYDAY", "BYSECOND"}:
            return None
        if parts.get("INTERVAL", "1") != "1" or parts.get("BYSECOND", "0") != "0":
            return None
        days = parts.get("BYDAY", "")
        cadence = "daily" if not days or set(days.split(",")) == set(DAYS) else "weekdays" if days == "MO,TU,WE,TH,FR" else "weekly"
        if parts.get("FREQ") not in ("DAILY", "WEEKLY") or (cadence == "weekly" and days not in DAYS):
            return None
        try:
            return cls(cadence, f"{int(parts['BYHOUR']):02d}:{int(parts['BYMINUTE']):02d}", days if cadence == "weekly" else "MO")
        except (KeyError, ValueError):
            return None


@dataclass(frozen=True)
class Run:
    id: str
    runner: Runner
    started_at: str
    status: Literal["ok", "failed", "unknown"]
    summary: str
    open_url: str | None
    resume_command: str | None


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    schedule: str
    command: str
    installs: list[Runner]
    runs: list[Run]
    schedule_settings: Schedule | None
    codex_thread_url: str | None
    codex_install_status: Literal["not_installed", "pending", "installed"]


def read_task(repo: Path) -> Task:
    runs = sorted([*_codex_runs(), *_claude_runs(repo)], key=lambda run: run.started_at, reverse=True)
    config = _codex_config()
    schedule = Schedule.from_rrule(config["rrule"]) if config else Schedule()
    thread_url = f"codex://threads/{config['target_thread_id']}" if config and config.get("target_thread_id") else None
    status = "installed" if config and _codex_imported(config) else "pending" if config else "not_installed"
    return Task(TASK_ID, TASK_NAME, schedule.label() if schedule else "Custom schedule in Codex", REFRESH_COMMAND,
                installed_runners(), runs[:MAX_RUNS], schedule, thread_url, status)


def installed_runners() -> list[Runner]:
    """Where the task is installed. Cheap: no run history is read."""
    installed: list[Runner] = []
    config = _codex_config()
    if config and _codex_imported(config):
        installed.append("codex")
    if any(_claude_entry(path) is not None for path in _claude_task_files()):
        installed.append("claude")
    return installed


# ---------------------------------------------------------------- install / remove


def install(runner: Runner, repo: Path, schedule: Schedule | None = None) -> None:
    """Install or update the schedule, preserving its existing chat."""
    schedule = schedule or Schedule()
    if runner == "codex":
        _install_codex(repo.resolve(), schedule)
    elif runner not in installed_runners():
        _install_claude(repo, schedule)


def uninstall(runner: Runner) -> None:
    if runner == "codex":
        _remove_codex()
        return
    with _claude_closed():
        for path in _claude_task_files():
            document = json.loads(path.read_text())
            document["scheduledTasks"] = [task for task in document["scheduledTasks"] if task["id"] != TASK_ID]
            path.write_text(json.dumps(document, indent=2))
    shutil.rmtree(CLAUDE_TASK.parent, ignore_errors=True)


def _install_codex(repo: Path, schedule: Schedule) -> None:
    config = _codex_config()
    thread_id = config.get("target_thread_id") if config else None
    if thread_id:
        _verify_codex_thread(thread_id, repo)
    else:
        thread_id = _create_codex_thread(repo)
    now = int(time.time() * 1000)
    if config is None:
        config = {"version": 1, "id": TASK_ID, "kind": "heartbeat", "name": TASK_NAME,
                  "prompt": PROMPT, "status": "ACTIVE", "target_thread_id": thread_id, "created_at": now}
    config.update(kind="heartbeat", target_thread_id=thread_id, rrule=schedule.rrule(), updated_at=now)
    path = CODEX_HOME / "automations" / config["id"] / "automation.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, CODEX_HOME / f"{config['id']}.{time.time_ns()}.bkup")
    if path.exists():
        text = path.read_text()
        for key in ("kind", "target_thread_id", "rrule", "updated_at"):
            line = f"{key} = {json.dumps(config[key], ensure_ascii=False)}"
            pattern = rf"^{key}\s*=.*$"
            text = re.sub(pattern, lambda _: line, text, flags=re.MULTILINE) if re.search(pattern, text, re.MULTILINE) else text + "\n" + line
    else:
        text = "\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in config.items())
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text + "\n")
    temporary.replace(path)
    subprocess.run(["open", f"codex://threads/{thread_id}"], check=True)


def _create_codex_thread(repo: Path) -> str:
    with subprocess.Popen(["codex", "app-server", "--listen", "stdio://"], cwd=repo,
                          env={**os.environ, "CODEX_HOME": str(CODEX_HOME)}, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0) as process:
        def request(request_id: int, method: str, params: dict) -> dict:
            process.stdin.write((json.dumps({"id": request_id, "method": method, "params": params}) + "\n").encode())
            deadline = time.monotonic() + CODEX_TIMEOUT_SECONDS
            while select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError("Codex exited before creating the scheduled task chat.")
                response = json.loads(line)
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    raise RuntimeError(response["error"]["message"])
                return response["result"]
            raise RuntimeError("Codex timed out creating the scheduled task chat.")

        try:
            request(0, "initialize", {"clientInfo": {"name": "powerpacks", "version": "1.0"},
                                      "capabilities": {"experimentalApi": True}})
            process.stdin.write(b'{"method":"initialized","params":{}}\n')
            thread = request(1, "thread/start", {"cwd": str(repo), "ephemeral": False, "historyMode": "legacy",
                                                "sandbox": "danger-full-access", "approvalPolicy": "never"})["thread"]
            request(2, "thread/name/set", {"threadId": thread["id"], "name": TASK_NAME})
            request(3, "thread/settings/update", {"threadId": thread["id"], "approvalPolicy": "never",
                                                   "sandboxPolicy": {"type": "dangerFullAccess"}})
            persisted = request(4, "thread/read", {"threadId": thread["id"], "includeTurns": False})["thread"]
            if persisted["cwd"] != str(repo) or persisted["id"] != thread["id"]:
                raise RuntimeError("Codex created the scheduled task chat in a different repository.")
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    _verify_codex_thread(thread["id"], repo)
    return thread["id"]


def _verify_codex_thread(thread_id: str, repo: Path) -> None:
    with sqlite3.connect(f"file:{CODEX_THREADS_DB}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT cwd, rollout_path, sandbox_policy, approval_mode FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if row is None or row[0] != str(repo) or not Path(row[1]).is_file():
        raise RuntimeError("The scheduled task chat is not persisted in this repository.")
    if json.loads(row[2])["type"] not in ("disabled", "danger-full-access") or row[3] != "never":
        raise RuntimeError("The scheduled task chat does not have the requested Full access permissions.")


def _codex_config() -> dict | None:
    tasks = _codex_tasks()
    automation_id = tasks[0][0] if tasks else TASK_ID
    path = CODEX_HOME / "automations" / automation_id / "automation.toml"
    if path.exists():
        return tomllib.loads(path.read_text())
    if not tasks:
        return None
    with sqlite3.connect(f"file:{CODEX_APP_DB}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT id, name, prompt, kind, status, rrule, target_thread_id, created_at, updated_at "
                           "FROM automations WHERE id = ?", (automation_id,)).fetchone()
    return {"version": 1, **dict(row)}


def _codex_imported(config: dict) -> bool:
    if config.get("kind") != "heartbeat" or not CODEX_APP_DB.exists():
        return False
    with sqlite3.connect(f"file:{CODEX_APP_DB}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT kind, target_thread_id, rrule, updated_at FROM automations WHERE id = ? AND status != 'DELETED'",
                           (config["id"],)).fetchone()
    return row == ("heartbeat", config["target_thread_id"], config["rrule"], config["updated_at"])


def _remove_codex() -> None:
    """Remove the native schedule while preserving its data and target chat."""
    for automation_id, _ in _codex_tasks():
        folder = CODEX_HOME / "automations" / automation_id
        if folder.exists():
            shutil.move(folder, CODEX_HOME / f"{automation_id}.{time.time_ns()}.bkup")
        with sqlite3.connect(CODEX_APP_DB, timeout=10) as conn:
            conn.execute("DELETE FROM automations WHERE id = ?", (automation_id,))


def _install_claude(repo: Path, schedule: Schedule) -> None:
    targets = _claude_task_files()
    if not targets:
        raise RuntimeError("Open Claude Desktop once and sign in, then install again.")
    entry = {
        "id": TASK_ID,
        "displayName": TASK_NAME,
        "cronExpression": schedule.cron(),
        "model": CLAUDE_MODEL,
        "enabled": True,
        "filePath": str(CLAUDE_TASK),
        "createdAt": int(time.time() * 1000),
        "cwd": str(repo),
        "approvedPermissions": [{"toolName": "Bash", "ruleContent": REFRESH_COMMAND}],
    }
    # Nothing is written until Desktop has quit, so a failed quit leaves no half install.
    with _claude_closed():
        CLAUDE_TASK.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE_TASK.write_text(Template((TEMPLATES / "claude-task.md").read_text()).substitute(prompt=PROMPT))
        for path in targets:
            document = json.loads(path.read_text())
            document["scheduledTasks"] = [
                *(task for task in document["scheduledTasks"] if task["id"] != TASK_ID), entry,
            ]
            path.write_text(json.dumps(document, indent=2))


def _claude_task_files() -> list[Path]:
    return sorted(CLAUDE_SESSIONS.glob("*/*/scheduled-tasks.json"))


def _claude_entry(path: Path) -> dict | None:
    tasks = json.loads(path.read_text())["scheduledTasks"]
    return next((task for task in tasks if task["id"] == TASK_ID), None)


class _claude_closed:  # noqa: N801 - reads as a with-statement phrase
    """Quit Claude Desktop for the edit and reopen it after, when it was running:
    Desktop reads scheduled-tasks.json at launch and rewrites it from memory."""

    def __enter__(self) -> None:
        self.was_running = _claude_running()
        if not self.was_running:
            return
        subprocess.run(["osascript", "-e", f'quit app "{CLAUDE_APP}"'], check=False, capture_output=True)
        deadline = time.monotonic() + QUIT_WAIT_SECONDS
        while _claude_running():
            if time.monotonic() > deadline:
                raise RuntimeError("Claude Desktop didn't quit. Close it and try again.")
            time.sleep(0.5)

    def __exit__(self, *_: object) -> None:
        if self.was_running:
            subprocess.run(["open", "-a", CLAUDE_APP], check=False)


def _claude_running() -> bool:
    return subprocess.run(["pgrep", "-x", CLAUDE_APP], capture_output=True).returncode == 0


# ---------------------------------------------------------------- Codex history


def _codex_runs() -> list[Run]:
    automations = _codex_tasks()
    targets = {thread_id for _, thread_id in automations if thread_id}
    runs = [
        Run(
            id=thread_id,
            runner="codex",
            started_at=_iso(created_ms / 1000),
            status=_report_status(summary) if (summary := _codex_final_message(Path(rollout))) else "unknown",
            summary=summary,
            open_url=f"codex://threads/{thread_id}",
            resume_command=f"codex resume {thread_id}",
        )
        for thread_id, created_ms, rollout in _codex_threads()
        if thread_id not in targets
    ]
    if CODEX_THREADS_DB.exists():
        with sqlite3.connect(f"file:{CODEX_THREADS_DB}?mode=ro", uri=True) as conn:
            for automation_id, thread_id in automations:
                if thread_id is None:
                    continue
                row = conn.execute("SELECT rollout_path FROM threads WHERE id = ?", (thread_id,)).fetchone()
                if row:
                    runs.extend(_codex_heartbeat_runs(automation_id, thread_id, Path(row[0])))
    return runs


def _codex_heartbeat_runs(automation_id: str, thread_id: str, rollout: Path) -> list[Run]:
    runs = []
    scheduled = False
    for row in _rows(rollout):
        payload = row.get("payload") or {}
        if row.get("type") == "event_msg" and payload.get("type") == "task_started":
            scheduled = False
        elif row.get("type") == "response_item" and payload.get("role") == "user":
            scheduled = scheduled or f"<automation_id>{automation_id}</automation_id>" in _text(payload.get("content"))
        elif row.get("type") == "event_msg" and payload.get("type") == "task_complete" and scheduled:
            error = payload.get("error")
            summary = _strip_codex_citations(payload.get("last_agent_message") or (error or {}).get("message", ""))
            status = "failed" if error else _report_status(summary) if summary else "unknown"
            runs.append(Run(
                id=payload["turn_id"],
                runner="codex",
                started_at=_iso(payload["started_at"]),
                status=status,
                summary=summary,
                open_url=f"codex://threads/{thread_id}",
                resume_command=f"codex resume {thread_id}",
            ))
    return runs


def _codex_threads() -> list[tuple[str, int, str]]:
    if not CODEX_THREADS_DB.exists():
        return []
    with sqlite3.connect(f"file:{CODEX_THREADS_DB}?mode=ro", uri=True) as conn:
        return conn.execute(
            "SELECT id, created_at_ms, rollout_path FROM threads "
            "WHERE thread_source = 'automation' AND title LIKE ? ORDER BY created_at_ms DESC LIMIT ?",
            (f"%Automation ID: {TASK_ID}%", MAX_RUNS),
        ).fetchall()


def _codex_tasks() -> list[tuple[str, str | None]]:
    if not CODEX_APP_DB.exists():
        return []
    with sqlite3.connect(f"file:{CODEX_APP_DB}?mode=ro", uri=True) as conn:
        return conn.execute(
            "SELECT id, target_thread_id FROM automations WHERE status != 'DELETED' "
            "AND (id = ? OR instr(prompt, ?) > 0)",
            (TASK_ID, f"Task: {TASK_ID}\n"),
        ).fetchall()


def _report_status(summary: str) -> Literal["ok", "failed"]:
    return "failed" if summary.lstrip().startswith(ATTENTION) else "ok"


def _codex_final_message(rollout: Path) -> str:
    if not rollout.exists():
        return ""
    final = ""
    for row in _rows(rollout):
        payload = row.get("payload") or {}
        if row.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
            final = "".join(part.get("text", "") for part in payload.get("content", []))
    return _strip_codex_citations(final)


def _strip_codex_citations(text: str) -> str:
    text = re.sub(r"<oai-mem-citation>.*?</oai-mem-citation>", "", text, flags=re.DOTALL)
    text = re.sub(r"<heartbeat>.*?</heartbeat>", "", text, flags=re.DOTALL)
    # Older runs ended with the App's `::inbox-item{...}` directive.
    return re.sub(r"^::inbox-item.*$", "", text, flags=re.MULTILINE).strip()


# ---------------------------------------------------------------- Claude history


def _claude_runs(repo: Path) -> list[Run]:
    project = CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(repo))
    if not project.is_dir():
        return []
    runs = []
    for transcript in sorted(project.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True):
        started_at = _claude_task_start(transcript)
        if started_at is None:
            continue
        summary = _claude_final_message(transcript)
        runs.append(Run(
            id=transcript.stem,
            runner="claude",
            started_at=started_at,
            status=_report_status(summary) if summary else "unknown",
            summary=summary,
            open_url=None,
            resume_command=f"claude --resume {transcript.stem}",
        ))
        if len(runs) == MAX_RUNS:
            break
    return runs


def _claude_task_start(transcript: Path) -> str | None:
    """The run's start when this session is our task: its first prompt carries the marker."""
    for _, row in zip(range(CLAUDE_HEAD_LINES), _rows(transcript)):
        if row.get("type") != "user" or row.get("isMeta"):
            continue
        return row["timestamp"] if f"Task: {TASK_ID}" in _text(row["message"].get("content")) else None
    return None


def _claude_final_message(transcript: Path) -> str:
    final = ""
    for row in _rows(transcript):
        if row.get("type") == "assistant":
            final = _text(row["message"].get("content")) or final
    return final.strip()


def _rows(transcript: Path) -> Iterator[dict]:
    """A transcript's JSON lines. A run still writing leaves a torn last line; skip it."""
    with transcript.open() as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _iso(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["list", "install", "uninstall"])
    parser.add_argument("--runner", choices=RUNNERS)
    args = parser.parse_args()
    repo = Path.cwd().resolve()
    if args.command in ("install", "uninstall") and args.runner is None:
        parser.error("--runner is required")
    if args.command == "install":
        install(args.runner, repo)
    elif args.command == "uninstall":
        uninstall(args.runner)
    emit(asdict(read_task(repo)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
