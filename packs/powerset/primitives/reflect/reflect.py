#!/usr/bin/env python3
"""Post-session reflection for Claude Code and Codex sessions that used Powerpacks.

Subcommands:
- `report <transcript> [--out DIR]`: print the scrubbed deterministic report
  (and write `DIR/report.md` when `--out` is given).
- `hook end`: SessionEnd hook. Reads `{session_id, transcript_path, cwd,
  reason, hook_event_name}` on stdin, always prints `{}` and exits 0; reads no
  transcript (Codex gives the hook 3 s, transcripts reach 50+ MB). Does nothing
  when `POWERPACKS_REFLECT=off` (also the recursion guard for the narrative
  run). Otherwise writes `<root>/<harness>-<session_id>/meta.json` (harness
  from the path shape: `/.codex/` → codex) and spawns `process` detached.
  Idempotent per session: an existing `meta.json` means already handled.
- `process <session_dir>`: parse the transcript, gate; gated out → remove the
  session dir; gated in → `report.md`, then `narrate`.
- `narrate <session_dir>`: headless model run → `narrative.md`, updates
  `meta.json`. Exit 0 on done/skip, 1 on failure.
- `latest`: print the newest session dir. Exit 1 if none.

Output root: `<repo>/.powerpacks/reflect/`, overridden by `POWERPACKS_REFLECT_ROOT`.
`POWERPACKS_REFLECT_NARRATOR=<executable>` replaces the detached `process`
child (called as `<executable> <session_dir>`).

Changelog:
- 2026-09-30: created; transcript parsing moved out of the hook into `process`.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.powerset.primitives.reflect.narrate import (  # noqa: E402
    META_FILE,
    REFLECT_ENV,
    REFLECT_OFF,
    REPORT_FILE,
    Exit,
    narrate,
)
from packs.powerset.primitives.reflect.report import build_report, gate  # noqa: E402
from packs.powerset.primitives.reflect.transcripts import CLAUDE_CODE, CODEX, Harness, parse  # noqa: E402

ROOT_ENV = "POWERPACKS_REFLECT_ROOT"
NARRATOR_ENV = "POWERPACKS_REFLECT_NARRATOR"
DEFAULT_ROOT = _REPO_ROOT / ".powerpacks" / "reflect"
CODEX_PATH_MARK = "/.codex/"
HOOK_LOG = "hook.log"
PROCESS_OUT = "process.out"
PROCESS_ERR = "process.err"
HOOK_REPLY = "{}"
PENDING = "pending"


@dataclass(frozen=True)
class HookPayload:
    session_id: str
    transcript_path: Path
    cwd: str
    reason: str

    @classmethod
    def parse(cls, raw: str) -> HookPayload:
        data = json.loads(raw)
        return cls(
            session_id=str(data["session_id"]),
            transcript_path=Path(data["transcript_path"]).expanduser(),
            cwd=str(data.get("cwd") or ""),
            reason=str(data.get("reason") or ""),
        )

    @property
    def harness(self) -> Harness:
        return CODEX if CODEX_PATH_MARK in str(self.transcript_path) else CLAUDE_CODE


def out_root() -> Path:
    return Path(os.environ.get(ROOT_ENV) or DEFAULT_ROOT).expanduser()


def hook_end(raw: str) -> None:
    if os.environ.get(REFLECT_ENV) == REFLECT_OFF:
        return
    payload = HookPayload.parse(raw)
    session_dir = out_root() / f"{payload.harness}-{payload.session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)
    try:
        (session_dir / META_FILE).open("x").close()
    except FileExistsError:
        return
    meta = {
        "harness": payload.harness,
        "session_id": payload.session_id,
        "transcript_path": str(payload.transcript_path),
        "cwd": payload.cwd,
        "reason": payload.reason,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "narrative": PENDING,
    }
    _write_meta(session_dir, meta)
    _spawn_process(session_dir)


def process(session_dir: Path) -> Exit:
    meta = json.loads((session_dir / META_FILE).read_text())
    try:
        session = parse(Path(meta["transcript_path"]))
    except Exception as e:
        meta["narrative"] = f"failed: {type(e).__name__}: {e}"
        _write_meta(session_dir, meta)
        return Exit.FAILED
    result = gate(session)
    if not result.passed:
        shutil.rmtree(session_dir)
        return Exit.OK
    (session_dir / REPORT_FILE).write_text(build_report(session))
    meta["harness"] = session.harness
    meta["gate"] = result.to_dict()
    _write_meta(session_dir, meta)
    return narrate(session_dir)


def _write_meta(session_dir: Path, meta: dict) -> None:
    (session_dir / META_FILE).write_text(json.dumps(meta, indent=2))


def _spawn_process(session_dir: Path) -> None:
    narrator = os.environ.get(NARRATOR_ENV)
    own = [sys.executable, str(Path(__file__).resolve()), "process", str(session_dir)]
    argv = [narrator, str(session_dir)] if narrator else own
    env = dict(os.environ)
    env[REFLECT_ENV] = REFLECT_OFF
    with (session_dir / PROCESS_OUT).open("w") as out, (session_dir / PROCESS_ERR).open("w") as err:
        subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env)


def _log_hook_failure(raw: str, error: Exception) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} reflect hook: {type(error).__name__}: {error}\n"
    sys.stderr.write(line)
    try:
        session_id = str(json.loads(raw)["session_id"])
    except Exception:
        return
    for session_dir in out_root().glob(f"*-{session_id}"):
        with (session_dir / HOOK_LOG).open("a") as fh:
            fh.write(line)


def latest() -> Path | None:
    root = out_root()
    dirs = [d for d in root.iterdir() if d.is_dir()] if root.is_dir() else []
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Post-session reflection.")
    sub = parser.add_subparsers(dest="command", required=True)
    report_cmd = sub.add_parser("report")
    report_cmd.add_argument("transcript", type=Path)
    report_cmd.add_argument("--out", type=Path)
    hook_cmd = sub.add_parser("hook")
    hook_cmd.add_argument("event", choices=["end"])
    sub.add_parser("process").add_argument("session_dir", type=Path)
    sub.add_parser("narrate").add_argument("session_dir", type=Path)
    sub.add_parser("latest")
    args = parser.parse_args()

    if args.command == "report":
        text = build_report(parse(args.transcript.expanduser()))
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / REPORT_FILE).write_text(text)
        print(text)
        return Exit.OK

    if args.command == "hook":
        raw = ""
        try:
            raw = sys.stdin.read()
            hook_end(raw)
        except Exception as e:
            _log_hook_failure(raw, e)
        print(HOOK_REPLY)
        return Exit.OK

    if args.command == "process":
        return process(args.session_dir)

    if args.command == "narrate":
        return narrate(args.session_dir)

    newest = latest()
    if newest is None:
        return Exit.FAILED
    print(newest)
    return Exit.OK


if __name__ == "__main__":
    sys.exit(main())
