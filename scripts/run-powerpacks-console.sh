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
ACTION="start"
APP_PATH="${APP_PATH:-/}"
OPEN_BROWSER="${OPEN_BROWSER:-0}"

mkdir -p "$STATE_DIR"

usage() {
  echo "Usage: $0 [start|stop|status|restart] [--path /route] [--open|--no-open]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    start|stop|status|restart)
      ACTION="$1"
      shift
      ;;
    --path)
      APP_PATH="${2:-}"
      if [[ -z "$APP_PATH" ]]; then
        usage
        exit 2
      fi
      shift 2
      ;;
    --open)
      OPEN_BROWSER="1"
      shift
      ;;
    --no-open)
      OPEN_BROWSER="0"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ "$APP_PATH" != /* ]]; then
  APP_PATH="/$APP_PATH"
fi

is_running() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

console_url() {
  local base
  base="$(grep -Eo 'http://localhost:[0-9]+' "$LOG_FILE" 2>/dev/null | tail -1 || true)"
  if [[ -z "$base" ]]; then
    base="$(grep -Eo 'http://127\.0\.0\.1:[0-9]+' "$LOG_FILE" 2>/dev/null | tail -1 || true)"
  fi
  if [[ -z "$base" ]]; then
    base="http://localhost:$PORT"
  fi
  printf "%s%s\n" "$base" "$APP_PATH"
}

print_or_open_url() {
  local url
  url="$(console_url)"
  echo "Open: $url"
  if [[ "$OPEN_BROWSER" == "1" ]]; then
    open "$url" >/dev/null 2>&1 || true
  fi
}

case "$ACTION" in
  start)
    if is_running; then
      echo "Powerpacks Console already running (pid $(cat "$PID_FILE"))."
      echo "Log: $LOG_FILE"
      print_or_open_url
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
      print_or_open_url
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
      print_or_open_url
    else
      echo "Powerpacks Console is not running."
      exit 1
    fi
    ;;
  restart)
    "$0" stop || true
    if [[ "$OPEN_BROWSER" == "1" ]]; then
      "$0" start --path "$APP_PATH" --open
    else
      "$0" start --path "$APP_PATH"
    fi
    ;;
  *)
    usage
    exit 2
    ;;
esac
