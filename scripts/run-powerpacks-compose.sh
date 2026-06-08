#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${POWERPACKS_COMPOSE_FILE:-$ROOT/compose.powerpacks.yml}"
ACTION="${1:-start}"
WITH_LOOPS="0"

usage() {
  cat <<'USAGE'
Usage: scripts/run-powerpacks-compose.sh [start|stop|restart|status|logs|open|config|help] [--with-loops]

Runs the local Powerpacks stack with Docker Compose.

Default `start` launches the persistent Vite console only. Add `--with-loops` to
also launch the Codex heartbeat scheduler profile. Services use Docker Compose
`restart: unless-stopped`, so they restart when Docker restarts, subject to Docker
Desktop/daemon itself starting after machine reboot.

Environment:
  POWERPACKS_CONSOLE_PORT   Host port for the console (default: 5177)
  HOST_CODEX_HOME           Host Codex home for loop auth snapshot (default: ${CODEX_HOME:-$HOME/.codex})
  HEARTBEAT_POLL_SECONDS    Local due-check poll interval for loops (default: 300)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    start|stop|restart|status|logs|open|config)
      ACTION="$1"
      shift
      ;;
    --with-loops)
      WITH_LOOPS="1"
      shift
      ;;
    help|--help|-h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

if ! docker compose version >/dev/null 2>&1; then
  echo "error: docker compose is required" >&2
  exit 1
fi

mkdir -p "$ROOT/.powerpacks"
HOST_CODEX_HOME="${HOST_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}"
if [[ "$WITH_LOOPS" == "1" ]]; then
  if [[ ! -d "$HOST_CODEX_HOME" ]]; then
    fallback_codex_home="$ROOT/.powerpacks/compose-empty-codex-home"
    mkdir -p "$fallback_codex_home"
    echo "warning: host Codex home does not exist: $HOST_CODEX_HOME" >&2
    echo "         using empty read-only auth snapshot for no-op loop startup; due Codex runs will need host auth" >&2
    HOST_CODEX_HOME="$fallback_codex_home"
  fi
fi
export HOST_CODEX_HOME
export POWERPACKS_CONSOLE_PORT="${POWERPACKS_CONSOLE_PORT:-5177}"

compose_args=(-f "$COMPOSE_FILE")
if [[ "$WITH_LOOPS" == "1" ]]; then
  compose_args+=(--profile loops)
fi

case "$ACTION" in
  start)
    (cd "$ROOT" && docker compose "${compose_args[@]}" up -d)
    echo "Powerpacks Compose stack started."
    echo "Open: http://localhost:${POWERPACKS_CONSOLE_PORT}"
    if [[ "$WITH_LOOPS" == "1" ]]; then
      echo "Loops profile: enabled"
    else
      echo "Loops profile: disabled (use --with-loops to start it)"
    fi
    ;;
  stop)
    (cd "$ROOT" && docker compose "${compose_args[@]}" down)
    ;;
  restart)
    loop_args=()
    if [[ "$WITH_LOOPS" == "1" ]]; then
      loop_args+=(--with-loops)
    fi
    "$0" stop "${loop_args[@]}" || true
    "$0" start "${loop_args[@]}"
    ;;
  status)
    (cd "$ROOT" && docker compose "${compose_args[@]}" ps)
    ;;
  logs)
    (cd "$ROOT" && docker compose "${compose_args[@]}" logs -f --tail=200)
    ;;
  open)
    echo "Open: http://localhost:${POWERPACKS_CONSOLE_PORT}"
    if command -v open >/dev/null 2>&1; then
      open "http://localhost:${POWERPACKS_CONSOLE_PORT}" >/dev/null 2>&1 || true
    fi
    ;;
  config)
    (cd "$ROOT" && docker compose "${compose_args[@]}" config)
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
