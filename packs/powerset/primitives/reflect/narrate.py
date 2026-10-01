"""Headless narrative run over a session's report.md, on the harness the session used.

Flow: read `<session_dir>/meta.json` (harness) and `report.md`, cap the report
at 90 000 chars, run `claude -p` or `codex exec` with `prompt.md`, scrub the
result into `narrative.md`, record the outcome in `meta.json`.
- Claude Code: `claude -p --output-format json --tools ""`; report on stdin,
  prompt as the system prompt, `CLAUDECODE` dropped from env. Default `opus`.
- Codex: `codex exec … -C ~/.powerpacks -`; prompt + report on stdin. Default
  `gpt-6-sol`.
- Both run from `~/.powerpacks`, outside any git checkout, so neither CLI
  loads a project's AGENTS.md / CLAUDE.md into the narrative run.
- `POWERPACKS_REFLECT_MODEL` overrides the model; the child env carries
  `POWERPACKS_REFLECT=off` so its own SessionEnd hook does nothing.

Changelog:
- 2026-09-30: created from the reflection prototype.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from packs.powerset.primitives.reflect.report import MAX_REPORT_CHARS, TRUNCATION_MARKER, scrub
from packs.powerset.primitives.reflect.transcripts import CODEX

PROMPT_PATH = Path(__file__).resolve().parent / "prompt.md"
MODEL_ENV = "POWERPACKS_REFLECT_MODEL"
REFLECT_ENV = "POWERPACKS_REFLECT"
REFLECT_OFF = "off"
NESTED_SESSION_ENV = "CLAUDECODE"
TIMEOUT_S = 600
CLAUDE_MODEL = "opus"
CODEX_MODEL = "gpt-6-sol"
CODEX_EFFORT = "medium"
HEADLESS_WORKDIR = Path.home() / ".powerpacks"
REPORT_FILE = "report.md"
META_FILE = "meta.json"
NARRATIVE_FILE = "narrative.md"
CODEX_RAW_FILE = "narrative.raw.md"
_STDERR_TAIL = 500
_TOKENS_USED = re.compile(r"tokens used[:\s]*([\d,]+)", re.I)


class Exit(IntEnum):
    OK = 0
    FAILED = 1


@dataclass(frozen=True)
class RunResult:
    text: str
    details: dict


class NarrateError(Exception):
    pass


def narrate(session_dir: Path) -> Exit:
    meta_path = session_dir / META_FILE
    meta = json.loads(meta_path.read_text())
    report = (session_dir / REPORT_FILE).read_text()
    if len(report) > MAX_REPORT_CHARS:
        report = report[:MAX_REPORT_CHARS] + TRUNCATION_MARKER
    is_codex = meta.get("harness") == CODEX
    cli = "codex" if is_codex else "claude"
    model = os.environ.get(MODEL_ENV) or (CODEX_MODEL if is_codex else CLAUDE_MODEL)
    meta["model"] = model
    if not shutil.which(cli):
        meta["narrative"] = f"skipped: no {cli} on PATH"
        meta_path.write_text(json.dumps(meta, indent=2))
        return Exit.OK
    prompt = PROMPT_PATH.read_text()
    HEADLESS_WORKDIR.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        result = _run_codex(model, prompt, report, session_dir) if is_codex else _run_claude(model, prompt, report)
    except subprocess.TimeoutExpired:
        return _fail(meta_path, meta, "timeout", started)
    except NarrateError as e:
        return _fail(meta_path, meta, str(e), started)
    (session_dir / NARRATIVE_FILE).write_text(scrub(result.text))
    meta.update(result.details)
    meta["seconds"] = round(time.monotonic() - started, 1)
    meta["narrative"] = "done"
    meta_path.write_text(json.dumps(meta, indent=2))
    return Exit.OK


def _fail(meta_path: Path, meta: dict, why: str, started: float) -> Exit:
    meta["seconds"] = round(time.monotonic() - started, 1)
    meta["narrative"] = f"failed: {why}"
    meta_path.write_text(json.dumps(meta, indent=2))
    return Exit.FAILED


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != NESTED_SESSION_ENV}
    env[REFLECT_ENV] = REFLECT_OFF
    return env


def _run_claude(model: str, prompt: str, report: str) -> RunResult:
    cmd = [
        "claude", "-p", "--model", model, "--output-format", "json", "--tools", "",
        "--no-session-persistence", "--setting-sources", "", "--system-prompt", prompt,
    ]
    proc = subprocess.run(
        cmd, input=report, capture_output=True, text=True, env=_child_env(), cwd=HEADLESS_WORKDIR, timeout=TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise NarrateError(f"claude exit {proc.returncode}: {proc.stderr[-_STDERR_TAIL:].strip()}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise NarrateError(f"claude output not JSON: {e}") from e
    if data.get("is_error") or not data.get("result"):
        raise NarrateError(f"claude returned no result: {str(data.get('result'))[:_STDERR_TAIL]}")
    return RunResult(data["result"], {"total_cost_usd": data.get("total_cost_usd"), "usage": data.get("usage")})


def _run_codex(model: str, prompt: str, report: str, session_dir: Path) -> RunResult:
    raw = session_dir / CODEX_RAW_FILE
    cmd = [
        "codex", "exec", "-m", model, "-c", f'model_reasoning_effort="{CODEX_EFFORT}"', "-c", "mcp_servers={}",
        "-s", "read-only", "--skip-git-repo-check", "--ephemeral", "-C", str(HEADLESS_WORKDIR), "-o", str(raw), "-",
    ]
    proc = subprocess.run(
        cmd, input=prompt + "\n\n" + report, capture_output=True, text=True, env=_child_env(),
        cwd=HEADLESS_WORKDIR, timeout=TIMEOUT_S,
    )
    if proc.returncode != 0 or not raw.exists():
        raise NarrateError(f"codex exit {proc.returncode}: {proc.stderr[-_STDERR_TAIL:].strip()}")
    tokens = _TOKENS_USED.search(proc.stdout + proc.stderr)
    text = raw.read_text()
    raw.unlink()  # only the scrubbed narrative.md stays on disk
    return RunResult(text, {"tokens_used": tokens.group(1) if tokens else None})
