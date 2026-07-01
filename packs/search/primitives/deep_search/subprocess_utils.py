#!/usr/bin/env python3
"""Shared subprocess helpers for deep-search primitives.

The deep-search harness is an orchestration layer: most work happens in child
primitives. Silent child failures are therefore product failures, not merely
debug noise. Keep all subprocess execution checked and artifact-aware here so
callers cannot accidentally report success after a failed stage.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable


def _tail(text: str | bytes | None, limit: int = 4000) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


class CommandError(RuntimeError):
    """Raised when a child primitive exits non-zero or misses expected outputs."""

    def __init__(
        self,
        cmd: Iterable[object],
        *,
        returncode: int | None = None,
        stdout: str | bytes | None = None,
        stderr: str | bytes | None = None,
        missing: list[Path] | None = None,
        description: str | None = None,
    ) -> None:
        self.cmd = [str(c) for c in cmd]
        self.returncode = returncode
        self.stdout_tail = _tail(stdout)
        self.stderr_tail = _tail(stderr)
        self.missing = missing or []
        self.description = description
        parts = []
        if description:
            parts.append(f"{description} failed")
        else:
            parts.append("command failed")
        parts.append("cmd=" + " ".join(self.cmd))
        if returncode is not None:
            parts.append(f"returncode={returncode}")
        if self.missing:
            parts.append("missing=" + ", ".join(str(p) for p in self.missing))
        if self.stderr_tail:
            parts.append("stderr_tail=" + self.stderr_tail)
        elif self.stdout_tail:
            parts.append("stdout_tail=" + self.stdout_tail)
        super().__init__(" | ".join(parts))

    def to_dict(self) -> dict[str, object]:
        return {
            "command": self.cmd,
            "returncode": self.returncode,
            "missing": [str(p) for p in self.missing],
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


def require_paths(paths: Iterable[Path], *, cmd: Iterable[object], description: str | None = None) -> None:
    missing = [Path(p) for p in paths if not Path(p).exists()]
    if missing:
        raise CommandError(cmd, missing=missing, description=description)


def run_checked(
    cmd: Iterable[object],
    *,
    expected_paths: Iterable[Path] | None = None,
    description: str | None = None,
    timeout: int | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a child command, fail on non-zero exit, and verify expected artifacts."""
    cmd_list = [str(c) for c in cmd]
    try:
        cp = subprocess.run(
            cmd_list,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(
            cmd_list,
            returncode=None,
            stdout=exc.stdout,
            stderr=exc.stderr,
            description=(description or "command") + " timed out",
        ) from exc
    except OSError as exc:
        raise CommandError(cmd_list, returncode=None, stderr=str(exc), description=description) from exc

    if cp.returncode != 0:
        raise CommandError(cmd_list, returncode=cp.returncode, stdout=cp.stdout, stderr=cp.stderr, description=description)
    if expected_paths:
        require_paths([Path(p) for p in expected_paths], cmd=cmd_list, description=description)
    return cp

