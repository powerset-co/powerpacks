"""One wacli metadata sync pass: how much to pull, pull it, refresh the rest.

`resolve_effective_max` is the whole sync-mode policy: every sync is unbounded
unless a cap is set through the environment. There is no user-facing sync mode.

`run_sync` then makes the single `wacli sync --once` call (contacts and groups
refreshed in the same pass), `refresh_contacts` / `refresh_group_info` top up
the metadata wacli did not push, and `store_stats` reports how big the store
got. The group refresh writes `wacli.group-participants.json`: the participant
roster for group chats, parsed once into `payloads.GroupInfo`, so the extractor
can attribute group membership without a second wacli round trip.

Changelog:
  2026-11-05: incremental syncs are uncapped by default (POWERPACKS_WACLI_INCREMENTAL_BUDGET=0).
    The old cap (store + 2,000) counted group chatter, so busy groups hit it every refresh, and
    whatsmeow had already acked the messages wacli refused after the cap, so those were lost.
  2026-11-04: a sync that reaches its cap ("storage limit reached", rc=1) moves on with what it
    stored instead of failing: after a few busy weeks every refresh stopped there (one store: 18,355
    stored, cap 20,355). The next sync's cap starts from the new total, so it catches up.
  2026-09-23 (typed rows): `run_sync` / `refresh_group_info` read the typed
    `runtime.CommandResult` (`.returncode`, `.combined_text`, `.json`) instead of
    indexing the subprocess result dict. Behavior unchanged.
  2026-07-30 (wacli split): extracted from the single-file `whatsapp_wacli.py`;
    `normalize_group_info_payload` became the typed `GroupInfo.from_payload`
    parse (`payloads.py`) and the cache entry it writes is unchanged.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import now_iso, write_json  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli import binary, runtime, store_db  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.paths import (  # noqa: E402
    DEFAULT_GROUP_PARTICIPANTS_CACHE,
    DEFAULT_STORE,
)
from packs.ingestion.primitives.discover.messages.wacli.payloads import GroupInfo  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.runtime import (  # noqa: E402
    PrimitiveBlocked,
    PrimitiveFailed,
)
from packs.ingestion.primitives.discover.messages.wacli.util import linked_device_blocked  # noqa: E402

DEFAULT_MAX_MESSAGES = int(os.environ.get("POWERPACKS_WACLI_MAX_MESSAGES", "0"))
# Optional store-size cap after the first sync: existing messages + headroom
# (max(1000, budget // 10)). 0 = no cap, the default: a sync stops at --idle-exit
# or the timeout. A cap counts group messages too, and whatsmeow acks the
# messages wacli refuses once it is reached, so a capped sync drops them.
DEFAULT_INCREMENTAL_BUDGET = int(os.environ.get("POWERPACKS_WACLI_INCREMENTAL_BUDGET", "0"))
DEFAULT_SYNC_TIMEOUT = int(os.environ.get("POWERPACKS_WACLI_SYNC_TIMEOUT", "10800"))
# What wacli prints when a sync stops at --max-messages.
STORAGE_LIMIT = "storage limit reached"


def effective_max_messages(requested: int, existing: int) -> int:
    if requested <= 0:
        return 0
    return max(requested, existing + max(1000, requested // 10))


def resolve_effective_max(requested: int, existing: int) -> int:
    """Choose full on an empty store and incremental once it is populated."""
    if requested and requested > 0:
        return effective_max_messages(requested, existing)
    if existing > 0:
        return effective_max_messages(DEFAULT_INCREMENTAL_BUDGET, existing)
    return 0


def run_sync(store: Path, *, timeout: int, idle_exit: str, max_messages: int) -> dict[str, Any]:
    runtime.emit_status("Syncing WhatsApp Messages and Contacts.")
    cmd = [
        binary.wacli_bin() or "wacli",
        "--store", str(store),
        "sync",
        "--once",
        "--idle-exit", idle_exit,
        "--refresh-contacts",
        "--refresh-groups",
        "--max-messages", str(max_messages),
    ]
    result = runtime.run_command(
        cmd,
        timeout=timeout,
        heartbeat_message="Syncing WhatsApp Messages and Contacts.",
    )
    text = result.combined_text
    if linked_device_blocked(text):
        raise PrimitiveBlocked({
            "status": "blocked_user_action",
            "message": "WhatsApp cannot link new devices right now. Try again later in WhatsApp, then rerun $import-messages.",
            "command": runtime.command_text(cmd),
        })
    # wacli exits 1 at the cap; what it stored up to there is kept and the run goes on.
    capped = STORAGE_LIMIT in text
    if result.returncode != 0 and not capped:
        detail = text.strip()[-2000:] or "no wacli output captured"
        raise PrimitiveFailed(
            f"sync failed rc={result.returncode} timeout={timeout}s max_messages={max_messages}; "
            f"command={runtime.command_text(cmd)}; output={detail}"
        )
    return {"command": runtime.command_text(cmd), "returncode": result.returncode, "max_messages": max_messages,
            "timeout": timeout, "capped": capped}


def refresh_contacts(store: Path) -> dict[str, Any]:
    try:
        payload = binary.wacli_json(store, ["contacts", "refresh"], timeout=300)
    except PrimitiveFailed as exc:
        return {"status": "warning", "error": str(exc)}
    return {"status": "ok", "payload": payload}


def group_participants_cache_path(store: Path) -> Path:
    if store == DEFAULT_STORE:
        return DEFAULT_GROUP_PARTICIPANTS_CACHE
    return store.parent / f"{store.name}.group-participants.json"


def refresh_group_info(store: Path, *, timeout: int, min_interval: float) -> dict[str, Any]:
    jids = store_db.group_chat_jids(store)
    cache_path = group_participants_cache_path(store)
    cache = {
        "version": 1,
        "updated_at": now_iso(),
        "groups": {},
    }
    summary = {
        "status": "ok",
        "group_chats": len(jids),
        "refreshed": 0,
        "not_participating": 0,
        "failed": 0,
        "cached_groups": 0,
        "cached_participants": 0,
    }
    for jid in jids:
        result = runtime.run_command(
            [binary.wacli_bin() or "wacli", "--store", str(store), "--json", "groups", "info", "--jid", jid],
            timeout=timeout,
        )
        text = (result.stderr or result.stdout or "").lower()
        if result.returncode == 0:
            summary["refreshed"] += 1
            group = GroupInfo.from_payload(result.json)
            if group:
                cache["groups"][group.jid] = group.as_cache_entry()
                summary["cached_groups"] += 1
                summary["cached_participants"] += len(group.participants)
        elif "not participating" in text or "not a participant" in text:
            summary["not_participating"] += 1
        else:
            summary["failed"] += 1
        if min_interval > 0:
            time.sleep(min_interval)
    if summary["failed"]:
        summary["status"] = "warning"
    write_json(cache_path, cache)
    summary["cache"] = str(cache_path)
    return summary


def store_stats(store: Path) -> dict[str, Any]:
    try:
        return binary.wacli_json(store, ["store", "stats"], timeout=60)
    except PrimitiveFailed as exc:
        return {"status": "warning", "error": str(exc)}
