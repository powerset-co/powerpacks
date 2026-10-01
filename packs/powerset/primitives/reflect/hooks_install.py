#!/usr/bin/env python3
"""Register or remove the Powerpacks reflect SessionEnd hook in a harness config.

    hooks_install.py --harness claude-code|codex --config-dir <dir> --command "<cmd>" [--remove]

Flow:
    1. Read <dir>/settings.json (claude-code) or <dir>/hooks.json (codex);
       a missing file is an empty config, invalid JSON exits 1 untouched.
    2. A handler is ours when its command contains `/bin/reflect `.
       claude-code: register replaces ours in place, or appends one group.
       codex: register moves ours to SessionEnd group 0, handler 0, ahead of
       any foreign groups, because the trust key below names that position.
       Remove drops ours plus the group/list/object it leaves empty.
    3. codex only: Codex runs a user hook only when <dir>/config.toml has
       `[hooks.state."<hooks.json path>:session_end:0:0"]` with a
       `trusted_hash` matching the hook. Register writes that table, remove
       deletes it; no other config.toml line is touched.
    4. Write atomically (temp file + os.replace); every other key is kept.

Shape (codex uses matcher null and timeout 3, its SessionEnd clamp):
    {"hooks": {"SessionEnd": [{"matcher": "", "hooks": [
        {"type": "command", "command": "<cmd>", "timeout": 30}]}]}}

Stdlib-only.

Changelog:
    2026-09-30: created; codex trust record in config.toml.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


CLAUDE_CODE = "claude-code"
CODEX = "codex"
CONFIG_FILENAMES = {CLAUDE_CODE: "settings.json", CODEX: "hooks.json"}
CODEX_TRUST_FILENAME = "config.toml"
HOOK_EVENT = "SessionEnd"
CODEX_EVENT_LABEL = "session_end"
HOOK_TIMEOUT_SECONDS = {CLAUDE_CODE: 30, CODEX: 3}
GROUP_MATCHER = {CLAUDE_CODE: "", CODEX: None}
OWNERSHIP_MARKER = "/bin/reflect "
EXIT_OK = 0
EXIT_INVALID_JSON = 1


def _is_ours(handler: dict[str, Any]) -> bool:
    return OWNERSHIP_MARKER in str(handler.get("command", ""))


def _handler(harness: str, command: str) -> dict[str, Any]:
    return {"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS[harness]}


def _strip_ours(groups: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Groups without our handlers; a group we emptied is dropped."""
    kept_groups = []
    removed = False
    for group in groups:
        handlers = group.get("hooks", [])
        kept = [handler for handler in handlers if not _is_ours(handler)]
        if len(kept) == len(handlers):
            kept_groups.append(group)
            continue
        removed = True
        if kept:
            kept_groups.append({**group, "hooks": kept})
    return kept_groups, removed


def _register_in_place(groups: list[dict[str, Any]], wanted: dict[str, Any]) -> None:
    for group in groups:
        handlers = group.get("hooks", [])
        for index, handler in enumerate(handlers):
            if _is_ours(handler):
                handlers[index] = wanted
                return
    groups.append({"matcher": GROUP_MATCHER[CLAUDE_CODE], "hooks": [wanted]})


def _register(config: dict[str, Any], harness: str, command: str) -> None:
    wanted = _handler(harness, command)
    hooks = config.setdefault("hooks", {})
    groups = hooks.setdefault(HOOK_EVENT, [])
    if harness == CLAUDE_CODE:
        _register_in_place(groups, wanted)
        return
    foreign, _ = _strip_ours(groups)
    hooks[HOOK_EVENT] = [{"matcher": GROUP_MATCHER[CODEX], "hooks": [wanted]}, *foreign]


def _remove(config: dict[str, Any]) -> None:
    hooks = config.get("hooks", {})
    kept_groups, _ = _strip_ours(hooks.get(HOOK_EVENT, []))
    hooks[HOOK_EVENT] = kept_groups
    if not kept_groups:
        del hooks[HOOK_EVENT]
    if not hooks:
        config.pop("hooks", None)


def _sort_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sort_keys(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sort_keys(item) for item in value]
    return value


def codex_trusted_hash(command: str) -> str:
    """Codex's hook fingerprint for our SessionEnd group (matcher null is omitted)."""
    identity = {
        "event_name": CODEX_EVENT_LABEL,
        "hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS[CODEX], "async": False}],
    }
    serialized = json.dumps(_sort_keys(identity), separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _trust_header(hooks_path: Path) -> str:
    key = f"{os.path.abspath(hooks_path)}:{CODEX_EVENT_LABEL}:0:0"
    return f"[hooks.state.{json.dumps(key, ensure_ascii=False)}]"


def _without_table(lines: list[str], header: str) -> tuple[list[str], int | None]:
    """Lines minus the table under `header` (and the blank line before it); its old index."""
    stripped = [line.strip() for line in lines]
    if header not in stripped:
        return lines, None
    start = stripped.index(header)
    end = start + 1
    while end < len(lines) and not stripped[end].startswith("["):
        end += 1
    while end > start + 1 and not stripped[end - 1]:
        end -= 1
    if start > 0 and not stripped[start - 1]:
        start -= 1
    return lines[:start] + lines[end:], start


def _trust_text(text: str, header: str, trusted_hash: str | None) -> str:
    """config.toml text with our table removed, or set to `trusted_hash`."""
    lines, at = _without_table(text.splitlines(keepends=True), header)
    if trusted_hash is None:
        return "".join(lines)
    table = ["\n", f"{header}\n", f"trusted_hash = {json.dumps(trusted_hash)}\n", "enabled = true\n"]
    if at is None:
        at = len(lines)
    if at == len(lines) and lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    if at == 0:
        table = table[1:]
    return "".join(lines[:at] + table + lines[at:])


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--harness", required=True, choices=sorted(CONFIG_FILENAMES))
    parser.add_argument("--config-dir", required=True, type=Path)
    parser.add_argument("--command", required=True)
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args()

    config_dir = args.config_dir.expanduser()
    path = config_dir / CONFIG_FILENAMES[args.harness]
    old_text = _read_text(path)
    try:
        config: dict[str, Any] = json.loads(old_text) if old_text else {}
    except json.JSONDecodeError as e:
        print(f"reflect hook: {path} is not valid JSON ({e}); left untouched", file=sys.stderr)
        return EXIT_INVALID_JSON

    original = json.loads(json.dumps(config))
    if args.remove:
        _remove(config)
    else:
        _register(config, args.harness, args.command)
    changed = config != original
    if changed:
        _write_atomic(path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")

    if args.harness == CODEX:
        trust_path = config_dir / CODEX_TRUST_FILENAME
        old_trust = _read_text(trust_path)
        trusted_hash = None if args.remove else codex_trusted_hash(args.command)
        new_trust = _trust_text(old_trust, _trust_header(path), trusted_hash)
        if new_trust != old_trust:
            _write_atomic(trust_path, new_trust)
            changed = True

    if args.remove:
        verb = "removed" if changed else "not registered"
    else:
        verb = "registered" if changed else "already registered"
    print(f"{verb} {args.harness} {HOOK_EVENT} hook in {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
