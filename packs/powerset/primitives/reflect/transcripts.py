"""Parse Claude Code and Codex session transcripts into typed turns and tool calls.

Flow:
- `parse(path)` picks the parser: a Codex rollout's first line is `session_meta`.
- Claude Code (`~/.claude/projects/<slug>/<session>.jsonl`): user/assistant
  lines; `tool_use` blocks pair with later `tool_result` blocks by id. A failing
  Bash result starts with `Exit code N`.
- Codex (`~/.codex/sessions/.../rollout-*.jsonl`): text from `response_item`
  messages; commands and MCP calls from `item_completed` events.
- Harness-injected user text (`<tag>…</tag>` blocks, `# AGENTS.md instructions`)
  is dropped; a Claude slash command block becomes the text `/<name> <args>`.
  Identical user turns are kept once (Codex replays history after compaction).

Changelog:
- 2026-09-30: created from the reflection prototype.
"""
from __future__ import annotations

import json
import re
from dataclasses import KW_ONLY, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

Harness = Literal["claude-code", "codex"]
Role = Literal["user", "assistant"]

CLAUDE_CODE: Harness = "claude-code"
CODEX: Harness = "codex"

_TAG_BLOCK = re.compile(r"<([a-zA-Z_-]+)[^>]*>.*?</\1>\s*", re.S)
_COMMAND_NAME = re.compile(r"<command-name>\s*(/[^<\s]+)\s*</command-name>")
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_AGENTS_INJECTION = "# AGENTS.md instructions"
_EXIT_CODE = re.compile(r"\s*Exit code (\d+)")
_SHELL_LC = ("-lc", "-c")
_TOOL_INPUT_KEYS = ("command", "file_path", "pattern", "prompt", "skill", "url")
_INPUT_FALLBACK_CHARS = 200
_MCP_OUTPUT_CHARS = 2000
_BASH_TOOLS = ("Bash",)
_MCP_OK = "completed"
_GENERIC_FAILURE_CODE = 1


@dataclass(frozen=True)
class Turn:
    ts: float
    role: Role
    text: str


@dataclass(frozen=True)
class ToolCall:
    started: float
    ended: float
    name: str
    command: str
    output: str
    exit_code: int | None
    _: KW_ONLY
    is_shell: bool


@dataclass(frozen=True)
class Session:
    harness: Harness
    path: str
    cwd: str
    models: tuple[str, ...]
    turns: tuple[Turn, ...]
    tools: tuple[ToolCall, ...]

    @property
    def user_turns(self) -> tuple[Turn, ...]:
        return tuple(t for t in self.turns if t.role == "user")


def parse(path: Path) -> Session:
    return parse_codex(path) if detect_harness(path) == CODEX else parse_claude(path)


def detect_harness(path: Path) -> Harness:
    with path.open(errors="replace") as fh:
        head = fh.readline()
    return CODEX if '"session_meta"' in head else CLAUDE_CODE


def user_text(text: str) -> str:
    """What the human typed: slash commands kept as `/name args`, injected blocks dropped."""
    command = _COMMAND_NAME.search(text)
    if command:
        args = _COMMAND_ARGS.search(text)
        return f"{command.group(1)} {args.group(1).strip() if args else ''}".strip()
    text = _TAG_BLOCK.sub("", text).strip()
    if text.startswith(_AGENTS_INJECTION):
        return ""
    return text


def parse_claude(path: Path) -> Session:
    cwd = ""
    models: list[str] = []
    turns: list[Turn] = []
    tools: list[ToolCall] = []
    pending: dict[str, tuple[float, str, str]] = {}
    for record in _records(path):
        kind = record.get("type")
        if kind not in ("user", "assistant"):
            continue
        ts = _iso(record.get("timestamp"))
        cwd = cwd or record.get("cwd") or ""
        message = record.get("message") or {}
        model = message.get("model")
        if model and model not in models:
            models.append(model)
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = user_text(block.get("text", "")) if kind == "user" else block.get("text", "").strip()
                if text:
                    turns.append(Turn(ts, kind, text))
                continue
            if block_type == "tool_use":
                pending[block.get("id", "")] = (ts, block.get("name", ""), _tool_input_text(block.get("input")))
                continue
            if block_type == "tool_result":
                tools.append(_claude_tool_result(block, ts, pending))
    return Session(CLAUDE_CODE, str(path), cwd, tuple(models), _dedupe_users(turns), tuple(tools))


def parse_codex(path: Path) -> Session:
    cwd = ""
    models: list[str] = []
    turns: list[Turn] = []
    tools: list[ToolCall] = []
    for record in _records(path):
        payload = record.get("payload") or {}
        kind = record.get("type")
        if kind == "session_meta":
            cwd = cwd or payload.get("cwd") or ""
            continue
        if kind == "turn_context":
            model = payload.get("model")
            if model and model not in models:
                models.append(model)
            continue
        ts = _iso(record.get("timestamp"))
        if kind == "response_item" and payload.get("type") == "message":
            turn = _codex_turn(payload, ts)
            if turn:
                turns.append(turn)
            continue
        if kind != "event_msg" or payload.get("type") != "item_completed":
            continue
        call = _codex_item(payload, ts)
        if call:
            tools.append(call)
    return Session(CODEX, str(path), cwd, tuple(models), _dedupe_users(turns), tuple(tools))


def _records(path: Path):
    with path.open(errors="replace") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _iso(ts: str | None) -> float:
    if not ts:
        return 0.0
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def _tool_input_text(raw: Any) -> str:
    if not isinstance(raw, dict):
        return str(raw)
    for key in _TOOL_INPUT_KEYS:
        if raw.get(key):
            return str(raw[key])
    return json.dumps(raw)[:_INPUT_FALLBACK_CHARS]


def _claude_tool_result(block: dict, ts: float, pending: dict[str, tuple[float, str, str]]) -> ToolCall:
    started, name, command = pending.pop(block.get("tool_use_id", ""), (ts, "?", "?"))
    raw = block.get("content")
    if isinstance(raw, str):
        output = raw
    else:
        output = " ".join(item.get("text", "") for item in raw or [] if isinstance(item, dict))
    return ToolCall(started, ts, name, command, output, _claude_exit_code(name, output, block), is_shell=name in _BASH_TOOLS)


def _claude_exit_code(name: str, output: str, block: dict) -> int | None:
    match = _EXIT_CODE.match(output)
    if match:
        return int(match.group(1))
    if block.get("is_error"):
        return _GENERIC_FAILURE_CODE
    if name in _BASH_TOOLS:
        return 0
    return None


def _codex_turn(payload: dict, ts: float) -> Turn | None:
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return None
    text = " ".join(item.get("text", "") for item in payload.get("content") or [] if isinstance(item, dict))
    text = user_text(text) if role == "user" else text.strip()
    return Turn(ts, role, text) if text else None


def _codex_item(payload: dict, ts: float) -> ToolCall | None:
    item = payload.get("item") or {}
    started = (payload.get("started_at_ms") or 0) / 1000 or ts
    duration = _duration(item.get("duration"))
    completed = (payload.get("completed_at_ms") or 0) / 1000 or started + duration
    if item.get("type") == "CommandExecution":
        code = item.get("exit_code")
        exit_code = int(code) if code not in (None, "") else None
        output = item.get("aggregated_output") or ""
        return ToolCall(started, completed, "shell", _codex_command(item.get("command")), output, exit_code, is_shell=True)
    if item.get("type") == "McpToolCall":
        result = item.get("result")
        failed = item.get("status") != _MCP_OK or (isinstance(result, dict) and result.get("isError"))
        command = f"{item.get('server')}.{item.get('tool')} {json.dumps(item.get('arguments'))[:_INPUT_FALLBACK_CHARS]}"
        output = json.dumps(result)[:_MCP_OUTPUT_CHARS] if result is not None else ""
        return ToolCall(started, completed, "mcp", command, output, _GENERIC_FAILURE_CODE if failed else 0, is_shell=False)
    return None


def _codex_command(command: Any) -> str:
    if not isinstance(command, list):
        return str(command)
    if len(command) >= 3 and command[1] in _SHELL_LC:
        return str(command[-1])
    return " ".join(str(part) for part in command)


def _duration(raw: Any) -> float:
    if not isinstance(raw, dict):
        return 0.0
    return raw.get("secs", 0) + raw.get("nanos", 0) / 1e9


def _dedupe_users(turns: list[Turn]) -> tuple[Turn, ...]:
    seen: set[str] = set()
    kept: list[Turn] = []
    for turn in sorted(turns, key=lambda t: t.ts):
        if turn.role == "user" and turn.text in seen:
            continue
        if turn.role == "user":
            seen.add(turn.text)
        kept.append(turn)
    return tuple(kept)
