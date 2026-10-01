"""Gate, deterministic report, and scrub for a parsed session.

Flow:
- `gate(session)`: reflect only when the session used Powerpacks through a skill
  signal — a user turn naming `$<skill>` (or a `/<skill>` slash command), a read
  of one of our `skills/<name>/SKILL.md` (or the Skill tool loading one), or a
  shell command running `bin/<ours>` / `packs/<pack>/primitives/…` — and made at
  least one tool call.
- `build_report(session)`: header counters, every user turn with the agent reply
  before it, tool calls (with output tails for calls in the 90 s after a user
  turn), failures with the calls around them, the agent's last reply. A call
  that ended within 1 s of a later `kill`/`pkill` is marked KILLED, not FAIL.
  Capped at 90 000 chars, then scrubbed.
- `scrub(text)`: emails (incl. `name\\@domain`), phone numbers, keys and
  tokens, `$HOME` → `~`.

Changelog:
- 2026-09-30: created from the reflection prototype, with the scored fixes
  (800-char replies, 90-s output tails, kill coincidence, `\\@` emails).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from packs.powerset.primitives.reflect.transcripts import Session, ToolCall, Turn

# Skill names from the `install_skill` lines in adapters/claude-code/install.sh.
SKILLS = frozenset({
    "search", "search-company", "search-sql", "search-contacts", "build-local-search-index",
    "powerset", "powerset-login", "powerset-set", "feedback", "update-powerpacks",
    "install-powerpacks", "import-messages", "setup", "msgvault", "import-gmail",
    "deep-context", "clean-slate", "logbook", "import-twitter", "sales-nav-search",
    "build-outbound",
})
_BIN_DIR = Path(__file__).resolve().parents[4] / "bin"

MAX_REPORT_CHARS = 90_000
TRUNCATION_MARKER = "\n\n[report truncated at input cap]"
_REPLY_CHARS = 800
_USER_CHARS = 600
_FAILURE_TAIL = 500
_FOLLOW_UP_TAIL = 300
_FOLLOW_UP_WINDOW_S = 90
_KILL_TOLERANCE_S = 1.0
_SLOW_S = 30
_MAX_LISTED_TOOLS = 150
_FAILURE_WINDOW = 2
_COMMAND_CHARS = 120
_MAX_REPEATS = 8
_MAX_SLOW = 6
_ERROR_SCAN_CHARS = 4000

_USER_SKILL = re.compile(r"(?<![\w$])\$([a-z][a-z0-9-]*)|^/([a-z][a-z0-9-]*)")
_SKILL_MD = re.compile(r"skills/([a-z0-9-]+)/SKILL\.md")
# `bin/<name>` counts only as the program of a shell segment (`bin/x`, `./bin/x`,
# `/path/bin/x`, after `;`/`&&`/`|`), never as an argument (`cat bin/x`).
_BIN_CMD = re.compile(r"(?:^|[;&|(]\s*)\S*?bin/([a-z][a-z0-9_.-]*)(?=\s|$)")
# A primitive counts only when the command EXECUTES it (python/uv run), not when
# it is read with cat/sed/rg — otherwise every dev session gates in.
_PRIMITIVE_CMD = re.compile(r"(?:python3?|uv run)\b[^|;&\n]*?\s(packs/[a-z0-9_-]+/primitives/[\w./-]+\.py)")
_SKILL_TOOL = "Skill"
_KILL_CMD = re.compile(r"\b(?:kill|pkill)\b")
_NO_MATCH_EXIT_1 = re.compile(r"^\s*(rg|grep|git grep|ugrep|diff|test|\[)\b")
_ERROR_MARKERS = re.compile(
    r"Traceback \(most recent call last\)|^(?:\S+: )?(?:error|Error|ERROR|fatal|FAILED|FAIL):|"
    r"command not found|No such file or directory|Permission denied|ECONNREFUSED|ENOTFOUND|"
    r"Connection refused|timed out|Killed: 9|out of memory|HTTP/[\d.]+ [45]\d\d\b|status=[45]\d\d\b|"
    r"\"status\": \"(?:failed|error)\"",
    re.M,
)

_HOME = str(Path.home())
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+\\?@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(
    r"(?<![\w.:/-])(?:"
    r"(?:\+\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}"
    r"|\+\d[\d .-]{5,16}\d"
    r")(?![\w.-])"
)
_KEY = re.compile(
    r"\b(?:sk|rk|pk|xox[a-z]|ghp|gho|AKIA)[-_A-Za-z0-9]{12,}\b"
    r"|\b[A-Fa-f0-9]{40,}\b|\b[A-Za-z0-9_]{40,}\b|[A-Za-z0-9+/]{32,}={1,2}"
)
_BEARER = re.compile(r"(Authorization:\s*(?:Bearer|Basic|Token)?|Bearer|token=|api[_-]?key=)\s*[^\s\"']+", re.I)


@dataclass(frozen=True)
class Gate:
    passed: bool
    reason: str
    signals: dict[str, list[str]]
    tool_calls: int

    def to_dict(self) -> dict:
        return {"passed": self.passed, "reason": self.reason, "signals": self.signals, "tool_calls": self.tool_calls}


def scrub(text: str) -> str:
    text = text.replace(_HOME, "~")
    text = _BEARER.sub(r"\1 <redacted>", text)
    text = _KEY.sub("<key>", text)
    text = _EMAIL.sub("<email>", text)
    return _PHONE.sub("<phone>", text)


def gate(session: Session) -> Gate:
    signals = {
        "user_named_skill": sorted({name for turn in session.user_turns for name in _user_skills(turn.text)}),
        "skill_read": sorted({name for call in session.tools for name in _skills_read(call)}),
        "ran_powerpacks_command": sorted({cmd for call in session.tools if call.is_shell for cmd in _our_commands(call.command)}),
    }
    signals = {kind: found for kind, found in signals.items() if found}
    tool_calls = len(session.tools)
    if not signals:
        return Gate(False, "no skill signal", signals, tool_calls)
    if not tool_calls:
        return Gate(False, "no tool calls", signals, tool_calls)
    return Gate(True, "skill signal: " + ", ".join(signals), signals, tool_calls)


def _user_skills(text: str) -> set[str]:
    return {name for match in _USER_SKILL.finditer(text) for name in match.groups() if name in SKILLS}


def _skills_read(call: ToolCall) -> set[str]:
    if call.name == _SKILL_TOOL and call.command in SKILLS:
        return {call.command}
    return {name for name in _SKILL_MD.findall(call.command) if name in SKILLS}


def _our_commands(command: str) -> set[str]:
    ours = {match.group(1) for match in _PRIMITIVE_CMD.finditer(command)}
    ours |= {f"bin/{name}" for name in _BIN_CMD.findall(command) if (_BIN_DIR / name).is_file()}
    return ours


def build_report(session: Session) -> str:
    kills = [call for call in session.tools if call.is_shell and _KILL_CMD.search(call.command)]
    killers = {id(call): killer for call in session.tools if (killer := _killed_by(call, kills))}
    lines = _header(session, killers)
    lines += _user_turn_lines(session)
    lines += _tool_lines(session, killers)
    lines += _failure_lines(session, killers)
    last = _last_reply(session.turns, float("inf"))
    lines += ["\n## Last thing the agent said", f"[{_clock(last.ts)}] {_one_line(last.text)[:_REPLY_CHARS]}" if last else "(none)"]
    text = "\n".join(lines)
    if len(text) > MAX_REPORT_CHARS:
        text = text[:MAX_REPORT_CHARS] + TRUNCATION_MARKER
    return scrub(text)


def _header(session: Session, killers: dict[int, ToolCall]) -> list[str]:
    users = session.user_turns
    stamps = [t.ts for t in session.turns] + [c.started for c in session.tools]
    start, end = (min(stamps), max(stamps)) if stamps else (0.0, 0.0)
    fails = [c for c in session.tools if _is_failure(c, killers)]
    lines = [
        f"# Session report ({session.harness})",
        f"- transcript: {session.path}",
        f"- cwd: {session.cwd}",
        f"- models: {', '.join(session.models) or 'unknown'}",
        f"- wall time: {(end - start) / 60:.0f} min ({_clock(start)}–{_clock(end)} UTC)",
        f"- user turns: {len(users)} · tool calls: {len(session.tools)} · failed/errored tool calls: {len(fails)}"
        f" · ended by a later kill: {len(killers)}",
    ]
    gaps = [(b.ts - a.ts, a, b) for a, b in zip(users, users[1:])]
    if gaps:
        gap, before, after = max(gaps, key=lambda g: g[0])
        lines.append(f"- longest silence between user turns: {gap / 60:.0f} min ({_clock(before.ts)} → {_clock(after.ts)})")
    gate_result = gate(session)
    lines.append(f"- skill signals: {_signal_text(gate_result.signals)}")
    repeats = _repeats(session.tools)
    if repeats:
        lines.append("- repeated commands: " + "; ".join(f"{n}× `{cmd}`" for n, cmd in repeats))
    slow = _slow(session.tools)
    if slow:
        lines.append(f"- slow tool calls (>{_SLOW_S}s): " + "; ".join(f"{_duration(c):.0f}s `{_one_line(c.command)[:60]}`" for c in slow))
    return lines


def _signal_text(signals: dict[str, list[str]]) -> str:
    return " · ".join(f"{kind}: {', '.join(found)}" for kind, found in signals.items()) or "none"


def _user_turn_lines(session: Session) -> list[str]:
    lines = ["\n## Every user turn (verbatim, in order)"]
    for turn in session.user_turns:
        reply = _last_reply(session.turns, turn.ts)
        if reply:
            lines.append(f"  (agent said, {_clock(reply.ts)}): {_one_line(reply.text)[:_REPLY_CHARS]}")
        lines.append(f"- [{_clock(turn.ts)}] USER: {turn.text.strip()[:_USER_CHARS]}")
    return lines


def _tool_lines(session: Session, killers: dict[int, ToolCall]) -> list[str]:
    lines = ["\n## Tool calls"]
    users = session.user_turns
    follow_ups = {id(c) for c in session.tools if _follows_user_turn(c, users)}
    chosen = list(session.tools)
    if len(chosen) > _MAX_LISTED_TOOLS:
        keep = follow_ups | {id(c) for c in session.tools if _is_failure(c, killers)} | {id(c) for c in _slow(session.tools)}
        chosen = [c for c in session.tools if id(c) in keep][:_MAX_LISTED_TOOLS]
        lines.append(f"(showing {len(chosen)} of {len(session.tools)}: failures, slow calls, and the calls in the {_FOLLOW_UP_WINDOW_S}s after each user turn)")
    for call in chosen:
        lines.append(_call_line(call, killers))
        if id(call) in follow_ups and call.output.strip():
            lines.append("  output: " + _tail(call.output, _FOLLOW_UP_TAIL).replace("\n", "\n  "))
    return lines


def _failure_lines(session: Session, killers: dict[int, ToolCall]) -> list[str]:
    lines = ["\n## Failures and the tool calls around them"]
    tools = session.tools
    failed = [i for i, c in enumerate(tools) if _is_failure(c, killers)]
    if not failed:
        lines.append("(no failed or errored tool calls)")
    shown: set[int] = set()
    for i in failed:
        block = [j for j in range(max(0, i - _FAILURE_WINDOW), min(len(tools), i + _FAILURE_WINDOW + 1)) if j not in shown]
        if not block:
            continue
        lines.append(f"\n### failure at {_clock(tools[i].started)}")
        for j in block:
            shown.add(j)
            lines.append(_call_line(tools[j], killers))
            if j == i or _is_failure(tools[j], killers):
                lines.append("  output: " + _tail(tools[j].output, _FAILURE_TAIL).replace("\n", "\n  "))
    return lines


def _call_line(call: ToolCall, killers: dict[int, ToolCall]) -> str:
    killer = killers.get(id(call))
    mark = "FAIL" if _is_failure(call, killers) else "ok"
    if killer:
        mark = f"KILLED (ended by a later kill at {_clock(killer.started)})"
    code = f" exit={call.exit_code}" if call.exit_code not in (None, 0) else ""
    return f"- [{_clock(call.started)}] {mark}{code} {_duration(call):.0f}s {call.name} `{_one_line(call.command)[:_COMMAND_CHARS]}`"


def _killed_by(call: ToolCall, kills: list[ToolCall]) -> ToolCall | None:
    for later in kills:
        if later.started <= call.started:
            continue
        if min(abs(later.started - call.ended), abs(later.ended - call.ended)) <= _KILL_TOLERANCE_S:
            return later
    return None


def _is_failure(call: ToolCall, killers: dict[int, ToolCall]) -> bool:
    if id(call) in killers:
        return False
    if call.exit_code == 1 and _NO_MATCH_EXIT_1.match(call.command):
        return False
    if call.exit_code is not None:
        return call.exit_code != 0
    return bool(_ERROR_MARKERS.search(call.output[-_ERROR_SCAN_CHARS:]))


def _follows_user_turn(call: ToolCall, users: tuple[Turn, ...]) -> bool:
    return any(0 <= call.started - turn.ts <= _FOLLOW_UP_WINDOW_S for turn in users)


def _last_reply(turns: tuple[Turn, ...], before: float) -> Turn | None:
    return next((t for t in reversed(turns) if t.role == "assistant" and t.ts < before), None)


def _repeats(tools: tuple[ToolCall, ...]) -> list[tuple[int, str]]:
    counts: dict[str, int] = {}
    for call in tools:
        key = _one_line(call.command)[:_COMMAND_CHARS]
        counts[key] = counts.get(key, 0) + 1
    return sorted(((n, cmd) for cmd, n in counts.items() if n > 1), reverse=True)[:_MAX_REPEATS]


def _slow(tools: tuple[ToolCall, ...]) -> list[ToolCall]:
    return sorted((c for c in tools if _duration(c) > _SLOW_S), key=lambda c: -_duration(c))[:_MAX_SLOW]


def _duration(call: ToolCall) -> float:
    return max(0.0, call.ended - call.started)


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _tail(text: str, chars: int) -> str:
    text = text.strip()
    return text if len(text) <= chars else "…" + text[-chars:]


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S")
