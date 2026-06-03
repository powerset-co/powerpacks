#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR/app"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-5177}"
ACTION="start"
APP_PATH="${APP_PATH:-/}"
OPEN_BROWSER="${OPEN_BROWSER:-0}"

is_powerpacks_root() {
  local candidate="$1"
  [[ -d "$candidate/packs" && -f "$candidate/pyproject.toml" ]]
}

has_powerpacks_state() {
  local candidate="$1"
  [[ -f "$candidate/.powerpacks/setup/setup-run.json" ]] \
    || [[ -f "$candidate/.powerpacks/ingestion/accounts.json" ]] \
    || [[ -f "$candidate/.powerpacks/network-import/merged/people.csv" ]] \
    || [[ -f "$candidate/.powerpacks/messages/research_review.csv" ]] \
    || [[ -f "$candidate/.powerpacks/search-index/local-search.duckdb" ]] \
    || [[ -f "$candidate/.powerpacks/operator-bootstrap/restore-manifest.json" ]]
}

resolve_repo_root() {
  if [[ -n "${POWERPACKS_REPO_ROOT:-}" ]]; then
    printf "%s\n" "$POWERPACKS_REPO_ROOT"
    return
  fi
  if is_powerpacks_root "$PWD"; then
    printf "%s\n" "$PWD"
    return
  fi

  local candidates=("$ROOT_DIR" "$HOME/.codex/powerpacks" "$HOME/workspace/powerpacks" "$HOME/powerpacks")
  local candidate
  for candidate in "${candidates[@]}"; do
    if is_powerpacks_root "$candidate" && has_powerpacks_state "$candidate"; then
      printf "%s\n" "$candidate"
      return
    fi
  done
  printf "%s\n" "$ROOT_DIR"
}

POWERPACKS_REPO_ROOT="$(resolve_repo_root)"
STATE_DIR="$POWERPACKS_REPO_ROOT/.powerpacks/servers"
PID_FILE="$STATE_DIR/powerpacks-console.pid"
LOG_FILE="$STATE_DIR/powerpacks-console.log"

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
      echo "Repo: $POWERPACKS_REPO_ROOT"
      echo "Log: $LOG_FILE"
      print_or_open_url
      exit 0
    fi

    "$ROOT_DIR/bin/setup-app" --repair-only --no-build

    echo "Starting Powerpacks Console on http://$HOST:$PORT"
    echo "Repo: $POWERPACKS_REPO_ROOT"
    PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
    if [[ -z "$PYTHON_BIN" ]]; then
      echo "python3 is required to launch the console in the background." >&2
      exit 1
    fi
    "$PYTHON_BIN" - "$APP_DIR" "$LOG_FILE" "$PID_FILE" "$POWERPACKS_REPO_ROOT" "$HOST" "$PORT" <<'PY'
import os
import subprocess
import sys

app_dir, log_file, pid_file, repo_root, host, port = sys.argv[1:]
env = os.environ.copy()
env["POWERPACKS_REPO_ROOT"] = repo_root
log = open(log_file, "ab", buffering=0)
proc = subprocess.Popen(
    ["npm", "run", "dev", "--", "--host", host, "--port", port],
    cwd=app_dir,
    env=env,
    stdin=subprocess.DEVNULL,
    stdout=log,
    stderr=subprocess.STDOUT,
    close_fds=True,
    start_new_session=True,
)
with open(pid_file, "w", encoding="utf-8") as handle:
    handle.write(str(proc.pid))
PY
    sleep 2
    if is_running; then
      echo "Powerpacks Console running (pid $(cat "$PID_FILE"))."
      echo "Repo: $POWERPACKS_REPO_ROOT"
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
      echo "Repo: $POWERPACKS_REPO_ROOT"
      echo "Log: $LOG_FILE"
      grep -E "Local:|Network:" "$LOG_FILE" || true
      print_or_open_url
    else
      echo "Repo: $POWERPACKS_REPO_ROOT"
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
