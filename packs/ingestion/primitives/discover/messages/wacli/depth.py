"""The history-depth stage: deepen recent shallow DMs, resumably.

WhatsApp only pushes a shallow slice of each conversation at pair time. This
stage picks the direct chats that are recent (within the three-year lookback)
and still shallow (at or below the message threshold), asks wacli for older
history in ONE batch command, and records what came back — then stops. It is
paced and budgeted on purpose: the next `$import-messages` run resumes from the
artifacts on disk, there is no ledger and no retry loop.

Flow: read `results.csv` -> reconcile every known row against the live store
(gone / out of scope / already deep enough) -> decide bootstrap vs changed-only
from the previous manifest's watermark -> select targets -> persist EVERY
selected target before the first network call -> run one batch -> fold each
attempt into its row -> persist after each fold.

Outputs are fixed paths overwritten in place — `results.csv`, `progress.jsonl`,
`manifest.json` — and their shape lives next door in `depth_results.py`; this
module owns the run.

Changelog:
  2026-07-30 (wacli split): extracted from the single-file `whatsapp_wacli.py`.
    The store queries it selects with moved to `depth_db.py`, the wacli batch
    command to `backfill.py`, and the results/summary/manifest artifacts to
    `depth_results.py`; the previous manifest is read through the typed
    `payloads.PriorDepthManifest` instead of four inline isinstance guards.
    Artifacts and outcomes unchanged.
"""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import now_iso  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli import (  # noqa: E402
    backfill,
    depth_db,
    depth_results,
    runtime,
)
from packs.ingestion.primitives.discover.messages.wacli.backfill import (  # noqa: E402
    DEFAULT_HISTORY_DEPTH_ATTEMPT_TIMEOUT,
    DEFAULT_HISTORY_DEPTH_BATCH_DELAY,
    DEFAULT_HISTORY_DEPTH_BATCH_SIZE,
    DEFAULT_HISTORY_DEPTH_BUDGET_SECONDS,
    DEFAULT_HISTORY_DEPTH_COUNT,
    DEFAULT_HISTORY_DEPTH_MAX_IN_FLIGHT,
    DEFAULT_HISTORY_DEPTH_REQUEST_DELAY,
    DEFAULT_HISTORY_DEPTH_REQUESTS,
    DEFAULT_HISTORY_DEPTH_RESPONSE_WAIT,
    DEFAULT_HISTORY_DEPTH_TIMEOUT_BACKOFF,
)
from packs.ingestion.primitives.discover.messages.wacli.depth_results import (  # noqa: E402
    DEFAULT_HISTORY_DEPTH_NO_GROWTH_LIMIT,
    HISTORY_DEPTH_POLICY_VERSION,
    HISTORY_DEPTH_TERMINAL_OUTCOMES,
)
from packs.ingestion.primitives.discover.messages.wacli.paths import DEFAULT_HISTORY_DEPTH_DIR  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.payloads import (  # noqa: E402
    HistoryDepthAttempt,
    HistoryDepthTarget,
)
from packs.ingestion.primitives.discover.messages.wacli.runtime import PrimitiveFailed  # noqa: E402
from packs.ingestion.primitives.discover.messages.wacli.depth_db import (  # noqa: E402
    DEFAULT_HISTORY_DEPTH_MAX_COUNT,
)
from packs.ingestion.primitives.discover.messages.wacli.util import (  # noqa: E402
    history_chat_ref,
    history_depth_cutoff_ts,
    history_depth_state_digest,
    result_int,
)

HISTORY_DEPTH_MORE_REMAIN_END_TYPES = {
    "COMPLETE_BUT_MORE_MESSAGES_REMAIN_ON_PRIMARY",
    "COMPLETE_ON_DEMAND_SYNC_BUT_MORE_MSG_REMAIN_ON_PRIMARY",
}


def run_history_depth_stage(
    store: Path,
    *,
    out_dir: Path = DEFAULT_HISTORY_DEPTH_DIR,
    active_since_ts: int | None = None,
    max_count: int = DEFAULT_HISTORY_DEPTH_MAX_COUNT,
    count: int = DEFAULT_HISTORY_DEPTH_COUNT,
    requests: int = DEFAULT_HISTORY_DEPTH_REQUESTS,
    request_delay: str = DEFAULT_HISTORY_DEPTH_REQUEST_DELAY,
    no_growth_limit: int = DEFAULT_HISTORY_DEPTH_NO_GROWTH_LIMIT,
    batch_size: int = DEFAULT_HISTORY_DEPTH_BATCH_SIZE,
    max_in_flight: int = DEFAULT_HISTORY_DEPTH_MAX_IN_FLIGHT,
    response_wait: str = DEFAULT_HISTORY_DEPTH_RESPONSE_WAIT,
    batch_delay: str = DEFAULT_HISTORY_DEPTH_BATCH_DELAY,
    timeout_backoff: str = DEFAULT_HISTORY_DEPTH_TIMEOUT_BACKOFF,
    attempt_timeout: int = DEFAULT_HISTORY_DEPTH_ATTEMPT_TIMEOUT,
    time_budget_seconds: int = DEFAULT_HISTORY_DEPTH_BUDGET_SECONDS,
    before_states: dict[str, tuple[int, int]] | None = None,
    before_total_messages: int | None = None,
    cold_start: bool = False,
    exclude_jids: set[str] | None = None,
) -> dict[str, Any]:
    if active_since_ts is None:
        active_since_ts = history_depth_cutoff_ts()
    results_path = out_dir / "results.csv"
    progress_path = out_dir / "progress.jsonl"
    manifest_path = out_dir / "manifest.json"
    initialized = results_path.exists()
    rows: dict[str, dict[str, Any]] = depth_results.read_history_depth_results(results_path)
    current_states = depth_db.history_depth_chat_states(store)
    current_by_ref = {
        history_chat_ref(chat_jid): state
        for chat_jid, state in current_states.items()
    }
    excluded_refs = {history_chat_ref(jid) for jid in (exclude_jids or set())}
    for chat_ref, row in rows.items():
        if row.get("outcome") in HISTORY_DEPTH_TERMINAL_OUTCOMES:
            continue
        if chat_ref in excluded_refs:
            row["outcome"] = "out_of_scope"
            row["error_category"] = "none"
            row["updated_at"] = now_iso()
            continue
        current_state = current_by_ref.get(chat_ref)
        if current_state is None:
            row["outcome"] = "gone"
            row["error_category"] = "none"
            row["updated_at"] = now_iso()
        elif current_state[1] < active_since_ts:
            row["current_count"] = current_state[0]
            row["current_latest_ts"] = current_state[1]
            row["outcome"] = "out_of_scope"
            row["error_category"] = "none"
            row["updated_at"] = now_iso()
        elif current_state[0] > max_count:
            row["current_count"] = current_state[0]
            row["current_latest_ts"] = current_state[1]
            row["outcome"] = "completed_threshold"
            row["error_category"] = "none"
            row["updated_at"] = now_iso()
    previous = depth_results.read_history_depth_manifest(manifest_path)
    pre_sync_states = before_states if before_states is not None else current_states
    pre_sync_digest = history_depth_state_digest(pre_sync_states)
    recovered_pre_sync_changes = (
        initialized
        and (
            (
                previous.source_total_messages is not None
                and before_total_messages is not None
                and previous.source_total_messages != before_total_messages
            )
            or (
                previous.has_source_digest
                and previous.dm_state_sha256 != pre_sync_digest
            )
        )
    )
    bootstrap = (
        cold_start
        or not initialized
        or previous.policy_version != HISTORY_DEPTH_POLICY_VERSION
        or not previous.has_source_total
        or not previous.has_source_digest
        or recovered_pre_sync_changes
    )
    # This watermark represents the post-account-sync state whose changed
    # chats are about to be durably seeded. Keep it fixed during targeted
    # backfill: if WhatsApp returns rows for another chat, the next invocation
    # sees the mismatch and performs one catch-up bootstrap.
    source_total_messages = depth_db.history_depth_total_count(store)
    source_dm_state_sha256 = history_depth_state_digest(current_states)
    resume_refs = {
        chat_ref
        for chat_ref, row in rows.items()
        if row.get("outcome") not in HISTORY_DEPTH_TERMINAL_OUTCOMES
    }
    targets = depth_db.history_depth_targets(
        store,
        active_since_ts=active_since_ts,
        max_count=max_count,
        before_states=before_states,
        bootstrap=bootstrap,
        resume_refs=resume_refs,
        exclude_jids=exclude_jids,
    )
    stage_started = time.monotonic()
    runtime.write_progress(progress_path, {
        "event": "history_depth_started",
        "eligible": len(targets),
        "selection": "bootstrap_recent_shallow" if bootstrap else "changed_recent_shallow",
        "recovered_pre_sync_changes": recovered_pre_sync_changes,
    })

    def persist() -> dict[str, Any]:
        depth_results.write_history_depth_results(results_path, rows)
        summary = depth_results.history_depth_summary(
            targets=targets,
            rows=rows,
            results_path=results_path,
            progress_path=progress_path,
            active_since_ts=active_since_ts,
            max_count=max_count,
            count=count,
            requests=requests,
            request_delay=request_delay,
            no_growth_limit=no_growth_limit,
            batch_size=batch_size,
            max_in_flight=max_in_flight,
            response_wait=response_wait,
            batch_delay=batch_delay,
            timeout_backoff=timeout_backoff,
            time_budget_seconds=time_budget_seconds,
            bootstrap=bootstrap,
            source_total_messages=source_total_messages,
            source_dm_state_sha256=source_dm_state_sha256,
            recovered_pre_sync_changes=recovered_pre_sync_changes,
        )
        return depth_results.write_history_depth_manifest(out_dir, summary)

    for target in targets:
        row = rows.get(target.chat_ref)
        if row is None:
            rows[target.chat_ref] = {
                "chat_ref": target.chat_ref,
                "kind": target.kind,
                "initial_count": target.current_count,
                "current_count": target.current_count,
                "current_latest_ts": target.current_latest_ts,
                "target_rows_added": 0,
                "unrelated_rows_added": 0,
                "attempts": 0,
                "requests_sent": 0,
                "responses_seen": 0,
                "transient_failures": 0,
                "no_growth_attempts": 0,
                "outcome": "pending",
                "error_category": "none",
                "updated_at": now_iso(),
            }
        elif (
            row.get("outcome") in {"gone", "out_of_scope"}
            or result_int(row, "current_count") != target.current_count
            or result_int(row, "current_latest_ts") != target.current_latest_ts
            or target.state_changed
        ):
            row["current_count"] = target.current_count
            row["current_latest_ts"] = target.current_latest_ts
            row["no_growth_attempts"] = 0
            row["outcome"] = "pending"
            row["error_category"] = "none"

    # Persist every selected target before the first network request so budget
    # exhaustion or interruption cannot lose unvisited work.
    summary = persist()

    def target_needs_attempt(candidate: HistoryDepthTarget) -> bool:
        candidate_row = rows.get(candidate.chat_ref)
        if candidate_row is None:
            return True
        return not (
            candidate_row.get("outcome") in HISTORY_DEPTH_TERMINAL_OUTCOMES
            and result_int(candidate_row, "current_count") == candidate.current_count
        )

    attempt_targets = [target for target in targets if target_needs_attempt(target)]
    if attempt_targets:
        elapsed = time.monotonic() - stage_started
        if time_budget_seconds > 0 and elapsed >= time_budget_seconds:
            runtime.write_progress(progress_path, {"event": "history_depth_budget_exhausted"})
            return persist()
        command_timeout = attempt_timeout
        if time_budget_seconds > 0:
            command_timeout = min(
                command_timeout,
                max(1, int(time_budget_seconds - elapsed)),
            )
        runtime.write_progress(progress_path, {
            "event": "history_depth_batch_started",
            "targets": len(attempt_targets),
            "batch_size": batch_size,
            "max_in_flight": max_in_flight,
        })
        if len(attempt_targets) == 1:
            target = attempt_targets[0]
            attempt = backfill.run_history_backfill_attempt(
                store,
                target,
                count=count,
                requests=requests,
                request_delay=request_delay,
                timeout=command_timeout,
            )
            attempts = {target.chat_ref: attempt}
            batch_unrelated_added = attempt.unrelated_added
            attempts[target.chat_ref] = replace(attempt, unrelated_added=0)
        else:
            attempts, batch_unrelated_added = backfill.run_history_backfill_batch_attempt(
                store,
                attempt_targets,
                count=count,
                requests=requests,
                request_delay=request_delay,
                batch_size=batch_size,
                max_in_flight=max_in_flight,
                response_wait=response_wait,
                batch_delay=batch_delay,
                timeout_backoff=timeout_backoff,
                timeout=command_timeout,
            )
        for index, target in enumerate(attempt_targets):
            row = rows.get(target.chat_ref)
            if row is None:
                raise PrimitiveFailed("history depth target was not seeded")
            attempt = attempts.get(target.chat_ref)
            if attempt is None:
                attempt = HistoryDepthAttempt(
                    returncode=0,
                    requests_sent=0,
                    responses_seen=0,
                    target_added=0,
                    unrelated_added=0,
                    after_count=target.current_count,
                    error_category="missing_result",
                    retryable=True,
                    after_latest_ts=target.current_latest_ts,
                )
            if index == 0 and batch_unrelated_added:
                attempt = replace(
                    attempt,
                    unrelated_added=attempt.unrelated_added + batch_unrelated_added,
                )

            row["attempts"] = result_int(row, "attempts") + 1
            row["requests_sent"] = result_int(row, "requests_sent") + attempt.requests_sent
            row["responses_seen"] = result_int(row, "responses_seen") + attempt.responses_seen
            row["target_rows_added"] = result_int(row, "target_rows_added") + attempt.target_added
            row["unrelated_rows_added"] = result_int(row, "unrelated_rows_added") + attempt.unrelated_added
            row["current_count"] = attempt.after_count
            if attempt.after_latest_ts:
                row["current_latest_ts"] = attempt.after_latest_ts
            row["error_category"] = attempt.error_category
            row["updated_at"] = now_iso()

            if attempt.returncode == 0 and attempt.target_added > 0:
                row["no_growth_attempts"] = 0
                if attempt.after_count > max_count:
                    row["outcome"] = "completed_threshold"
                elif attempt.end_type == "COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY":
                    row["outcome"] = "recovered"
                else:
                    row["outcome"] = "pending"
            elif attempt.retryable:
                row["transient_failures"] = result_int(row, "transient_failures") + 1
                if attempt.target_added > 0:
                    row["no_growth_attempts"] = 0
                row["outcome"] = (
                    "completed_threshold"
                    if attempt.after_count > max_count
                    else "pending"
                )
            elif (
                attempt.returncode == 0
                and attempt.responses_seen > 0
                and attempt.messages_received == 0
            ):
                if attempt.end_type in HISTORY_DEPTH_MORE_REMAIN_END_TYPES:
                    row["no_growth_attempts"] = 0
                    row["outcome"] = "pending"
                else:
                    row["no_growth_attempts"] = result_int(row, "no_growth_attempts") + 1
                    row["outcome"] = (
                        "server_zero"
                        if result_int(row, "no_growth_attempts") >= no_growth_limit
                        else "pending"
                    )
            elif attempt.returncode == 0:
                row["outcome"] = "pending"
            else:
                row["outcome"] = "terminal_error"

            runtime.write_progress(progress_path, {
                "event": "history_depth_attempt",
                "chat_ref": target.chat_ref,
                "attempt": result_int(row, "attempts"),
                "requests_sent": attempt.requests_sent,
                "responses_seen": attempt.responses_seen,
                "messages_received": attempt.messages_received,
                "target_added": attempt.target_added,
                "unrelated_added": attempt.unrelated_added,
                "outcome": row["outcome"],
                "error_category": attempt.error_category,
            })
            summary = persist()

    runtime.write_progress(progress_path, {
        "event": "history_depth_completed",
        "eligible": summary["counts"]["eligible"],
        "completed": summary["counts"]["completed"],
        "pending": summary["counts"]["pending"],
    })
    return summary
