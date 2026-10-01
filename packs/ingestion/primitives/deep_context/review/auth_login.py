"""The Powerset sign-in, opened in a browser on this machine.

`start_auth_login` runs `packs/powerset/primitives/auth/auth.py login` as a child process
and remembers it, so a second click while the browser flow is open starts nothing.

Changelog:
  2026-10-01: split out of api.py (it came there from server.py with POST /auth/login).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

AUTH_SCRIPT = Path(__file__).resolve().parents[5] / "packs/powerset/primitives/auth/auth.py"
_auth_proc: subprocess.Popen[bytes] | None = None


def start_auth_login() -> str:
    """Open the Powerset sign-in in a browser on this machine, once at a time."""
    global _auth_proc
    if _auth_proc is not None and _auth_proc.poll() is None:
        return "already_running"

    _auth_proc = subprocess.Popen(
        [sys.executable, str(AUTH_SCRIPT), "login"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return "login_started"
