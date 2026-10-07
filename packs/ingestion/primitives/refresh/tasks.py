#!/usr/bin/env python3
"""The scheduled refresh task: install it into Codex or Claude, list its runs.

Both harnesses run the same prompt (run `refresh_sources.py run`, report) daily
at 06:00:

    codex   install opens `codex://threads/new?prompt=…` asking Codex to create
            a heartbeat attached to that chat, so each run resumes it. The
            App's own automation tool creates it. Remove does what the App's own
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
import urllib.parse
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

TASK_ID = "refresh-message-sources"
TASK_NAME = "Refresh message sources"
SCHEDULE = "Daily at 6:00 AM"
CRON = "0 6 * * *"
REFRESH_COMMAND = "uv run --project . python packs/ingestion/primitives/refresh/refresh_sources.py run"
ATTENTION = "NEEDS ATTENTION"
PROMPT = (
    f"Task: {TASK_ID}\n"
    f"From the repository root, run exactly this command and nothing else:\n\n{REFRESH_COMMAND}\n\n"
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


def read_task(repo: Path) -> Task:
    runs = sorted([*_codex_runs(), *_claude_runs(repo)], key=lambda run: run.started_at, reverse=True)
    return Task(TASK_ID, TASK_NAME, SCHEDULE, REFRESH_COMMAND, installed_runners(), runs[:MAX_RUNS])


def installed_runners() -> list[Runner]:
    """Where the task is installed. Cheap: no run history is read."""
    installed: list[Runner] = []
    if _codex_tasks():
        installed.append("codex")
    if any(_claude_entry(path) is not None for path in _claude_task_files()):
        installed.append("claude")
    return installed


# ---------------------------------------------------------------- install / remove


def install(runner: Runner, repo: Path) -> None:
    """A no-op when already installed there, so a second click never makes a copy."""
    if runner in installed_runners():
        return
    if runner == "codex":
        _install_codex(repo)
    else:
        _install_claude(repo)


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


def _install_codex(repo: Path) -> None:
    """Open the chat that creates and receives the recurring task."""
    request = (
        f'Create an active scheduled task named "{TASK_NAME}" using Codex\'s native '
        'automation_update tool with kind heartbeat and destination thread, attached to this chat. Run daily at 6:00 AM '
        'in local time. Keep "Start each run in new chat" OFF so every run resumes this chat. '
        "Keep this chat open after successful runs. Do not run it now.\n\n"
        f"Use this saved prompt:\n\nWorking directory: {repo}.\n{PROMPT}\n\n"
        "Stay quiet while all sources refreshed or are not connected. Notify only when the command "
        "fails or a source is failed or needs_you."
    )
    subprocess.run(["open", f"codex://threads/new?{urllib.parse.urlencode({'prompt': request})}"], check=True)


def _remove_codex() -> None:
    """Remove the native schedule while preserving its data and target chat."""
    for automation_id, _ in _codex_tasks():
        folder = CODEX_HOME / "automations" / automation_id
        if folder.exists():
            shutil.move(folder, CODEX_HOME / f"{automation_id}.{time.time_ns()}.bkup")
        with sqlite3.connect(CODEX_APP_DB, timeout=10) as conn:
            conn.execute("DELETE FROM automations WHERE id = ?", (automation_id,))


def _install_claude(repo: Path) -> None:
    targets = _claude_task_files()
    if not targets:
        raise RuntimeError("Open Claude Desktop once and sign in, then install again.")
    entry = {
        "id": TASK_ID,
        "displayName": TASK_NAME,
        "cronExpression": CRON,
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
