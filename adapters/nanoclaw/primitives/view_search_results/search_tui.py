#!/usr/bin/env python3
"""Terminal UI for chatting while browsing Powerpacks search runs."""

from __future__ import annotations

import argparse
import curses
import importlib.util
import json
import os
import re
import shlex
import socket
import sqlite3
import subprocess
import textwrap
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def default_powerpacks_root() -> Path:
    configured = os.environ.get("POWERPACKS_ROOT")
    if configured:
        return Path(configured)
    # Post-reorg layout: the root is whichever ancestor contains a `packs/`
    # directory. Works on host (repo root) and inside the nanoclaw runtime
    # (install.sh copies packs/ into $TARGET/powerpacks/packs).
    for parent in Path(__file__).resolve().parents:
        if (parent / "packs").is_dir():
            return parent
    return Path(__file__).resolve().parents[3]


POWERPACKS_ROOT = default_powerpacks_root()
# `results_io.py` lives in the search pack post-reorg.
RESULTS_IO = (
    POWERPACKS_ROOT
    / "packs"
    / "search"
    / "primitives"
    / "persist_search_results"
    / "results_io.py"
)
DEFAULT_RUNS_DIR = POWERPACKS_ROOT / ".powerpacks" / "runs"
TRANSCRIPT_REPLAY_LIMIT = int(os.environ.get("POWERPACKS_TUI_TRANSCRIPT_REPLAY_LIMIT", "500"))
MESSAGE_HISTORY_LIMIT = int(os.environ.get("POWERPACKS_TUI_MESSAGE_HISTORY_LIMIT", "3000"))


def default_nanoclaw_dir() -> Path:
    configured = os.environ.get("NANOCLAW_DIR")
    if configured:
        return Path(configured)

    candidates = [
        POWERPACKS_ROOT.parent,
        Path.cwd(),
    ]
    for candidate in candidates:
        if (candidate / "nanoclaw.sh").exists():
            return candidate
    return POWERPACKS_ROOT.parent


DEFAULT_NANOCLAW_DIR = default_nanoclaw_dir()

spec = importlib.util.spec_from_file_location("powerpacks_results_io", RESULTS_IO)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load {RESULTS_IO}")
results_io = importlib.util.module_from_spec(spec)
spec.loader.exec_module(results_io)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def truncate(value: Any, width: int) -> str:
    text = str(value or "")
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "~"


def clean_display_text(speaker: str, text: Any) -> str:
    value = str(text)
    if speaker == "agent":
        value = re.sub(r"^\s*<message(?:\s+[^>]*)?>\s*", "", value)
        value = re.sub(r"\s*</message>\s*$", "", value)
        value = value.replace("[poll-loop] Result: ", "")
    return value.strip("\n")


def wrap_chat_line(speaker: str, text: str, width: int) -> list[str]:
    prefix = f"{speaker}> "
    if width <= len(prefix):
        return [truncate(prefix + text, width)]
    content_width = max(1, width - len(prefix))
    chunks = text.splitlines() or [""]
    rendered: list[str] = []
    for chunk in chunks:
        wrapped = textwrap.wrap(
            chunk,
            width=content_width,
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        for idx, part in enumerate(wrapped):
            rendered.append((prefix if idx == 0 else " " * len(prefix)) + part)
    return rendered


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, remainder = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {remainder:02d}s"
    if minutes:
        return f"{minutes}m {remainder:02d}s"
    return f"{remainder}s"


def searchable_text(row: dict[str, Any]) -> str:
    keys = ["name", "headline", "location", "current_titles", "current_companies", "linkedin_url", "person_id"]
    return " ".join(str(row.get(key, "")) for key in keys).lower()


def default_review_log(state_path: Path, state: dict[str, Any]) -> Path:
    artifacts = state.get("artifacts") or {}
    artifact_dir = artifacts.get("artifact_dir")
    task_id = state.get("task_id") or state_path.stem
    if artifact_dir:
        return Path(artifact_dir) / f"{task_id}.review.jsonl"
    return state_path.with_suffix(state_path.suffix + ".review.jsonl")


def state_sort_key(item: dict[str, Any]) -> str:
    return str(item.get("updated_at") or item.get("created_at") or item.get("mtime") or "")


def discover_run_dirs(runs_dir: Path, nanoclaw_dir: Path | None = None) -> list[Path]:
    dirs = [runs_dir]
    if nanoclaw_dir:
        groups_dir = nanoclaw_dir / "groups"
        if groups_dir.exists():
            dirs.extend(sorted(groups_dir.glob("*/.powerpacks/runs")))
    deduped = []
    seen = set()
    for directory in dirs:
        try:
            key = directory.resolve()
        except OSError:
            key = directory
        if key in seen:
            continue
        seen.add(key)
        deduped.append(directory)
    return deduped


def discover_runs(runs_dir: Path, nanoclaw_dir: Path | None = None) -> list[dict[str, Any]]:
    run_dirs = [directory for directory in discover_run_dirs(runs_dir, nanoclaw_dir) if directory.exists()]
    if not run_dirs:
        return []

    runs = []
    for directory in run_dirs:
        for path in directory.glob("*.json"):
            if path.name.endswith(".events.jsonl") or ".manifest." in path.name:
                continue
            try:
                state = read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if not state.get("task_id") or not state.get("query"):
                continue
            artifacts = state.get("artifacts") or {}
            rows = 0
            try:
                rows = len(results_io.result_rows(state))
            except Exception:
                rows = int(artifacts.get("row_count") or 0)
            runs.append({
                "path": str(path),
                "task_id": state.get("task_id"),
                "query": state.get("query"),
                "status": state.get("status") or state.get("summary", {}).get("status") or "",
                "row_count": rows,
                "hydrated_count": artifacts.get("hydrated_count"),
                "created_at": state.get("created_at"),
                "updated_at": state.get("updated_at"),
                "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            })
    return sorted(runs, key=state_sort_key, reverse=True)


def default_agent_command(nanoclaw_dir: Path, thread_id: str | None = None) -> str | None:
    configured = os.environ.get("POWERPACKS_AGENT_COMMAND")
    if configured:
        return configured
    threaded_chat = nanoclaw_dir / "scripts" / "chat-threaded.ts"
    if thread_id and threaded_chat.exists():
        return f"pnpm --silent -C {shlex.quote(str(nanoclaw_dir))} exec tsx scripts/chat-threaded.ts --thread {shlex.quote(thread_id)}"
    if nanoclaw_dir.exists():
        return f"pnpm --silent -C {shlex.quote(str(nanoclaw_dir))} run chat"
    return None


def default_start_command(nanoclaw_dir: Path) -> str | None:
    configured = os.environ.get("POWERPACKS_NANOCLAW_START_COMMAND")
    if configured:
        return configured
    if nanoclaw_dir.exists():
        return f"pnpm --silent -C {shlex.quote(str(nanoclaw_dir))} run dev"
    return None


def agent_argv(agent_command: str, prompt: str) -> list[str]:
    if "{prompt}" in agent_command:
        return [part.replace("{prompt}", prompt) for part in shlex.split(agent_command)]
    return [*shlex.split(agent_command), prompt]


def nanoclaw_socket_path(nanoclaw_dir: Path) -> Path:
    return nanoclaw_dir / "data" / "cli.sock"


def cli_threaded_socket_path(nanoclaw_dir: Path) -> Path:
    return nanoclaw_dir / "data" / "cli-threaded.sock"


def skill_summary(path: Path) -> dict[str, str] | None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return {"name": path.parent.name, "description": ""}

    data: dict[str, str] = {"name": path.parent.name, "description": ""}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in {"name", "description"}:
            data[key] = value.strip().strip('"')
    return data


def discover_powerpacks_skills(nanoclaw_dir: Path) -> list[dict[str, str]]:
    skills_dir = POWERPACKS_ROOT / "skills"
    if not skills_dir.exists():
        skills_dir = nanoclaw_dir / ".claude" / "skills"
    if not skills_dir.exists():
        return []
    skills = []
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        summary = skill_summary(path)
        if summary and not str(summary.get("name", "")).startswith("add-"):
            skills.append(summary)
    return skills


def command_items(skills: list[dict[str, str]]) -> list[dict[str, str]]:
    items = [
        {"command": "/skills", "description": "Show Powerpacks skills", "type": "command"},
        {"command": "/session", "description": "Show NanoClaw shared CLI session status", "type": "command"},
        {"command": "/resume", "description": "Open the newest Powerpacks search run", "type": "command"},
        {"command": "/runs", "description": "Show search runs", "type": "command"},
        {"command": "/back", "description": "Return to search-run browser", "type": "command"},
        {"command": "/reload", "description": "Reload runs or active result set", "type": "command"},
        {"command": "/start-nanoclaw", "description": "Start/check NanoClaw daemon", "type": "command"},
        {"command": "/filter", "description": "Filter visible runs or candidates", "type": "command"},
        {"command": "/clear", "description": "Clear active filter", "type": "command"},
        {"command": "/select", "description": "Select visible run index or candidate rank", "type": "command"},
        {"command": "/open", "description": "Open selected run or show selected candidate URL", "type": "command"},
        {"command": "/keep", "description": "Mark selected candidate as keep", "type": "command"},
        {"command": "/reject", "description": "Mark selected candidate as reject", "type": "command"},
        {"command": "/tag", "description": "Tag selected candidate", "type": "command"},
        {"command": "/note", "description": "Record note on selected candidate", "type": "command"},
        {"command": "/quit", "description": "Exit PowerClaw", "type": "command"},
    ]
    for skill in skills:
        name = skill.get("name") or ""
        if name:
            items.insert(0, {
                "command": f"/{name}",
                "description": skill.get("description") or "",
                "type": "skill",
            })
    return items


def fuzzy_score(query: str, text: str) -> int | None:
    needle = query.lower().strip().lstrip("/$")
    haystack = text.lower().lstrip("/")
    if not needle:
        return 1
    if needle in haystack:
        return 100 - haystack.index(needle)
    pos = 0
    score = 0
    for char in needle:
        found = haystack.find(char, pos)
        if found < 0:
            return None
        score += 5 if found == pos else 1
        pos = found + 1
    return score


def socket_reachable(sock_path: Path, timeout: float = 0.25) -> bool:
    if not sock_path.exists():
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(sock_path))
        return True
    except OSError:
        return False
    finally:
        client.close()


def parse_content_text(content: str) -> str:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content.strip()
    if isinstance(parsed, dict):
        text = parsed.get("text") or parsed.get("markdown")
        if isinstance(text, str):
            return text.strip()
    if isinstance(parsed, str):
        return parsed.strip()
    return ""


def display_user_text(text: str) -> str:
    """Hide Powerpacks transport context that is appended to NanoClaw prompts."""
    marker = "\n\nPowerpacks UI context:"
    if marker in text:
        text = text.split(marker, 1)[0]
    return text.strip()


def is_approval_prompt_text(text: str) -> bool:
    normalized = text.lower()
    return (
        ("search plan ready" in normalized and "awaiting approval" in normalized)
        or ("approve to run" in normalized and ("yolo" in normalized or "skip future gates" in normalized))
    )


def is_terminal_agent_text(text: str) -> bool:
    normalized = text.lower()
    return (
        "done " in normalized
        or normalized.startswith("done")
        or "found and hydrated" in normalized
        or "status: completed" in normalized
        or '"status": "completed"' in normalized
    )


def is_approval_reply_text(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in {"approve", "approved", "yolo"} or normalized.startswith("change:")


def nanoclaw_session_for_thread(nanoclaw_dir: Path, thread_id: str) -> dict[str, str] | None:
    db_path = nanoclaw_dir / "data" / "v2.db"
    if not db_path.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT s.id, s.agent_group_id, s.thread_id, s.created_at
            FROM sessions s
            LEFT JOIN messaging_groups mg ON mg.id = s.messaging_group_id
            WHERE s.thread_id = ? AND COALESCE(mg.channel_type, '') = 'cli-threaded'
            ORDER BY s.created_at DESC
            LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT id, agent_group_id, thread_id, created_at
                FROM sessions
                WHERE thread_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
        if row is None:
            return None
        return {key: str(row[key] or "") for key in row.keys()}
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            conn.close()


def read_session_rows(db_path: Path, table: str, speaker: str, limit: int) -> list[tuple[int, str, str]]:
    if not db_path.exists():
        return []
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=0.5)
        rows = conn.execute(
            f"SELECT seq, kind, content FROM {table} WHERE kind = 'chat' ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()

    transcript: list[tuple[int, str, str]] = []
    for seq, _kind, content in rows:
        text = parse_content_text(str(content))
        if speaker == "you":
            text = display_user_text(text)
        if text:
            transcript.append((int(seq), speaker, text))
    return transcript


def load_thread_transcript(nanoclaw_dir: Path, thread_id: str | None, limit: int = TRANSCRIPT_REPLAY_LIMIT) -> list[tuple[str, str]]:
    if not thread_id:
        return []
    session = nanoclaw_session_for_thread(nanoclaw_dir, thread_id)
    if not session:
        return []
    session_dir = nanoclaw_dir / "data" / "v2-sessions" / session["agent_group_id"] / session["id"]
    rows = [
        *read_session_rows(session_dir / "inbound.db", "messages_in", "you", limit),
        *read_session_rows(session_dir / "outbound.db", "messages_out", "agent", limit),
    ]
    rows.sort(key=lambda row: row[0])
    return [(speaker, text) for _seq, speaker, text in rows[-limit:]]


def thread_outbound_rows(nanoclaw_dir: Path, thread_id: str, after_seq: int = 0) -> list[tuple[int, str]]:
    session = nanoclaw_session_for_thread(nanoclaw_dir, thread_id)
    if not session:
        return []
    db_path = nanoclaw_dir / "data" / "v2-sessions" / session["agent_group_id"] / session["id"] / "outbound.db"
    if not db_path.exists():
        return []
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=0.5)
        rows = conn.execute(
            """
            SELECT seq, content
            FROM messages_out
            WHERE thread_id = ? AND seq > ?
            ORDER BY seq
            """,
            (thread_id, after_seq),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()
    return [(int(seq), parse_content_text(str(content))) for seq, content in rows if parse_content_text(str(content))]


def thread_inbound_completed(nanoclaw_dir: Path, thread_id: str) -> bool:
    session = nanoclaw_session_for_thread(nanoclaw_dir, thread_id)
    if not session:
        return False
    db_path = nanoclaw_dir / "data" / "v2-sessions" / session["agent_group_id"] / session["id"] / "inbound.db"
    if not db_path.exists():
        return False
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=0.5)
        row = conn.execute(
            """
            SELECT status
            FROM messages_in
            WHERE thread_id = ?
            ORDER BY seq DESC
            LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()
    return bool(row and str(row[0]) == "completed")


def thread_heartbeat_age(nanoclaw_dir: Path, thread_id: str) -> float | None:
    session = nanoclaw_session_for_thread(nanoclaw_dir, thread_id)
    if not session:
        return None
    heartbeat = nanoclaw_dir / "data" / "v2-sessions" / session["agent_group_id"] / session["id"] / ".heartbeat"
    if not heartbeat.exists():
        return None
    return max(0.0, time.time() - heartbeat.stat().st_mtime)


class SearchTui:
    def __init__(
        self,
        state_path: Path | None,
        runs_dir: Path,
        review_log: Path | None,
        nanoclaw_dir: Path,
        thread_id: str | None,
        agent_command: str | None,
        start_command: str | None,
        auto_start: bool,
    ):
        self.runs_dir = runs_dir
        self.nanoclaw_dir = nanoclaw_dir
        self.thread_id = thread_id
        self.agent_command = agent_command
        self.start_command = start_command
        self.auto_start = auto_start
        self.message_lock = threading.Lock()
        self.agent_state_lock = threading.RLock()
        self.agent_thread: threading.Thread | None = None
        self.current_process: subprocess.Popen[str] | None = None
        self.current_interrupt: threading.Event | None = None
        self.busy_started_at: float | None = None
        self.current_work_text = ""
        self.pending_messages: list[str] = []
        self.state_path: Path | None = None
        self.state_mtime = 0.0
        self.state: dict[str, Any] = {}
        self.review_log: Path | None = review_log
        self.skills = discover_powerpacks_skills(nanoclaw_dir)
        self.completion_items = command_items(self.skills)
        self.rows: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = discover_runs(runs_dir, nanoclaw_dir)
        self.mode = "runs"
        self.filtered_indexes: list[int] = list(range(len(self.runs)))
        self.selected = 0
        self.scroll = 0
        self.input_buffer = ""
        self.ctrl_c_deadline = 0.0
        transcript = load_thread_transcript(nanoclaw_dir, thread_id)
        outbound_rows = thread_outbound_rows(nanoclaw_dir, thread_id) if thread_id else []
        self.last_outbound_seq = max((seq for seq, _text in outbound_rows), default=0)
        self.seen_outbound_texts = {text for _seq, text in outbound_rows if text}
        self.messages: list[tuple[str, str]] = [
            ("system", "Powerpacks chat/review TUI"),
            ("system", "Right pane shows search runs. Enter opens a run. /help shows commands."),
        ]
        if transcript:
            self.messages.append(("system", f"Loaded {len(transcript)} previous NanoClaw messages for this thread."))
            self.messages.extend(transcript)
        if self.agent_command:
            self.messages.append(("system", f"NanoClaw bridge: {self.agent_command}"))
        else:
            self.messages.append(("system", "NanoClaw bridge is not configured; pass --agent-command or set POWERPACKS_AGENT_COMMAND."))
        if self.thread_id:
            self.messages.append(("system", f"NanoClaw thread: {self.thread_id}"))
        if self.start_command and self.auto_start:
            self.messages.append(("system", f"NanoClaw auto-start: {self.start_command}"))
        self.review_marks: dict[str, str] = {}
        self.active_filter = ""
        self.last_auto_refresh = 0.0

        if state_path:
            self.load_state(state_path, announce=False)

    def load_state(self, state_path: Path, announce: bool = True) -> None:
        self.state_path = state_path
        self.state = read_json(state_path)
        self.state_mtime = state_path.stat().st_mtime
        self.rows = results_io.result_rows(self.state)
        self.review_log = self.review_log or default_review_log(state_path, self.state)
        self.mode = "results"
        self.filtered_indexes = list(range(len(self.rows)))
        self.selected = 0
        self.scroll = 0
        self.active_filter = ""
        if announce:
            self.add_message("system", f"Loaded {len(self.rows)} candidates for: {self.state.get('query', '')}")
        else:
            self.messages.append(("system", f"Loaded {len(self.rows)} candidates for: {self.state.get('query', '')}"))
        self.messages.append(("system", "Results commands: /filter text, /select N, /keep, /reject, /tag tag, /note text, /runs, /skills, /quit"))

    def reload_runs(self) -> None:
        self.refresh_runs(preserve_selection=False)
        self.add_message("system", f"Loaded {len(self.runs)} search runs from {self.runs_dir}")

    def refresh_runs(self, preserve_selection: bool = True) -> None:
        selected_path = None
        if preserve_selection:
            run = self.selected_run()
            selected_path = run.get("path") if run else None
        self.runs = discover_runs(self.runs_dir, self.nanoclaw_dir)
        self.mode = "runs"
        self.filtered_indexes = [
            idx
            for idx, run in enumerate(self.runs)
            if not self.active_filter
            or self.active_filter.lower() in f"{run.get('query', '')} {run.get('task_id', '')} {run.get('status', '')}".lower()
        ]
        self.selected = 0
        if selected_path:
            for pos, idx in enumerate(self.filtered_indexes):
                if self.runs[idx].get("path") == selected_path:
                    self.selected = pos
                    break
        self.scroll = max(0, min(self.scroll, max(0, len(self.filtered_indexes) - 1)))

    def refresh_active_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        mtime = self.state_path.stat().st_mtime
        if mtime <= self.state_mtime:
            return
        selected_rank = None
        row = self.selected_row()
        if row:
            selected_rank = row.get("rank")
        active_filter = self.active_filter
        self.state = read_json(self.state_path)
        self.state_mtime = mtime
        self.rows = results_io.result_rows(self.state)
        self.active_filter = active_filter
        if active_filter:
            needle = active_filter.lower().strip()
            self.filtered_indexes = [idx for idx, row in enumerate(self.rows) if needle in searchable_text(row)]
        else:
            self.filtered_indexes = list(range(len(self.rows)))
        self.selected = 0
        if selected_rank is not None:
            for pos, idx in enumerate(self.filtered_indexes):
                if self.rows[idx].get("rank") == selected_rank:
                    self.selected = pos
                    break
        self.scroll = max(0, min(self.scroll, max(0, len(self.filtered_indexes) - 1)))

    def auto_refresh(self) -> None:
        now = time.time()
        if now - self.last_auto_refresh < 1.0:
            return
        self.last_auto_refresh = now
        self.poll_outbound_once()
        if self.mode == "runs":
            self.refresh_runs(preserve_selection=True)
        else:
            self.refresh_active_state()

    def poll_outbound_once(self) -> None:
        if not self.thread_id:
            return
        for seq, text in thread_outbound_rows(self.nanoclaw_dir, self.thread_id, self.last_outbound_seq):
            self.last_outbound_seq = max(self.last_outbound_seq, seq)
            if not text or text in self.seen_outbound_texts:
                continue
            self.seen_outbound_texts.add(text)
            self.add_message("agent", text)

    def resume_latest_run(self) -> None:
        self.reload_runs()
        if not self.runs:
            self.add_message("system", "No Powerpacks runs to resume")
            return
        self.selected = 0
        self.open_selected_run()

    def show_skills(self) -> None:
        if not self.skills:
            self.add_message("system", f"No Powerpacks skills found under {POWERPACKS_ROOT / 'skills'}")
            return
        self.add_message("system", f"Powerpacks skills ({len(self.skills)}). Use slash form, e.g. /search-network who are software engineers in sf")
        for skill in self.skills:
            name = skill.get("name") or ""
            description = skill.get("description") or ""
            self.add_message("skill", f"/{name} - {description}")

    def suggestions(self, query: str | None = None, limit: int = 6) -> list[dict[str, str]]:
        text = self.input_buffer if query is None else query
        if text.startswith("$"):
            text = "/" + text[1:]
        if not text.startswith("/"):
            return []
        scored = []
        for item in self.completion_items:
            target = f"{item.get('command', '')} {item.get('description', '')}"
            score = fuzzy_score(text, target)
            if score is not None:
                type_bonus = 20 if item.get("type") == "skill" else 0
                scored.append((score + type_bonus, item))
        scored.sort(key=lambda pair: (pair[0], 1 if pair[1].get("type") == "skill" else 0, pair[1].get("command", "")), reverse=True)
        return [item for _, item in scored[:limit]]

    def complete_input(self) -> None:
        suggestions = self.suggestions(limit=1)
        if not suggestions:
            return
        command = suggestions[0].get("command", "")
        if command:
            self.input_buffer = command + " "

    def confirm_ctrl_c_exit(self) -> bool:
        now = time.time()
        if now <= self.ctrl_c_deadline:
            self.persist_event("quit", {"via": "ctrl-c"})
            return True
        self.ctrl_c_deadline = now + 3
        self.add_message("system", "Press Ctrl-C again within 3 seconds to exit. Use /quit to exit immediately.")
        return False

    def resume_instructions(self) -> list[str]:
        resume_chat = f"cd {self.nanoclaw_dir} && ./powerclaw"
        if self.thread_id:
            resume_chat = f"{resume_chat} --resume {self.thread_id}"
        lines = [
            "Exited PowerClaw.",
            f"Resume chat: {resume_chat}",
            f"New chat thread: cd {self.nanoclaw_dir} && ./powerclaw --new",
            f"Resume latest search run: cd {self.nanoclaw_dir} && ./powerclaw --resume-run",
        ]
        if self.state_path:
            lines.append(f"Resume this result set: cd {self.nanoclaw_dir} && ./powerclaw --state {self.state_path}")
        if self.thread_id:
            lines.append(f"NanoClaw thread id: {self.thread_id}")
        return lines

    def show_session_status(self) -> None:
        db_path = self.nanoclaw_dir / "data" / "v2.db"
        if not db_path.exists():
            self.add_message("system", f"No NanoClaw session DB found at {db_path}")
            return
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, agent_group_id, messaging_group_id, thread_id, status, container_status, last_active, created_at
                FROM sessions
                ORDER BY created_at DESC
                LIMIT 8
                """
            ).fetchall()
        except sqlite3.Error as exc:
            self.add_message("system", f"Could not read NanoClaw sessions: {exc}")
            return
        finally:
            if conn:
                conn.close()
        if not rows:
            self.add_message("system", "No NanoClaw sessions found")
            return
        self.add_message("system", "NanoClaw sessions. CLI is wired in shared mode, so normal chat resumes the same CLI session.")
        for row in rows:
            thread = row["thread_id"] or "shared"
            self.add_message(
                "session",
                f"{row['id']} | {row['status']}/{row['container_status']} | thread={thread} | last={row['last_active'] or row['created_at']}",
            )

    def visible_count(self) -> int:
        return len(self.filtered_indexes)

    def selected_run(self) -> dict[str, Any] | None:
        if self.mode != "runs" or not self.filtered_indexes:
            return None
        self.selected = max(0, min(self.selected, len(self.filtered_indexes) - 1))
        return self.runs[self.filtered_indexes[self.selected]]

    def selected_row(self) -> dict[str, Any] | None:
        if self.mode != "results" or not self.filtered_indexes:
            return None
        self.selected = max(0, min(self.selected, len(self.filtered_indexes) - 1))
        return self.rows[self.filtered_indexes[self.selected]]

    def persist_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self.state_path or not self.review_log:
            return
        row = self.selected_row()
        event = {
            "event": event_type,
            "timestamp": now_iso(),
            "task_id": self.state.get("task_id"),
            "state": str(self.state_path),
            "query": self.state.get("query"),
            **payload,
        }
        if row:
            event["person_id"] = row.get("person_id")
            event["rank"] = row.get("rank")
            event["candidate"] = {
                key: row.get(key)
                for key in ["name", "headline", "location", "current_titles", "current_companies", "linkedin_url", "hydrated"]
            }
        append_jsonl(self.review_log, event)

    def add_message(self, speaker: str, text: str) -> None:
        text = clean_display_text(speaker, text)
        with self.message_lock:
            for line in str(text).splitlines() or [""]:
                self.messages.append((speaker, line))
            self.messages = self.messages[-MESSAGE_HISTORY_LIMIT:]

    def add_debug_message(self, text: str) -> None:
        if os.environ.get("POWERPACKS_TUI_DEBUG_SYSTEM") == "1":
            self.add_message("system", text)

    def approval_prompt_active(self) -> bool:
        with self.message_lock:
            recent = list(self.messages[-80:])
        last_user = max((idx for idx, (speaker, _text) in enumerate(recent) if speaker == "you"), default=-1)
        last_approval = max(
            (idx for idx, (speaker, text) in enumerate(recent) if speaker == "agent" and is_approval_prompt_text(text)),
            default=-1,
        )
        last_terminal = max(
            (idx for idx, (speaker, text) in enumerate(recent) if speaker == "agent" and is_terminal_agent_text(text)),
            default=-1,
        )
        return last_approval > last_user and last_approval > last_terminal

    def is_agent_busy(self) -> bool:
        with self.agent_state_lock:
            return bool(self.agent_thread and self.agent_thread.is_alive())

    def queue_message(self, text: str) -> None:
        with self.agent_state_lock:
            self.pending_messages.append(text)
            queue_len = len(self.pending_messages)
        self.add_message("system", f"Queued message #{queue_len}. Press Esc to interrupt the current wait and send it immediately.")

    def apply_filter(self, query: str) -> None:
        self.active_filter = query
        needle = query.lower().strip()
        if self.mode == "runs":
            if not needle:
                self.filtered_indexes = list(range(len(self.runs)))
            else:
                self.filtered_indexes = [
                    idx
                    for idx, run in enumerate(self.runs)
                    if needle in f"{run.get('query', '')} {run.get('task_id', '')} {run.get('status', '')}".lower()
                ]
            self.add_message("system", f"Run filter '{query}' -> {len(self.filtered_indexes)} runs")
        else:
            if not needle:
                self.filtered_indexes = list(range(len(self.rows)))
            else:
                self.filtered_indexes = [idx for idx, row in enumerate(self.rows) if needle in searchable_text(row)]
            self.add_message("system", f"Candidate filter '{query}' -> {len(self.filtered_indexes)} candidates")
            self.persist_event("filter", {"filter": query, "result_count": len(self.filtered_indexes)})
        self.selected = 0
        self.scroll = 0

    def open_selected_run(self) -> None:
        run = self.selected_run()
        if not run:
            self.add_message("system", "No search run selected")
            return
        self.load_state(Path(str(run["path"])))

    def select_rank(self, rank_text: str) -> None:
        if self.mode == "runs":
            try:
                index = int(rank_text) - 1
            except ValueError:
                self.add_message("system", f"Invalid run index: {rank_text}")
                return
            if 0 <= index < len(self.filtered_indexes):
                self.selected = index
                self.open_selected_run()
            else:
                self.add_message("system", f"Run index {rank_text} is not visible")
            return

        try:
            rank = int(rank_text)
        except ValueError:
            self.add_message("system", f"Invalid rank: {rank_text}")
            return
        for pos, idx in enumerate(self.filtered_indexes):
            if int(self.rows[idx].get("rank", -1)) == rank:
                self.selected = pos
                self.ensure_visible(20)
                self.describe_selected()
                return
        self.add_message("system", f"Rank {rank} is not visible in the current filter")

    def describe_selected(self) -> None:
        if self.mode == "runs":
            run = self.selected_run()
            if not run:
                self.add_message("system", "No search run selected")
                return
            self.add_message("run", f"{run.get('query')} | {run.get('row_count')} rows | {run.get('path')}")
            return

        row = self.selected_row()
        if not row:
            self.add_message("system", "No candidate selected")
            return
        summary = (
            f"#{row.get('rank')} {row.get('name') or '(unhydrated)'} | "
            f"{row.get('headline') or row.get('person_id')} | "
            f"{row.get('current_companies') or 'unknown company'} | "
            f"{row.get('location') or 'unknown location'}"
        )
        self.add_message("candidate", summary)

    def mark_selected(self, mark: str, note: str = "") -> None:
        row = self.selected_row()
        if not row:
            self.add_message("system", "No candidate selected")
            return
        person_id = str(row.get("person_id"))
        self.review_marks[person_id] = mark
        self.persist_event(mark, {"note": note})
        label = row.get("name") or person_id
        self.add_message("review", f"{mark}: {label}{f' - {note}' if note else ''}")

    def agent_prompt(self, user_text: str) -> str:
        context = [
            user_text,
            "",
            "Powerpacks UI context:",
            f"- runs_dir: {self.runs_dir}",
        ]
        if self.thread_id:
            context.append(f"- nanoclaw_thread_id: {self.thread_id}")
        if self.state_path:
            context.extend([
                f"- active_state: {self.state_path}",
                f"- active_query: {self.state.get('query', '')}",
                f"- review_log: {self.review_log or ''}",
                f"- visible_candidates: {len(self.filtered_indexes)}",
            ])
            row = self.selected_row()
            if row:
                context.append(f"- selected_candidate: #{row.get('rank')} {row.get('name') or row.get('person_id')}")
        else:
            run = self.selected_run()
            if run:
                context.extend([
                    f"- selected_run: {run.get('path')}",
                    f"- selected_run_query: {run.get('query')}",
                ])
        return "\n".join(context)

    def send_to_agent(self, text: str) -> None:
        if not self.agent_command:
            self.add_message("agent", "No NanoClaw bridge configured. Use --agent-command or POWERPACKS_AGENT_COMMAND.")
            return
        if self.is_agent_busy():
            self.queue_message(text)
            return
        self.start_agent_message(text)

    def start_agent_message(self, text: str) -> None:
        prompt = self.agent_prompt(text)
        argv = agent_argv(self.agent_command, prompt)
        interrupt = threading.Event()
        with self.agent_state_lock:
            self.busy_started_at = time.time()
            self.current_work_text = text
            self.current_interrupt = interrupt
            self.current_process = None
            self.agent_thread = threading.Thread(target=self.run_agent_command, args=(argv, text, interrupt), daemon=True)
            self.agent_thread.start()
        self.add_debug_message("Sending to NanoClaw...")

    def interrupt_current_wait(self) -> None:
        with self.agent_state_lock:
            interrupt = self.current_interrupt
            process = self.current_process
            has_pending = bool(self.pending_messages)
        if not interrupt:
            return
        interrupt.set()
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if has_pending:
            self.add_message("system", "Interrupted local wait; queued message will send next.")
        else:
            self.add_message("system", "Interrupted local wait. NanoClaw may keep working in the background.")

    def finish_agent_command(self) -> None:
        next_text = None
        with self.agent_state_lock:
            self.current_process = None
            self.current_interrupt = None
            self.busy_started_at = None
            self.current_work_text = ""
            if self.pending_messages:
                next_text = self.pending_messages.pop(0)
        if self.mode == "runs":
            self.refresh_runs(preserve_selection=True)
        else:
            self.refresh_active_state()
        if next_text:
            self.start_agent_message(next_text)

    def run_agent_command(self, argv: list[str], original_text: str, interrupt: threading.Event) -> None:
        try:
            if self.auto_start and not self.ensure_nanoclaw_running():
                return
            baseline_seq = 0
            if self.thread_id:
                rows = thread_outbound_rows(self.nanoclaw_dir, self.thread_id)
                if rows:
                    baseline_seq = max(seq for seq, _text in rows)
            try:
                process = subprocess.Popen(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except FileNotFoundError as exc:
                self.add_message("agent", f"NanoClaw command failed: {exc}")
                return
            with self.agent_state_lock:
                self.current_process = process
            while process.poll() is None:
                if interrupt.is_set():
                    try:
                        output, error = process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        except OSError:
                            pass
                        output, error = process.communicate()
                    if output.strip():
                        self.add_message("agent", output.strip())
                    if error.strip():
                        self.add_message("agent", error.strip())
                    return
                time.sleep(0.1)

            output, error = process.communicate()
            output = (output or "").strip()
            error = (error or "").strip()
            seen_texts = set()
            if output:
                self.add_message("agent", output)
                seen_texts.add(output)
                self.seen_outbound_texts.add(output)
            if error:
                self.add_message("agent", error)
            if not output and not error:
                self.add_message("agent", f"NanoClaw exited with code {process.returncode} and no output")
            if process.returncode != 0:
                self.add_message("system", f"NanoClaw exit code: {process.returncode}")
            should_poll_followups = (
                bool(self.thread_id)
                and (
                    original_text.strip().startswith("/search-network")
                    or is_approval_reply_text(original_text)
                    or os.environ.get("POWERPACKS_TUI_POLL_ALL") == "1"
                )
            )
            if should_poll_followups:
                self.poll_thread_followups(baseline_seq, seen_texts, interrupt)
        finally:
            self.finish_agent_command()

    def poll_thread_followups(self, baseline_seq: int, seen_texts: set[str], interrupt: threading.Event) -> None:
        if not self.thread_id:
            return
        max_seconds = int(os.environ.get("POWERPACKS_TUI_THREAD_TIMEOUT_SECONDS", "3600"))
        idle_seconds = int(os.environ.get("POWERPACKS_TUI_IDLE_TIMEOUT_SECONDS", "300"))
        status_seconds = int(os.environ.get("POWERPACKS_TUI_STATUS_SECONDS", "30"))
        started = time.time()
        last_healthy = time.time()
        last_status = 0.0
        last_seq = baseline_seq
        last_message = time.time()
        while True:
            if interrupt.is_set():
                return
            elapsed = time.time() - started
            if max_seconds > 0 and elapsed > max_seconds:
                self.add_message("system", f"Stopped watching NanoClaw after {max_seconds}s.")
                return
            for seq, text in thread_outbound_rows(self.nanoclaw_dir, self.thread_id, last_seq):
                last_seq = max(last_seq, seq)
                if text and text not in seen_texts:
                    if text in self.seen_outbound_texts:
                        continue
                    self.seen_outbound_texts.add(text)
                    self.add_message("agent", text)
                    seen_texts.add(text)
                    last_message = time.time()
                    if is_approval_prompt_text(text):
                        return

            heartbeat_age = thread_heartbeat_age(self.nanoclaw_dir, self.thread_id)
            if heartbeat_age is None or heartbeat_age <= idle_seconds:
                last_healthy = time.time()
            if time.time() - last_healthy > idle_seconds:
                self.add_message("system", f"NanoClaw appears idle/unhealthy for >{idle_seconds}s; stopped watching thread.")
                return
            if thread_inbound_completed(self.nanoclaw_dir, self.thread_id) and time.time() - last_message > 0.5:
                return
            if status_seconds > 0 and time.time() - last_status > status_seconds:
                age_text = "unknown" if heartbeat_age is None else f"{heartbeat_age:.0f}s"
                self.add_debug_message(f"NanoClaw still working; heartbeat age {age_text}.")
                last_status = time.time()
            for _ in range(20):
                if interrupt.is_set():
                    return
                time.sleep(0.1)

    def ensure_nanoclaw_running(self) -> bool:
        sock_path = cli_threaded_socket_path(self.nanoclaw_dir) if self.thread_id else nanoclaw_socket_path(self.nanoclaw_dir)
        if socket_reachable(sock_path):
            self.add_debug_message(f"NanoClaw daemon is reachable at {sock_path}")
            return True
        base_sock_path = nanoclaw_socket_path(self.nanoclaw_dir)
        if self.thread_id and socket_reachable(base_sock_path):
            self.add_message("agent", f"NanoClaw is running, but {sock_path} is missing. Restart NanoClaw after installing cli-threaded.")
            return False
        if not self.start_command:
            self.add_message("agent", f"NanoClaw daemon is not reachable at {sock_path}. No start command is configured.")
            return False

        log_dir = POWERPACKS_ROOT / ".powerpacks" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "nanoclaw-daemon.log"
        self.add_message("system", f"NanoClaw socket missing; starting daemon. Log: {log_path}")
        argv = shlex.split(self.start_command)
        env = os.environ.copy()
        env.setdefault("LOG_LEVEL", "warn")
        try:
            log_handle = log_path.open("a")
            subprocess.Popen(
                argv,
                cwd=str(self.nanoclaw_dir),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            self.add_message("agent", f"Failed to start NanoClaw: {exc}")
            return False

        deadline = time.time() + 45
        while time.time() < deadline:
            if socket_reachable(sock_path):
                self.add_debug_message(f"NanoClaw daemon is reachable at {sock_path}")
                return True
            time.sleep(0.5)
        self.add_message("agent", f"NanoClaw did not become reachable within 45s. Check {log_path}")
        return False

    def handle_command(self, text: str) -> bool:
        stripped = text.strip()
        if stripped.startswith("$"):
            stripped = "/" + stripped[1:]
        if not stripped:
            if self.mode == "runs":
                self.open_selected_run()
            else:
                self.describe_selected()
            return True
        self.add_message("you", stripped)

        if stripped in {"/q", "/quit", "/exit"}:
            self.persist_event("quit", {})
            return False
        if stripped == "/help":
            self.add_message("system", "Commands: /skills, /session, /resume, /runs, /back, /reload, /start-nanoclaw, /filter text, /clear, /select N, /open, /keep, /reject, /tag, /note, /quit")
            self.add_message("system", "Skill calls use slash form, e.g. /search-network who are software engineers in sf. Plain text is sent to NanoClaw.")
            return True
        if stripped in {"/skills", "/skill"}:
            self.show_skills()
            return True
        if stripped in {"/session", "/sessions", "/threads"}:
            self.show_session_status()
            return True
        if stripped == "/resume":
            self.resume_latest_run()
            return True
        if stripped in {"/start-nanoclaw", "/start"}:
            self.ensure_nanoclaw_running()
            return True
        if stripped in {"/runs", "/searches"}:
            self.reload_runs()
            return True
        if stripped in {"/back", "/top"}:
            self.reload_runs()
            return True
        if stripped == "/reload":
            if self.mode == "runs":
                self.reload_runs()
            elif self.state_path:
                self.load_state(self.state_path)
            return True
        if stripped == "/clear":
            self.apply_filter("")
            return True
        if stripped.startswith("/filter "):
            self.apply_filter(stripped[len("/filter "):])
            return True
        if stripped.startswith("/select "):
            self.select_rank(stripped[len("/select "):].strip())
            return True
        if stripped == "/open":
            if self.mode == "runs":
                self.open_selected_run()
            else:
                row = self.selected_row()
                url = row.get("linkedin_url") if row else ""
                self.add_message("system", url or "No LinkedIn URL for selected candidate")
                self.persist_event("open_linkedin", {"url": url})
            return True
        if stripped.startswith("/keep"):
            self.mark_selected("keep", stripped[len("/keep"):].strip())
            return True
        if stripped.startswith("/reject"):
            self.mark_selected("reject", stripped[len("/reject"):].strip())
            return True
        if stripped.startswith("/tag "):
            tags = [tag for tag in stripped[len("/tag "):].replace(",", " ").split() if tag]
            self.persist_event("tag", {"tags": tags})
            self.add_message("review", f"tagged selected candidate: {', '.join(tags)}")
            return True
        if stripped.startswith("/note "):
            note = stripped[len("/note "):].strip()
            self.persist_event("note", {"note": note})
            self.add_message("review", f"noted selected candidate: {note}")
            return True

        self.persist_event("chat", {"text": stripped})
        self.send_to_agent(stripped)
        return True

    def ensure_visible(self, list_height: int) -> None:
        if self.selected < self.scroll:
            self.scroll = self.selected
        if self.selected >= self.scroll + list_height:
            self.scroll = self.selected - list_height + 1
        self.scroll = max(0, min(self.scroll, max(0, len(self.filtered_indexes) - list_height)))

    def draw_text(self, win: Any, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
        try:
            win.addstr(y, x, truncate(text, width), attr)
        except curses.error:
            pass

    def draw(self, stdscr: Any) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        min_width = 86
        min_height = 20
        if width < min_width or height < min_height:
            self.draw_text(stdscr, 0, 0, f"Resize terminal to at least {min_width}x{min_height}", width - 1)
            stdscr.refresh()
            return

        input_height = 9
        body_height = height - input_height
        right_width = max(42, min(68, width // 3))
        left_width = width - right_width - 1

        self.draw_chat(stdscr, 0, 0, body_height, left_width)
        if self.mode == "runs":
            self.draw_runs(stdscr, 0, left_width + 1, body_height, right_width)
        else:
            self.draw_candidates(stdscr, 0, left_width + 1, body_height, right_width)
        self.draw_input(stdscr, body_height, 0, input_height, width)
        stdscr.refresh()

    def draw_chat(self, win: Any, y0: int, x0: int, height: int, width: int) -> None:
        task_id = self.state.get("task_id", "run browser") if self.state else "run browser"
        title = f" NanoClaw Chat | {task_id} "
        self.draw_text(win, y0, x0, title.ljust(width, "-"), width, curses.A_BOLD)
        with self.message_lock:
            messages = list(self.messages)
        lines = []
        for speaker, text in messages:
            lines.extend(wrap_chat_line(speaker, text, width - 1))
        visible = lines[-(height - 2):]
        for offset, line in enumerate(visible, start=1):
            self.draw_text(win, y0 + offset, x0, line, width - 1)

    def draw_runs(self, win: Any, y0: int, x0: int, height: int, width: int) -> None:
        title = f" Searches {len(self.filtered_indexes)}/{len(self.runs)} "
        if self.active_filter:
            title += f" filter:{self.active_filter} "
        self.draw_text(win, y0, x0, title.ljust(width, "-"), width, curses.A_BOLD)
        list_height = height - 2
        self.ensure_visible(list_height)
        for line_no in range(list_height):
            pos = self.scroll + line_no
            if pos >= len(self.filtered_indexes):
                break
            run = self.runs[self.filtered_indexes[pos]]
            selected = ">" if pos == self.selected else " "
            status = str(run.get("status") or "")[:1].upper() or " "
            text = f"{selected}{pos + 1:3} {run.get('row_count', 0):4} {status} {run.get('query', '')}"
            attr = curses.A_REVERSE if pos == self.selected else 0
            self.draw_text(win, y0 + 1 + line_no, x0, text, width - 1, attr)
        footer = "Enter opens selected search"
        self.draw_text(win, y0 + height - 1, x0, footer.ljust(width, "-"), width)

    def draw_candidates(self, win: Any, y0: int, x0: int, height: int, width: int) -> None:
        title = f" Results {len(self.filtered_indexes)}/{len(self.rows)} "
        if self.active_filter:
            title += f" filter:{self.active_filter} "
        self.draw_text(win, y0, x0, title.ljust(width, "-"), width, curses.A_BOLD)
        list_height = height - 2
        self.ensure_visible(list_height)
        for line_no in range(list_height):
            pos = self.scroll + line_no
            if pos >= len(self.filtered_indexes):
                break
            row = self.rows[self.filtered_indexes[pos]]
            person_id = str(row.get("person_id", ""))
            mark = self.review_marks.get(person_id, " ")
            hydrated = "H" if row.get("hydrated") else " "
            selected = ">" if pos == self.selected else " "
            name = row.get("name") or person_id[:8]
            company = row.get("current_companies") or ""
            rank = row.get("rank")
            text = f"{selected}{str(rank).rjust(3)} {mark}{hydrated} {name} | {company}"
            attr = curses.A_REVERSE if pos == self.selected else 0
            self.draw_text(win, y0 + 1 + line_no, x0, text, width - 1, attr)
        row = self.selected_row()
        footer = ""
        if row:
            footer = f"#{row.get('rank')} {row.get('headline') or ''}"
        self.draw_text(win, y0 + height - 1, x0, footer.ljust(width, "-"), width)

    def draw_input(self, win: Any, y0: int, x0: int, height: int, width: int) -> None:
        label = " Input: / commands, Tab completes, plain text chats with NanoClaw "
        self.draw_text(win, y0, x0, label.ljust(width, "-"), width, curses.A_BOLD)
        prompt = "> " + self.input_buffer
        self.draw_text(win, y0 + 1, x0, prompt, width - 1)
        next_line = 3
        with self.agent_state_lock:
            busy_started_at = self.busy_started_at
            current_work_text = self.current_work_text
            pending = list(self.pending_messages)
            busy = bool(self.agent_thread and self.agent_thread.is_alive())
        if busy and busy_started_at:
            spinner = "|/-\\"[int(time.time() * 4) % 4]
            elapsed = format_duration(time.time() - busy_started_at)
            status = f"{spinner} Working ({elapsed} • esc to interrupt)"
            if current_work_text:
                status += f" • {current_work_text}"
            self.draw_text(win, y0 + next_line - 1, x0, status, width - 1, curses.A_DIM)
        if pending:
            self.draw_text(
                win,
                y0 + next_line,
                x0,
                "• Messages to be submitted after current response (press esc to interrupt and send immediately)",
                width - 1,
                curses.A_DIM,
            )
            next_line += 1
            for pending_text in pending[: max(0, height - next_line - 1)]:
                self.draw_text(win, y0 + next_line, x0, f"  ↳ {pending_text}", width - 1, curses.A_DIM)
                next_line += 1
        if self.approval_prompt_active():
            self.draw_text(
                win,
                y0 + next_line,
                x0,
                "[1] Approve   [2] Yolo / skip future gates   [3] Tell NanoClaw what to change",
                width - 1,
                curses.A_BOLD,
            )
            next_line += 1
        suggestions = self.suggestions(limit=max(0, height - next_line))
        for offset, item in enumerate(suggestions, start=next_line):
            marker = "skill" if item.get("type") == "skill" else "cmd"
            text = f"  {item.get('command', ''):<20} {marker:<5} {item.get('description', '')}"
            self.draw_text(win, y0 + offset, x0, text, width - 1, curses.A_DIM)
        try:
            win.move(y0 + 1, min(width - 1, len(prompt)))
        except curses.error:
            pass

    def run(self, stdscr: Any) -> None:
        curses.curs_set(1)
        stdscr.keypad(True)
        stdscr.timeout(100)
        while True:
            self.auto_refresh()
            self.draw(stdscr)
            try:
                key = stdscr.getch()
            except KeyboardInterrupt:
                if self.confirm_ctrl_c_exit():
                    break
                continue
            if key == -1:
                continue
            if key in (curses.KEY_UP,):
                self.selected = max(0, self.selected - 1)
            elif key in (curses.KEY_DOWN,):
                self.selected = min(max(0, len(self.filtered_indexes) - 1), self.selected + 1)
            elif key in (curses.KEY_LEFT,):
                self.reload_runs()
            elif key in (curses.KEY_NPAGE,):
                self.selected = min(max(0, len(self.filtered_indexes) - 1), self.selected + 10)
            elif key in (curses.KEY_PPAGE,):
                self.selected = max(0, self.selected - 10)
            elif key in (10, 13):
                command = self.input_buffer
                self.input_buffer = ""
                if not self.handle_command(command):
                    break
            elif key in (3,):
                if self.confirm_ctrl_c_exit():
                    break
            elif key in (27,):
                if self.is_agent_busy():
                    self.interrupt_current_wait()
                else:
                    self.add_message("system", "Escape ignored. Use /quit or press Ctrl-C twice to exit.")
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.input_buffer = self.input_buffer[:-1]
            elif key in (9,):
                self.complete_input()
            elif self.approval_prompt_active() and not self.input_buffer and key in (ord("1"), ord("2"), ord("3")):
                if key == ord("1"):
                    if not self.handle_command("approved"):
                        break
                elif key == ord("2"):
                    if not self.handle_command("yolo"):
                        break
                else:
                    self.input_buffer = "change: "
            elif 0 <= key <= 255:
                char = chr(key)
                if char.isprintable():
                    if char == "$" and not self.input_buffer:
                        self.input_buffer += "/"
                    else:
                        self.input_buffer += char


def build_tui(args: argparse.Namespace) -> SearchTui:
    state_path = Path(args.state) if args.state else None
    runs_dir = Path(args.runs_dir)
    review_log = Path(args.review_log) if args.review_log else None
    nanoclaw_dir = Path(args.nanoclaw_dir)
    thread_id = args.thread_id
    agent_command = args.agent_command if args.agent_command is not None else default_agent_command(nanoclaw_dir, thread_id)
    start_command = args.nanoclaw_start_command if args.nanoclaw_start_command is not None else default_start_command(nanoclaw_dir)
    return SearchTui(state_path, runs_dir, review_log, nanoclaw_dir, thread_id, agent_command, start_command, not args.no_auto_start)


def cmd_dump(args: argparse.Namespace) -> None:
    tui = build_tui(args)
    if tui.mode == "runs":
        print(json.dumps({
            "runs_dir": str(tui.runs_dir),
            "run_count": len(tui.runs),
            "agent_command": tui.agent_command,
            "start_command": tui.start_command,
            "socket": str(nanoclaw_socket_path(tui.nanoclaw_dir)),
            "threaded_socket": str(cli_threaded_socket_path(tui.nanoclaw_dir)),
            "socket_reachable": socket_reachable(nanoclaw_socket_path(tui.nanoclaw_dir)),
            "threaded_socket_reachable": socket_reachable(cli_threaded_socket_path(tui.nanoclaw_dir)),
            "thread_id": tui.thread_id,
            "sample": tui.runs[: args.limit],
        }, indent=2, sort_keys=True))
        return
    print(json.dumps({
        "state": str(tui.state_path),
        "task_id": tui.state.get("task_id"),
        "query": tui.state.get("query"),
        "row_count": len(tui.rows),
        "hydrated_count": sum(1 for row in tui.rows if row.get("hydrated")),
        "review_log": str(tui.review_log),
        "agent_command": tui.agent_command,
        "start_command": tui.start_command,
        "socket": str(nanoclaw_socket_path(tui.nanoclaw_dir)),
        "threaded_socket": str(cli_threaded_socket_path(tui.nanoclaw_dir)),
        "socket_reachable": socket_reachable(nanoclaw_socket_path(tui.nanoclaw_dir)),
        "threaded_socket_reachable": socket_reachable(cli_threaded_socket_path(tui.nanoclaw_dir)),
        "thread_id": tui.thread_id,
        "sample": tui.rows[: args.limit],
    }, indent=2, sort_keys=True))


def cmd_commands(args: argparse.Namespace) -> None:
    tui = build_tui(args)
    executed = []
    for command in args.command or []:
        executed.append(command)
        if not tui.handle_command(command):
            break
    deadline = time.time() + 30
    while time.time() < deadline:
        thread = tui.agent_thread
        if thread and thread.is_alive():
            thread.join(timeout=0.25)
            continue
        with tui.agent_state_lock:
            if not tui.pending_messages:
                break
        time.sleep(0.05)
    selected = tui.selected_row() if tui.mode == "results" else tui.selected_run()
    print(json.dumps({
        "mode": tui.mode,
        "state": str(tui.state_path) if tui.state_path else None,
        "task_id": tui.state.get("task_id"),
        "query": tui.state.get("query"),
        "review_log": str(tui.review_log) if tui.review_log else None,
        "agent_command": tui.agent_command,
        "start_command": tui.start_command,
        "socket_reachable": socket_reachable(nanoclaw_socket_path(tui.nanoclaw_dir)),
        "threaded_socket_reachable": socket_reachable(cli_threaded_socket_path(tui.nanoclaw_dir)),
        "thread_id": tui.thread_id,
        "executed": executed,
        "visible_count": tui.visible_count(),
        "selected": selected,
        "messages": tui.messages[-10:],
    }, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with NanoClaw while browsing Powerpacks search results")
    parser.add_argument("--state", help="Optional task state JSON. If omitted, right pane lists prior searches.")
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    parser.add_argument("--review-log")
    parser.add_argument("--agent-command", help="One-shot agent command. Use {prompt} placeholder or prompt is appended.")
    parser.add_argument("--nanoclaw-dir", default=str(DEFAULT_NANOCLAW_DIR))
    parser.add_argument("--thread-id")
    parser.add_argument("--nanoclaw-start-command", help="Long-running daemon command used when the NanoClaw socket is missing.")
    parser.add_argument("--no-auto-start", action="store_true", help="Do not start NanoClaw automatically before chat.")
    parser.add_argument("--dump", action="store_true", help="Print parsed data instead of opening curses")
    parser.add_argument("--suggest", help="Print autocomplete suggestions for input text and exit.")
    parser.add_argument("--command", action="append", help="Run a TUI command non-interactively; repeatable")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    if args.dump:
        cmd_dump(args)
        return
    if args.suggest is not None:
        tui = build_tui(args)
        print(json.dumps(tui.suggestions(args.suggest, limit=args.limit), indent=2, sort_keys=True))
        return
    if args.command:
        cmd_commands(args)
        return

    tui = build_tui(args)
    try:
        curses.wrapper(tui.run)
    except KeyboardInterrupt:
        pass
    print()
    for line in tui.resume_instructions():
        print(line)


if __name__ == "__main__":
    main()
