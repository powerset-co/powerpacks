#!/usr/bin/env python3
"""Register or remove the Powerpacks reflect SessionEnd hook in a harness config.

    hooks_install.py --harness claude-code|codex --config-dir <dir> --command "<cmd>" [--remove]

Flow:
    1. Read <dir>/settings.json (claude-code) or <dir>/hooks.json (codex);
       a missing file is an empty config, invalid JSON exits 1 untouched.
    2. A handler is ours when its command contains `/bin/reflect `.
       Register replaces ours in place, or appends one group LAST; existing
       groups are never reordered. Remove drops ours plus the
       group/list/object it leaves empty.
    3. codex only: Codex runs a user hook only when <dir>/config.toml has
       `[hooks.state."<hooks.json path>:session_end:<group>:<handler>"]` with
       a `trusted_hash` matching the hook. Register writes that table under
       our handler's actual position and drops a stale record of ours under
       another position (ours = its hash is our old or new command's hash).
       Remove deletes only the record at the position ours sat. Other lines
       are untouched; config.toml is parsed before and after the edit, and
       an unparsable result exits 1 without writing.
    4. Write atomically (temp file + os.replace); every other key is kept.

Shape (codex uses matcher null and timeout 3, its SessionEnd clamp):
    {"hooks": {"SessionEnd": [{"matcher": "", "hooks": [
        {"type": "command", "command": "<cmd>", "timeout": 30}]}]}}

Stdlib-only.

Changelog:
    2026-09-30: created; codex trust record in config.toml, keyed by position.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import tomllib
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
EXIT_INVALID_CONFIG = 1

Position = tuple[int, int]


def _is_ours(handler: dict[str, Any]) -> bool:
    return OWNERSHIP_MARKER in str(handler.get("command", ""))


def _handler(harness: str, command: str) -> dict[str, Any]:
    return {"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS[harness]}


def _our_position(config: dict[str, Any]) -> Position | None:
    groups = config.get("hooks", {}).get(HOOK_EVENT, [])
    for group_index, group in enumerate(groups):
        for handler_index, handler in enumerate(group.get("hooks", [])):
            if _is_ours(handler):
                return group_index, handler_index
    return None


def _command_at(config: dict[str, Any], position: Position) -> str:
    group_index, handler_index = position
    return config["hooks"][HOOK_EVENT][group_index]["hooks"][handler_index]["command"]


def _register(config: dict[str, Any], harness: str, command: str) -> Position:
    """Replace ours in place, or append one group last; return its position."""
    groups = config.setdefault("hooks", {}).setdefault(HOOK_EVENT, [])
    wanted = _handler(harness, command)
    position = _our_position(config)
    if position is None:
        groups.append({"matcher": GROUP_MATCHER[harness], "hooks": [wanted]})
        return len(groups) - 1, 0
    group_index, handler_index = position
    groups[group_index]["hooks"][handler_index] = wanted
    return position


def _remove(config: dict[str, Any]) -> None:
    """Drop ours and any group, event list, or hooks object it empties."""
    hooks = config.get("hooks", {})
    kept_groups = []
    for group in hooks.get(HOOK_EVENT, []):
        handlers = group.get("hooks", [])
        kept = [handler for handler in handlers if not _is_ours(handler)]
        if len(kept) == len(handlers):
            kept_groups.append(group)
        elif kept:
            kept_groups.append({**group, "hooks": kept})
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


def _trust_key(hooks_path: Path, position: Position) -> str:
    group_index, handler_index = position
    return f"{os.path.abspath(hooks_path)}:{CODEX_EVENT_LABEL}:{group_index}:{handler_index}"


def _header(key: str) -> str:
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


def _drop_table(text: str, key: str) -> str:
    lines, _ = _without_table(text.splitlines(keepends=True), _header(key))
    return "".join(lines)


def _set_table(text: str, key: str, trusted_hash: str) -> str:
    header = _header(key)
    lines, at = _without_table(text.splitlines(keepends=True), header)
    table = ["\n", f"{header}\n", f"trusted_hash = {json.dumps(trusted_hash)}\n", "enabled = true\n"]
    if at is None:
        at = len(lines)
    if at == len(lines) and lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    if at == 0:
        table = table[1:]
    return "".join(lines[:at] + table + lines[at:])


def _stale_keys(trust: dict[str, Any], hooks_path: Path, keep: str, our_hashes: set[str]) -> list[str]:
    """Our records for this hooks.json under any key other than `keep`."""
    prefix = f"{os.path.abspath(hooks_path)}:{CODEX_EVENT_LABEL}:"
    state = trust.get("hooks", {}).get("state", {})
    return [
        key for key, record in state.items()
        if key != keep and key.startswith(prefix) and record.get("trusted_hash") in our_hashes
    ]


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


def _refuse(message: str) -> int:
    print(f"reflect hook: {message}; left untouched", file=sys.stderr)
    return EXIT_INVALID_CONFIG


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--harness", required=True, choices=sorted(CONFIG_FILENAMES))
    parser.add_argument("--config-dir", required=True, type=Path)
    parser.add_argument("--command", required=True)
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args()

    config_dir = args.config_dir.expanduser()
    path = config_dir / CONFIG_FILENAMES[args.harness]
    trust_path = config_dir / CODEX_TRUST_FILENAME
    try:
        config: dict[str, Any] = json.loads(_read_text(path) or "{}")
    except json.JSONDecodeError as e:
        return _refuse(f"{path} is not valid JSON ({e})")

    old_trust = _read_text(trust_path) if args.harness == CODEX else ""
    try:
        trust = tomllib.loads(old_trust)
    except tomllib.TOMLDecodeError as e:
        return _refuse(f"{trust_path} is not valid TOML ({e})")

    original = json.loads(json.dumps(config))
    old_position = _our_position(config)
    old_command = _command_at(config, old_position) if old_position else None

    new_trust = old_trust
    if args.remove:
        _remove(config)
        if args.harness == CODEX and old_position:
            new_trust = _drop_table(old_trust, _trust_key(path, old_position))
    else:
        position = _register(config, args.harness, args.command)
        if args.harness == CODEX:
            key = _trust_key(path, position)
            our_hashes = {codex_trusted_hash(command) for command in (args.command, old_command) if command}
            for stale in _stale_keys(trust, path, key, our_hashes):
                new_trust = _drop_table(new_trust, stale)
            new_trust = _set_table(new_trust, key, codex_trusted_hash(args.command))

    try:
        tomllib.loads(new_trust)
    except tomllib.TOMLDecodeError as e:
        return _refuse(f"editing {trust_path} would produce invalid TOML ({e})")

    changed = config != original or new_trust != old_trust
    if config != original:
        _write_atomic(path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    if new_trust != old_trust:
        _write_atomic(trust_path, new_trust)

    if args.remove:
        verb = "removed" if changed else "not registered"
    else:
        verb = "registered" if changed else "already registered"
    print(f"{verb} {args.harness} {HOOK_EVENT} hook in {path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
