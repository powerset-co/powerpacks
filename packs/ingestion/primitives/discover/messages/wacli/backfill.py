"""The `wacli history backfill-batch` boundary: ask WhatsApp for older messages.

wacli owns connection reuse, throttling, response correlation, and PN/LID
preference/fallback, so one command per run deepens every selected chat.
`WacliHistoryDepthAdapter` is the thin Powerpacks side of that: it builds the
command, runs it once, and converts the raw result into per-chat
`HistoryDepthAttempt` records by diffing the store's message counts around the
call — hashed chat refs only, never raw JIDs.

Two judgements live here and nowhere else:

- `classify_history_backfill_error` maps a return code plus stderr text onto
  `(category, retryable)`, so a network blip or a timeout stays resumable while
  an auth or access failure does not;
- a request that got no protocol response is NOT proof the server has no older
  history, so it is re-labelled `timeout` and stays retryable — as is a chat
  wacli left out of its result entirely (`missing_result`).

Changelog:
  2026-07-30 (wacli split): extracted from the single-file `whatsapp_wacli.py`;
    the command's JSON is now parsed once into `payloads.BackfillBatchResult`
    instead of re-guarded field by field. Classification and pacing unchanged.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.messages.wacli import binary, depth_db, runtime  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.payloads import (  # noqa: E402
    BackfillBatchResult,
    HistoryDepthAttempt,
    HistoryDepthTarget,
)

DEFAULT_HISTORY_DEPTH_COUNT = int(os.environ.get("POWERPACKS_WACLI_DEPTH_COUNT", "500"))
DEFAULT_HISTORY_DEPTH_REQUESTS = int(os.environ.get("POWERPACKS_WACLI_DEPTH_REQUESTS", "10"))
DEFAULT_HISTORY_DEPTH_REQUEST_DELAY = os.environ.get("POWERPACKS_WACLI_DEPTH_REQUEST_DELAY", "10s")
DEFAULT_HISTORY_DEPTH_BATCH_SIZE = int(os.environ.get("POWERPACKS_WACLI_DEPTH_BATCH_SIZE", "10"))
DEFAULT_HISTORY_DEPTH_MAX_IN_FLIGHT = int(
    os.environ.get("POWERPACKS_WACLI_DEPTH_MAX_IN_FLIGHT", "10")
)
DEFAULT_HISTORY_DEPTH_RESPONSE_WAIT = os.environ.get("POWERPACKS_WACLI_DEPTH_RESPONSE_WAIT", "10s")
DEFAULT_HISTORY_DEPTH_BATCH_DELAY = os.environ.get(
    "POWERPACKS_WACLI_DEPTH_BATCH_DELAY",
    "10s",
)
DEFAULT_HISTORY_DEPTH_TIMEOUT_BACKOFF = os.environ.get(
    "POWERPACKS_WACLI_DEPTH_TIMEOUT_BACKOFF",
    "1m",
)
DEFAULT_HISTORY_DEPTH_BUDGET_SECONDS = int(os.environ.get("POWERPACKS_WACLI_DEPTH_BUDGET_SECONDS", "6300"))
DEFAULT_HISTORY_DEPTH_ATTEMPT_TIMEOUT = int(
    os.environ.get(
        "POWERPACKS_WACLI_DEPTH_ATTEMPT_TIMEOUT",
        str(DEFAULT_HISTORY_DEPTH_BUDGET_SECONDS),
    )
)


def classify_history_backfill_error(
    *,
    returncode: int,
    stderr: str,
    requests_sent: int,
) -> tuple[str, bool]:
    if returncode == 0:
        return "none", False
    text = stderr.casefold()
    if returncode == 124 or "timed out" in text or "timeout" in text:
        return "timeout", True
    if any(token in text for token in (
        "no such host",
        "temporary failure in name resolution",
        "network is unreachable",
        "connection reset",
        "connection refused",
        "no route to host",
        "dial tcp",
        "i/o timeout",
        "websocket",
    )):
        return "connection", True
    if "database is locked" in text or "store is locked" in text or "lock wait" in text:
        return "store_lock", True
    if "not authenticated" in text or "logged out" in text:
        return "unauthenticated", False
    if "access denied" in text or "forbidden" in text or "no access" in text:
        return "access_limited", False
    if requests_sent > 0:
        return "request_error", False
    return "command_error", False


@dataclass(frozen=True)
class WacliHistoryDepthAdapter:
    """Thin Powerpacks boundary around wacli's native batch command.

    wacli owns connection reuse, throttling, response correlation, and PN/LID
    preference/fallback. This adapter owns only command construction plus the
    privacy-safe conversion from raw command results to hashed stage rows.
    """

    store: Path
    count: int = DEFAULT_HISTORY_DEPTH_COUNT
    requests: int = DEFAULT_HISTORY_DEPTH_REQUESTS
    request_delay: str = DEFAULT_HISTORY_DEPTH_REQUEST_DELAY
    batch_size: int = DEFAULT_HISTORY_DEPTH_BATCH_SIZE
    max_in_flight: int = DEFAULT_HISTORY_DEPTH_MAX_IN_FLIGHT
    response_wait: str = DEFAULT_HISTORY_DEPTH_RESPONSE_WAIT
    batch_delay: str = DEFAULT_HISTORY_DEPTH_BATCH_DELAY
    timeout_backoff: str = DEFAULT_HISTORY_DEPTH_TIMEOUT_BACKOFF
    timeout: int = DEFAULT_HISTORY_DEPTH_ATTEMPT_TIMEOUT

    def command(self, targets: list[HistoryDepthTarget]) -> list[str]:
        cmd = [
            binary.wacli_bin() or "wacli",
            "--store",
            str(self.store),
            "--json",
            "history",
            "backfill-batch",
            "--count",
            str(self.count),
            "--requests",
            str(self.requests),
            "--wait",
            self.response_wait,
            "--request-delay",
            self.request_delay,
            "--batch-size",
            str(self.batch_size),
            "--max-inflight",
            str(self.max_in_flight),
            "--batch-delay",
            self.batch_delay,
            "--timeout-backoff",
            self.timeout_backoff,
            "--idle-exit",
            "5s",
        ]
        for target in targets:
            cmd.extend(["--chat", target.chat_jid])
        return cmd

    def run(
        self,
        targets: list[HistoryDepthTarget],
    ) -> tuple[dict[str, HistoryDepthAttempt], int]:
        if not targets:
            return {}, 0
        before_counts = {
            target.chat_ref: depth_db.history_depth_counts(self.store, target.chat_jid)[0]
            for target in targets
        }
        before_total = depth_db.history_depth_total_count(self.store)
        result = runtime.run_command(
            self.command(targets),
            timeout=self.timeout,
            heartbeat_message="Deepening WhatsApp history.",
        )
        batch = BackfillBatchResult.from_command_json(result.get("json"))
        global_returncode = int(result.get("returncode") or 0)
        stderr = str(result.get("stderr") or "")
        attempts: dict[str, HistoryDepthAttempt] = {}
        total_target_added = 0
        for target in targets:
            after_count, _after_total, after_latest_ts = depth_db.history_depth_counts(
                self.store,
                target.chat_jid,
            )
            target_added = max(0, after_count - before_counts[target.chat_ref])
            total_target_added += target_added
            chat = batch.chat(target.chat_jid)
            local_returncode = global_returncode
            if local_returncode == 0 and chat.error:
                local_returncode = (
                    124 if "timed out" in chat.error.casefold() else 1
                )
            error_text = "\n".join(
                part for part in (chat.error, stderr) if part
            )
            error_category, retryable = classify_history_backfill_error(
                returncode=local_returncode,
                stderr=error_text,
                requests_sent=chat.requests_sent,
            )
            if not chat.present and global_returncode == 0:
                error_category = "missing_result"
                retryable = True
            elif (
                local_returncode == 0
                and chat.requests_sent > 0
                and chat.responses_seen == 0
            ):
                # A request without a protocol response is not proof that the
                # server has no older history. Keep it resumable.
                error_category = "timeout"
                retryable = True
            attempts[target.chat_ref] = HistoryDepthAttempt(
                returncode=local_returncode,
                requests_sent=chat.requests_sent,
                responses_seen=chat.responses_seen,
                target_added=target_added,
                unrelated_added=0,
                after_count=after_count,
                error_category=error_category,
                retryable=retryable,
                after_latest_ts=after_latest_ts,
                messages_received=chat.messages_received,
                end_type=chat.end_type,
            )
        after_total = depth_db.history_depth_total_count(self.store)
        unrelated_added = max(
            0,
            (after_total - before_total) - total_target_added,
        )
        return attempts, unrelated_added


def run_history_backfill_batch_attempt(
    store: Path,
    targets: list[HistoryDepthTarget],
    *,
    count: int = DEFAULT_HISTORY_DEPTH_COUNT,
    requests: int = DEFAULT_HISTORY_DEPTH_REQUESTS,
    request_delay: str = DEFAULT_HISTORY_DEPTH_REQUEST_DELAY,
    batch_size: int = DEFAULT_HISTORY_DEPTH_BATCH_SIZE,
    max_in_flight: int = DEFAULT_HISTORY_DEPTH_MAX_IN_FLIGHT,
    response_wait: str = DEFAULT_HISTORY_DEPTH_RESPONSE_WAIT,
    batch_delay: str = DEFAULT_HISTORY_DEPTH_BATCH_DELAY,
    timeout_backoff: str = DEFAULT_HISTORY_DEPTH_TIMEOUT_BACKOFF,
    timeout: int = DEFAULT_HISTORY_DEPTH_ATTEMPT_TIMEOUT,
) -> tuple[dict[str, HistoryDepthAttempt], int]:
    return WacliHistoryDepthAdapter(
        store=store,
        count=count,
        requests=requests,
        request_delay=request_delay,
        batch_size=batch_size,
        max_in_flight=max_in_flight,
        response_wait=response_wait,
        batch_delay=batch_delay,
        timeout_backoff=timeout_backoff,
        timeout=timeout,
    ).run(targets)


def run_history_backfill_attempt(
    store: Path,
    target: HistoryDepthTarget,
    *,
    count: int = DEFAULT_HISTORY_DEPTH_COUNT,
    requests: int = DEFAULT_HISTORY_DEPTH_REQUESTS,
    request_delay: str = DEFAULT_HISTORY_DEPTH_REQUEST_DELAY,
    timeout: int = DEFAULT_HISTORY_DEPTH_ATTEMPT_TIMEOUT,
) -> HistoryDepthAttempt:
    attempts, unrelated_added = run_history_backfill_batch_attempt(
        store,
        [target],
        count=count,
        requests=requests,
        request_delay=request_delay,
        batch_size=1,
        max_in_flight=1,
        timeout=timeout,
    )
    attempt = attempts[target.chat_ref]
    return replace(attempt, unrelated_added=unrelated_added)
