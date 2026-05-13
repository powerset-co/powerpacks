#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR/app"
STATE_DIR="$ROOT_DIR/.powerpacks/servers"
PID_FILE="$STATE_DIR/powerpacks-console.pid"
LOG_FILE="$STATE_DIR/powerpacks-console.log"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-5177}"
POWERPACKS_REPO_ROOT="${POWERPACKS_REPO_ROOT:-$ROOT_DIR}"
ACTION="${1:-start}"

mkdir -p "$STATE_DIR"

is_running() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

case "$ACTION" in
  start)
    if is_running; then
      echo "Powerpacks Console already running (pid $(cat "$PID_FILE"))."
      echo "Log: $LOG_FILE"
      exit 0
    fi

    if [[ ! -d "$APP_DIR/node_modules" ]]; then
      echo "Installing Powerpacks Console dependencies..."
      (cd "$APP_DIR" && npm install)
    fi

    echo "Starting Powerpacks Console on http://$HOST:$PORT"
    (
      cd "$APP_DIR"
      POWERPACKS_REPO_ROOT="$POWERPACKS_REPO_ROOT" nohup npm run dev -- --host "$HOST" --port "$PORT" > "$LOG_FILE" 2>&1 &
      echo $! > "$PID_FILE"
    )
    sleep 2
    if is_running; then
      echo "Powerpacks Console running (pid $(cat "$PID_FILE"))."
      echo "Log: $LOG_FILE"
      grep -E "Local:|Network:" "$LOG_FILE" || true
    else
      echo "Powerpacks Console failed to start. Log: $LOG_FILE" >&2
      tail -80 "$LOG_FILE" >&2 || true
      exit 1
    fi
    ;;
  stop)
    if is_running; then
      kill "$(cat "$PID_FILE")" 2>/dev/null || true
      rm -f "$PID_FILE"
      echo "Stopped Powerpacks Console."
    else
      rm -f "$PID_FILE"
      echo "Powerpacks Console is not running."
    fi
    ;;
  status)
    if is_running; then
      echo "Powerpacks Console running (pid $(cat "$PID_FILE"))."
      echo "Log: $LOG_FILE"
      grep -E "Local:|Network:" "$LOG_FILE" || true
    else
      echo "Powerpacks Console is not running."
      exit 1
    fi
    ;;
  restart)
    "$0" stop || true
    "$0" start
    ;;
  *)
    echo "Usage: $0 [start|stop|status|restart]" >&2
    exit 2
    ;;
esac
