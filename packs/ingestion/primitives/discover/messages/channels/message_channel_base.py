"""MessageChannel base: the per-source extract contract plus the channel
return-shape payload builders (``blocked_child`` / ``failed_child``).

A channel is one message source (iMessage or WhatsApp). The concrete subclasses
(``IMessageChannel`` / ``WhatsAppChannel``, in sibling modules) are
``MessageChannel`` AND ``pipeline/contract.py:Node``: they DECLARE their inputs
and outputs, set their fixed output paths in their own ``__init__``, and own
their in-process call into the leaf extractor class. ``execute()`` — the Node
template's one hook — is the whole step: it returns
``MessageChannelExtracted`` carrying what this channel produced, or the
``MessageChannelBlocked`` / ``MessageChannelFailed`` shape that short-circuits
the store's run loop. ``blocked_child`` and ``failed_child`` are the shared
return shapes both channels (and the store's merge) emit; they live here as the
base's return-shape helpers.

``MessageChannel`` itself is deliberately NOT a ``Node``: it is an abstract base
with no contract of its own, and a Node subclass would have to declare
name/inputs/outputs and would then show up in the declared graph as a phantom
node. The concrete channels inherit ``(MessageChannel, Node)``.

Changelog:
  2026-07-30 (steps return results): ``extract()`` and the per-channel
    ``self.artifacts`` dict are GONE; ``execute()`` is the single step method and
    a success is a VALUE (``MessageChannelExtracted``), not ``None``. A channel
    used to signal success by returning ``None`` and record what it produced by
    mutating an untyped ``artifacts`` dict that the store unioned back out of the
    channel objects after the loop — a step writing into a shared blob the
    orchestrator reads afterwards. The store now composes the manifest from the
    returned payloads; the rendered ``artifacts`` keys and values are unchanged.
  2026-07-25 (normalize deleted): ``normalize()`` and the whole
    ``normalize_contacts.py`` primitive are GONE. Its output
    (``*.contacts.normalized.jsonl``) was byte-for-byte identical to the
    extractors' own ``*.contacts.raw.jsonl`` — verified sha256-equal on real
    local data — and had zero readers repo-wide, so the step re-derived a file
    nobody opened. The ``normalized_jsonl``/``normalized_manifest`` channel paths
    and the ``IMESSAGE_NORMALIZED_*``/``WHATSAPP_NORMALIZED_*`` constants went
    with it.
  2026-07-25 (declared contract): ``run()`` was REMOVED and replaced by
    ``execute()``. That is load-bearing, not cosmetic: the concrete channels now
    inherit ``(MessageChannel, Node)``, and a ``run()`` on this base would shadow
    the Node run template in the MRO — silently, because the template's
    "do not override run()" guard only inspects the subclass's own ``__dict__``.
    ``extract()`` now returns the TYPED channel payloads of ``models.py`` instead
    of hand-built dicts, and the channel's source name moved from ``name`` to
    ``channel`` because ``name`` is now the declared NODE name.
  2026-07-23 (explicit-selection): dropped the ``accounts_path`` parameter from
    ``MessageChannel.__init__`` and from ``blocked_child`` — message channel
    selection is now explicit ``--include-*`` only, so nothing reads accounts.json.
    The blocked/QR-resume ``continue_command`` is rebuilt from the include flags
    alone (``discover.py discover [--include-imessage] [--include-whatsapp]``, no
    ``--accounts``).
  2026-07-23 (terse): dropped the ``@property``/``NotImplementedError`` path
    accessors that existed to read module constants at call time for test
    patching; subclasses now set their fixed paths as plain attributes in their
    own ``__init__``.
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

from packs.ingestion.primitives.discover.messages.models import (  # noqa: E402
    MessageChannelBlocked,
    MessageChannelExtracted,
    MessageChannelFailed,
)


# --- child payloads (a channel step returns None on success, else one of these) ---

def blocked_child(
    *,
    message: str,
    detail: Any = None,
    whatsapp_provider: str = "",
    qr_page: str = "",
    include_imessage: bool = False,
    include_whatsapp: bool = False,
) -> MessageChannelBlocked:
    """Build the ``blocked_user_action`` payload a channel returns when it needs
    a user step (Full Disk Access, a WhatsApp QR scan). Rebuilds an accurate
    ``--include-*`` continue command (no ``--accounts``) so the skill can resume
    the same channels.

    The empty-string arguments become ``None`` on the payload so
    ``to_payload()``'s ``exclude_none`` drops those keys — the same shape the
    hand-rolled ``value not in (None, "")`` filter produced."""
    command = (
        "uv run --project . python "
        "packs/ingestion/primitives/discover/messages/discover.py discover"
    )
    if include_imessage:
        command += " --include-imessage"
    if include_whatsapp:
        command += " --include-whatsapp"
    return MessageChannelBlocked(
        message=message,
        detail=detail or None,
        whatsapp_provider=whatsapp_provider or None,
        qr_page=qr_page or None,
        continue_command=command,
    )


def failed_child(step_id: str, payload: dict[str, Any], stderr: str) -> MessageChannelFailed:
    """Build the ``failed`` payload a channel (or the store's merge) returns when
    a child step reports a non-success status; picks the most specific error text."""
    detail = payload.get("error") or payload.get("message") or payload or stderr or "child command failed"
    return MessageChannelFailed(step_id=step_id, error=detail)


# --- channels: each source owns its output paths + its extract step -----------

class MessageChannel:
    """One message source (iMessage or WhatsApp). Owns its output paths and its
    in-process call into the leaf extractor, and RETURNS what it contributed.

    ``execute()`` (the Node template's hook) is the whole step: a typed
    ``MessageChannelExtracted`` on success, otherwise the blocked/failed payload
    that stops the store's run loop. Nothing is written into shared state on the
    way — the store composes the stage manifest from these returns.

    Not a ``Node`` itself — see the module docstring."""

    # The message SOURCE name, used to build step ids and to key this channel's
    # contribution in the stage manifest (`<channel>_contacts_csv`, ...). The
    # declared NODE name is the `name` ClassVar each concrete channel sets
    # alongside its contract.
    channel = ""

    # A subclass sets this fixed output path in its __init__.
    contacts_csv: Path

    def __init__(self, *, other_enabled: bool) -> None:
        # Whether the OTHER channel is enabled — only used to rebuild an accurate
        # `--include-*` continue command when this channel blocks.
        self.other_enabled = other_enabled

    def execute(self) -> MessageChannelExtracted | MessageChannelBlocked | MessageChannelFailed:
        raise NotImplementedError
