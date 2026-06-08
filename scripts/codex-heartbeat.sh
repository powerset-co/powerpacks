#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: codex-heartbeat.sh [--once|--help]

Runs a config-gated Codex heartbeat loop from a Powerpacks checkout. The loop is
cheap when no processing is due: it only reads local config/state and does not
invoke Codex until the configured schedule says a run is due.

Environment:
  POWERPACKS_REPO_ROOT             Powerpacks checkout path (default: script repo root)
  CODEX_HOME                       Writable Codex home inside the container (default: ~/.codex)
  HOST_CODEX_HOME                  Optional read-only host Codex home snapshot (default: /host-codex)
  POWERPACKS_SYNC_HOST_CODEX_HOME  Copy HOST_CODEX_HOME into CODEX_HOME on startup (default: 1)
  POWERPACKS_HEARTBEAT_SKIP_INSTALL Skip Powerpacks skill install before heartbeats (default: 0)
  HEARTBEAT_POLL_SECONDS           Delay between local due checks (default: HEARTBEAT_INTERVAL_SECONDS or 300)
  POWERPACKS_HEARTBEAT_CONFIG      JSON config with prompt/schedule
  POWERPACKS_HEARTBEAT_STATE       JSON state file with last run timestamps
  CODEX_HEARTBEAT_PROMPT           Optional env override for prompt in config
  HEARTBEAT_ONCE                   Run exactly one heartbeat and exit when set to 1
USAGE
}

ONCE="${HEARTBEAT_ONCE:-0}"
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
elif [[ "${1:-}" == "--once" ]]; then
  ONCE=1
elif [[ -n "${1:-}" ]]; then
  usage >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
POWERPACKS_REPO_ROOT="${POWERPACKS_REPO_ROOT:-$DEFAULT_REPO_ROOT}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
HOST_CODEX_HOME="${HOST_CODEX_HOME:-/host-codex}"
HEARTBEAT_POLL_SECONDS="${HEARTBEAT_POLL_SECONDS:-${HEARTBEAT_INTERVAL_SECONDS:-300}}"
POWERPACKS_SYNC_HOST_CODEX_HOME="${POWERPACKS_SYNC_HOST_CODEX_HOME:-1}"
POWERPACKS_HEARTBEAT_SKIP_INSTALL="${POWERPACKS_HEARTBEAT_SKIP_INSTALL:-0}"
POWERPACKS_HEARTBEAT_CONFIG="${POWERPACKS_HEARTBEAT_CONFIG:-}"
POWERPACKS_HEARTBEAT_STATE="${POWERPACKS_HEARTBEAT_STATE:-}"

export CODEX_HOME

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

runner_args() {
  if [[ -n "$POWERPACKS_HEARTBEAT_CONFIG" ]]; then
    printf '%s\0%s\0' --config "$POWERPACKS_HEARTBEAT_CONFIG"
  fi
  if [[ -n "$POWERPACKS_HEARTBEAT_STATE" ]]; then
    printf '%s\0%s\0' --state "$POWERPACKS_HEARTBEAT_STATE"
  fi
}

collect_runner_args() {
  RUNNER_ARGS=()
  while IFS= read -r -d '' arg; do
    RUNNER_ARGS+=("$arg")
  done < <(runner_args)
}

sync_host_codex_home() {
  if [[ "$POWERPACKS_SYNC_HOST_CODEX_HOME" != "1" ]]; then
    return 0
  fi
  if [[ ! -d "$HOST_CODEX_HOME" ]]; then
    log "host Codex home not mounted at $HOST_CODEX_HOME; using container CODEX_HOME=$CODEX_HOME"
    return 0
  fi

  mkdir -p "$CODEX_HOME"
  # Copy a read-only host login/config snapshot into the writable container home.
  # This lets the container use the regular-shell login without writing back to it.
  rsync -a --delete \
    --exclude 'log/' \
    --exclude 'logs/' \
    --exclude 'sessions/' \
    --exclude 'tmp/' \
    "$HOST_CODEX_HOME"/ "$CODEX_HOME"/
  chmod -R u+rwX "$CODEX_HOME" 2>/dev/null || true
  log "synced host Codex login/config snapshot from $HOST_CODEX_HOME into $CODEX_HOME"
}

prepare_codex_auth() {
  if [[ -f "$CODEX_HOME/auth.json" ]]; then
    return 0
  fi
  if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    return 0
  fi

  log "creating container Codex auth from OPENAI_API_KEY"
  mkdir -p "$CODEX_HOME"
  if ! printf '%s\n' "$OPENAI_API_KEY" | codex login --with-api-key >/dev/null; then
    log "warning: codex login --with-api-key failed; heartbeat will try environment auth"
  fi
}

run_install() {
  if [[ "$POWERPACKS_HEARTBEAT_SKIP_INSTALL" == "1" ]]; then
    log "skipping Powerpacks Codex install (POWERPACKS_HEARTBEAT_SKIP_INSTALL=1)"
    return 0
  fi
  log "installing/updating Powerpacks Codex skills from mounted checkout"
  export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/root/.cache/powerpacks/.venv}"
  export POWERPACKS_SKIP_AGENT_BOOTSTRAP="${POWERPACKS_SKIP_AGENT_BOOTSTRAP:-1}"
  "$POWERPACKS_REPO_ROOT/install.sh" codex
}

prepare_due_run() {
  sync_host_codex_home || return $?
  prepare_codex_auth || return $?
  run_install || return $?
}

run_heartbeat_tick() {
  if [[ ! -f "$CODEX_HOME/auth.json" && -z "${OPENAI_API_KEY:-}" ]]; then
    log "warning: no $CODEX_HOME/auth.json and OPENAI_API_KEY is unset; codex may require login"
  fi
  python3 "$POWERPACKS_REPO_ROOT/scripts/codex-heartbeat-runner.py" "${RUNNER_ARGS[@]}" --include-pending
}

heartbeat_due_status() {
  set +e
  python3 "$POWERPACKS_REPO_ROOT/scripts/codex-heartbeat-runner.py" "${RUNNER_ARGS[@]}" --check-due
  local status=$?
  set -e
  return "$status"
}

record_due_attempt() {
  python3 "$POWERPACKS_REPO_ROOT/scripts/codex-heartbeat-runner.py" \
    "${RUNNER_ARGS[@]}" \
    --record-attempt \
    --attempt-reason "preparing due heartbeat run"
}

record_prep_failure() {
  local status="$1"
  python3 "$POWERPACKS_REPO_ROOT/scripts/codex-heartbeat-runner.py" \
    "${RUNNER_ARGS[@]}" \
    --record-failure "$status" \
    --failure-reason "heartbeat preparation failed before Codex invocation"
}

if [[ ! -d "$POWERPACKS_REPO_ROOT" ]]; then
  log "error: Powerpacks repo not found at $POWERPACKS_REPO_ROOT"
  exit 1
fi
cd "$POWERPACKS_REPO_ROOT"
collect_runner_args

while true; do
  heartbeat_status=0
  due_status=0
  heartbeat_due_status || due_status=$?
  if [[ "$due_status" == "10" ]]; then
    record_due_attempt || true
    prep_status=0
    prepare_due_run || prep_status=$?
    if [[ "$prep_status" != "0" ]]; then
      record_prep_failure "$prep_status" || true
      heartbeat_status="$prep_status"
    else
      run_heartbeat_tick || heartbeat_status=$?
    fi
  elif [[ "$due_status" != "0" ]]; then
    heartbeat_status="$due_status"
  fi
  if [[ "$heartbeat_status" -ne 0 ]]; then
    log "heartbeat failed with exit code $heartbeat_status"
  fi
  if [[ "$ONCE" == "1" ]]; then
    exit "$heartbeat_status"
    break
  fi
  sleep "$HEARTBEAT_POLL_SECONDS"
done
