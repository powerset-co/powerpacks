"""MessageChannel base: the per-source extract -> normalize contract plus the
channel return-shape payload builders (``blocked_child`` / ``failed_child``).

A channel is one message source (iMessage or WhatsApp). The concrete subclasses
(``IMessageChannel`` / ``WhatsAppChannel``, in sibling modules) set their three
fixed output paths (``contacts_csv``/``normalized_jsonl``/``normalized_manifest``)
in their own ``__init__`` and own their extract -> normalize subprocess chain.
``extract()``/``normalize()``/``run()`` return ``None`` on
success or a blocked/failed child payload that short-circuits the discovery run.
``blocked_child`` and ``failed_child`` are the shared return shapes both channels
(and the store's merge) emit; they live here as the base's return-shape helpers.

Changelog:
  2026-07-23 (terse): dropped the ``@property``/``NotImplementedError`` path
    accessors (contacts_csv/normalized_jsonl/normalized_manifest) that existed to
    read module constants at call time for test patching; subclasses now set the
    three fixed paths as plain attributes in their own ``__init__``.
  2026-07-23 (channels split): extracted from messages/discover.py into the
    channels/ subpackage — the ``MessageChannel`` base and the
    ``blocked_child``/``failed_child`` builders moved here;
    ``IMessageChannel``/``WhatsAppChannel`` and their owned path constants moved
    to sibling channel modules. Behavior unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import write_json  # noqa: E402
from packs.ingestion.primitives.common.proc import py_cmd, run_cmd  # noqa: E402


# --- child payloads (a channel step returns None on success, else one of these) ---

def blocked_child(
    *,
    message: str,
    accounts_path: Path,
    detail: Any = None,
    whatsapp_provider: str = "",
    qr_page: str = "",
    include_imessage: bool = False,
    include_whatsapp: bool = False,
) -> dict[str, Any]:
    """Build the ``blocked_user_action`` payload a channel returns when it needs
    a user step (Full Disk Access, a WhatsApp QR scan). Rebuilds an accurate
    ``--include-*`` continue command so the skill can resume the same channels."""
    command = (
        "uv run --project . python "
        "packs/ingestion/primitives/discover/messages/discover.py discover "
        f"--accounts {accounts_path}"
    )
    if include_imessage:
        command += " --include-imessage"
    if include_whatsapp:
        command += " --include-whatsapp"
    payload = {
        "primitive": "messages_discovery",
        "status": "blocked_user_action",
        "message": message,
        "detail": detail,
        "whatsapp_provider": whatsapp_provider,
        "qr_page": qr_page,
        "continue_command": command,
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


def failed_child(step_id: str, payload: dict[str, Any], stderr: str) -> dict[str, Any]:
    """Build the ``failed`` payload a channel (or the store's merge) returns when
    a child subprocess exits non-zero; picks the most specific error text."""
    detail = payload.get("error") or payload.get("message") or payload or stderr or "child command failed"
    return {
        "primitive": "messages_discovery",
        "status": "failed",
        "step_id": step_id,
        "error": detail,
    }


# --- channels: each source owns its output paths + extract -> normalize -------

class MessageChannel:
    """One message source (iMessage or WhatsApp). Owns its output paths and its
    extract -> normalize subprocess chain, and records what it contributed in
    ``artifacts``. extract()/normalize()/run() return None on success or a
    blocked/failed child payload that short-circuits the discovery run."""

    name = ""

    # A subclass sets these three fixed output paths in its __init__.
    contacts_csv: Path
    normalized_jsonl: Path
    normalized_manifest: Path

    def __init__(self, *, accounts_path: Path, other_enabled: bool) -> None:
        self.accounts_path = accounts_path
        # Whether the OTHER channel is enabled — only used to rebuild an accurate
        # `--include-*` continue command when this channel blocks.
        self.other_enabled = other_enabled
        self.artifacts: dict[str, Any] = {}

    def extract(self) -> dict[str, Any] | None:
        raise NotImplementedError

    def normalize(self) -> dict[str, Any] | None:
        """Normalize this channel's contacts CSV into JSONL. No-op when the JSONL
        is already at least as new as the CSV; writes an empty JSONL + manifest
        when the CSV is missing (a channel that produced no contacts)."""
        input_csv, output_jsonl, manifest = self.contacts_csv, self.normalized_jsonl, self.normalized_manifest
        if output_jsonl.exists() and (
            not input_csv.exists()
            or output_jsonl.stat().st_mtime_ns >= input_csv.stat().st_mtime_ns
        ):
            return None
        if not input_csv.exists():
            output_jsonl.parent.mkdir(parents=True, exist_ok=True)
            output_jsonl.write_text("", encoding="utf-8")
            write_json(manifest, {
                "primitive": "messages/normalize_contacts",
                "status": "ok",
                "reason": f"missing_input:{input_csv}",
                "output": str(output_jsonl),
                "counts": {"rows_written": 0},
            })
            return None
        code, payload, stderr = run_cmd(py_cmd(
            "packs/ingestion/primitives/discover/messages/normalize_contacts.py",
            "normalize",
            "--input", str(input_csv),
            "--out-jsonl", str(output_jsonl),
            "--manifest", str(manifest),
        ))
        if code != 0:
            return failed_child(f"normalize_{self.name}", payload, stderr)
        return None

    def run(self) -> dict[str, Any] | None:
        blocked = self.extract()
        if blocked is not None:
            return blocked
        return self.normalize()
