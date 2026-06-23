#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_PATH="$ROOT_DIR/scripts/$(basename "${BASH_SOURCE[0]}")"
APP_DIR="$ROOT_DIR/app"
HOST="${HOST:-localhost}"
PORT="${PORT:-5177}"
ACTION="start"
APP_PATH="${APP_PATH:-/}"
OPEN_BROWSER="${OPEN_BROWSER:-0}"

is_powerpacks_root() {
  local candidate="$1"
  [[ -d "$candidate/packs" && -f "$candidate/pyproject.toml" ]]
}

is_codex_root() {
  local candidate="$1"
  [[ "$candidate" == *"/.codex/"* || "$candidate" == "$HOME/.codex" || "$candidate" == "$HOME/.codex/"* ]]
}

has_powerpacks_state() {
  local candidate="$1"
  [[ -f "$candidate/.powerpacks/setup/setup-run.json" ]] \
    || [[ -f "$candidate/.powerpacks/ingestion/accounts.json" ]] \
    || [[ -f "$candidate/.powerpacks/network-import/merged/people.csv" ]] \
    || [[ -f "$candidate/.powerpacks/messages/research_review.csv" ]] \
    || [[ -f "$candidate/.powerpacks/search-index/local-search.duckdb" ]]
}

resolve_repo_root() {
  if [[ -n "${POWERPACKS_REPO_ROOT:-}" ]]; then
    printf "%s\n" "$POWERPACKS_REPO_ROOT"
    return
  fi
  if is_powerpacks_root "$PWD" && ! is_codex_root "$PWD"; then
    printf "%s\n" "$PWD"
    return
  fi

  local preferred=("$HOME/powerpacks" "$HOME/workspace/powerpacks" "$ROOT_DIR")
  local legacy=("$HOME/.codex/powerpacks")
  local candidate
  for candidate in "${preferred[@]}"; do
    if is_powerpacks_root "$candidate" && ! is_codex_root "$candidate" && has_powerpacks_state "$candidate"; then
      printf "%s\n" "$candidate"
      return
    fi
  done
  for candidate in "${preferred[@]}"; do
    if is_powerpacks_root "$candidate" && ! is_codex_root "$candidate"; then
      printf "%s\n" "$candidate"
      return
    fi
  done
  for candidate in "${legacy[@]}"; do
    if is_powerpacks_root "$candidate"; then
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

REPO_SLUG="$(basename "$POWERPACKS_REPO_ROOT" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"
LAUNCHD_LABEL="co.powerset.powerpacks-console.$REPO_SLUG"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
LAUNCHD_LOG="$STATE_DIR/powerpacks-console.launchd.log"
LAUNCHD_DOMAIN="gui/$(id -u)"

mkdir -p "$STATE_DIR"

usage() {
  echo "Usage: $0 [start|stop|status|restart|run|daemon-install|daemon-uninstall|daemon-status] [--path /route] [--open|--no-open]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    start|stop|status|restart|run|daemon-install|daemon-uninstall|daemon-status)
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

console_pid_on_port() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1
}

is_running() {
  # Primary signal: the pid file we wrote on the last `start`.
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    return 0
  fi
  # Fallback: something is already serving our port (daemonized via launchd, or
  # started outside this script) but the pid file is stale/absent. Recognize it
  # as running and heal the pid file so reuse, status, stop, and restart all work
  # without anyone hand-editing files.
  local port_pid
  port_pid="$(console_pid_on_port)"
  if [[ -n "$port_pid" ]]; then
    printf "%s" "$port_pid" > "$PID_FILE"
    return 0
  fi
  return 1
}

console_url() {
  local base=""
  # Prefer the canonical port when it is actually serving. This covers reuse of a
  # daemonized/running console and avoids stale ports lingering in the log file
  # (an earlier duplicate could otherwise send the user to the wrong page).
  if [[ -n "$(console_pid_on_port)" ]]; then
    base="http://localhost:$PORT"
  fi
  if [[ -z "$base" ]]; then
    base="$(grep -Eo 'http://localhost:[0-9]+' "$LOG_FILE" 2>/dev/null | tail -1 || true)"
  fi
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
  run)
    # Foreground mode for launchd supervision: no backgrounding, no pid file.
    echo "Running Powerpacks Console in foreground on http://$HOST:$PORT"
    echo "Repo: $POWERPACKS_REPO_ROOT"
    export POWERPACKS_REPO_ROOT
    cd "$APP_DIR"
    exec npm run dev -- --host "$HOST" --port "$PORT" --strictPort
    ;;
  daemon-install)
    NPM_BIN="$(command -v npm || true)"
    if [[ -z "$NPM_BIN" ]]; then
      echo "npm is required to install the console daemon." >&2
      exit 1
    fi
    LAUNCHD_PATH="$(dirname "$NPM_BIN"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$LAUNCHD_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LAUNCHD_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$SCRIPT_PATH</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$POWERPACKS_REPO_ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>POWERPACKS_REPO_ROOT</key>
    <string>$POWERPACKS_REPO_ROOT</string>
    <key>HOST</key>
    <string>$HOST</string>
    <key>PORT</key>
    <string>$PORT</string>
    <key>PATH</key>
    <string>$LAUNCHD_PATH</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LAUNCHD_LOG</string>
  <key>StandardErrorPath</key>
  <string>$LAUNCHD_LOG</string>
</dict>
</plist>
PLIST
    if launchctl print "$LAUNCHD_DOMAIN/$LAUNCHD_LABEL" >/dev/null 2>&1; then
      launchctl bootout "$LAUNCHD_DOMAIN/$LAUNCHD_LABEL"
    fi
    launchctl bootstrap "$LAUNCHD_DOMAIN" "$LAUNCHD_PLIST"
    echo "Installed LaunchAgent $LAUNCHD_LABEL"
    echo "Plist: $LAUNCHD_PLIST"
    echo "Serving: http://$HOST:$PORT"
    echo "Log: $LAUNCHD_LOG"
    ;;
  daemon-uninstall)
    if launchctl print "$LAUNCHD_DOMAIN/$LAUNCHD_LABEL" >/dev/null 2>&1; then
      launchctl bootout "$LAUNCHD_DOMAIN/$LAUNCHD_LABEL"
      echo "Booted out $LAUNCHD_LABEL."
    else
      echo "$LAUNCHD_LABEL is not loaded."
    fi
    rm -f "$LAUNCHD_PLIST"
    echo "Removed $LAUNCHD_PLIST"
    ;;
  daemon-status)
    PLIST_PORT="$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:PORT' "$LAUNCHD_PLIST" 2>/dev/null || true)"
    if [[ -n "$PLIST_PORT" ]]; then
      PORT="$PLIST_PORT"
    fi
    echo "Label: $LAUNCHD_LABEL"
    echo "Plist: $LAUNCHD_PLIST"
    if launchctl print "$LAUNCHD_DOMAIN/$LAUNCHD_LABEL" >/dev/null 2>&1; then
      launchctl print "$LAUNCHD_DOMAIN/$LAUNCHD_LABEL" | grep -E "^\s*(state|pid) = " || true
    else
      echo "Not loaded in $LAUNCHD_DOMAIN."
      exit 1
    fi
    echo "Port $PORT listeners:"
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN || echo "  (nothing listening on $PORT)"
    echo "Recent log ($LAUNCHD_LOG):"
    tail -20 "$LAUNCHD_LOG" 2>/dev/null || echo "  (no log yet)"
    ;;
  *)
    usage
    exit 2
    ;;
esac
