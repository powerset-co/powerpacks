#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: codex-heartbeat.sh [--once|--help]

Runs a Codex heartbeat loop from a Powerpacks checkout.

Environment:
  POWERPACKS_REPO_ROOT             Powerpacks checkout path (default: /workspace/powerpacks)
  CODEX_HOME                       Writable Codex home inside the container (default: ~/.codex)
  HOST_CODEX_HOME                  Optional read-only host Codex home snapshot (default: /host-codex)
  POWERPACKS_SYNC_HOST_CODEX_HOME  Copy HOST_CODEX_HOME into CODEX_HOME on startup (default: 1)
  POWERPACKS_HEARTBEAT_SKIP_INSTALL Skip Powerpacks skill install before heartbeats (default: 0)
  HEARTBEAT_INTERVAL_SECONDS       Delay between heartbeat runs (default: 300)
  CODEX_HEARTBEAT_PROMPT           Prompt passed to `codex exec`
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

POWERPACKS_REPO_ROOT="${POWERPACKS_REPO_ROOT:-/workspace/powerpacks}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
HOST_CODEX_HOME="${HOST_CODEX_HOME:-/host-codex}"
HEARTBEAT_INTERVAL_SECONDS="${HEARTBEAT_INTERVAL_SECONDS:-300}"
POWERPACKS_SYNC_HOST_CODEX_HOME="${POWERPACKS_SYNC_HOST_CODEX_HOME:-1}"
POWERPACKS_HEARTBEAT_SKIP_INSTALL="${POWERPACKS_HEARTBEAT_SKIP_INSTALL:-0}"
CODEX_HEARTBEAT_PROMPT="${CODEX_HEARTBEAT_PROMPT:-Powerpacks heartbeat: report one terse line with the current time, the Powerpacks repo path, and whether the installed Powerpacks skills are visible locally. Do not run network calls, uploads, searches, or spend-bearing tools.}"

export CODEX_HOME

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*"
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

run_heartbeat() {
  log "starting Codex heartbeat"
  if ! command -v codex >/dev/null 2>&1; then
    log "error: codex CLI is not installed or not on PATH"
    return 127
  fi
  if [[ ! -f "$CODEX_HOME/auth.json" && -z "${OPENAI_API_KEY:-}" ]]; then
    log "warning: no $CODEX_HOME/auth.json and OPENAI_API_KEY is unset; codex may require login"
  fi
  local output_file status
  output_file="$(mktemp)"
  set +e
  codex exec "$CODEX_HEARTBEAT_PROMPT" 2>&1 | tee "$output_file"
  status=${PIPESTATUS[0]}
  set -e
  if grep -E "401 Unauthorized|Missing bearer|authentication|not authenticated|API key" "$output_file" >/dev/null 2>&1; then
    log "error: Codex heartbeat appears unauthenticated"
    rm -f "$output_file"
    return 1
  fi
  rm -f "$output_file"
  if [[ "$status" -ne 0 ]]; then
    return "$status"
  fi
  log "finished Codex heartbeat"
}

if [[ ! -d "$POWERPACKS_REPO_ROOT" ]]; then
  log "error: Powerpacks repo not found at $POWERPACKS_REPO_ROOT"
  exit 1
fi
cd "$POWERPACKS_REPO_ROOT"

sync_host_codex_home
prepare_codex_auth
run_install

while true; do
  heartbeat_status=0
  run_heartbeat || heartbeat_status=$?
  if [[ "$heartbeat_status" -ne 0 ]]; then
    log "heartbeat failed with exit code $heartbeat_status"
  fi
  if [[ "$ONCE" == "1" ]]; then
    exit "$heartbeat_status"
    break
  fi
  sleep "$HEARTBEAT_INTERVAL_SECONDS"
done
