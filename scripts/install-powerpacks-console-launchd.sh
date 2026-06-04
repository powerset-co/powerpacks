#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-install}"
LABEL="${POWERPACKS_CONSOLE_LABEL:-com.powerset.powerpacks.console}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="${POWERPACKS_CONSOLE_LOG_DIR:-$HOME/Library/Logs/Powerpacks}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5177}"
POWERPACKS_REPO_ROOT="${POWERPACKS_REPO_ROOT:-$ROOT}"
POWERPACKS_CONSOLE_URL="${POWERPACKS_CONSOLE_URL:-http://localhost:$PORT}"

usage() {
  cat <<'USAGE'
Usage: scripts/install-powerpacks-console-launchd.sh [install|uninstall|restart|status|logs|open|help]

Installs the existing Powerpacks Vite console as a persistent macOS launchd
LaunchAgent. This is the recommended local control plane for Codex loop tasks:
the browser UI can stay up while Docker/launchd workers do scheduled work.

Environment:
  PORT                         Console port (default: 5177)
  HOST                         Bind host (default: 127.0.0.1)
  POWERPACKS_REPO_ROOT         Repo whose .powerpacks state the console should show
  POWERPACKS_CONSOLE_URL       URL to print/open (default: http://localhost:$PORT)
USAGE
}

if [[ "$ACTION" == "help" || "$ACTION" == "--help" || "$ACTION" == "-h" ]]; then
  usage
  exit 0
fi

require_macos() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "error: launchd install is only supported on macOS" >&2
    exit 1
  fi
}

write_plist() {
  mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"
  python3 - "$PLIST" "$LABEL" "$ROOT" "$POWERPACKS_REPO_ROOT" "$HOST" "$PORT" "$LOG_DIR" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path, label, root, repo_root, host, port, log_dir = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": [f"{root}/scripts/powerpacks-console-daemon.sh"],
    "EnvironmentVariables": {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "POWERPACKS_REPO_ROOT": repo_root,
        "HOST": host,
        "PORT": str(port),
    },
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": f"{log_dir}/powerpacks-console.out.log",
    "StandardErrorPath": f"{log_dir}/powerpacks-console.err.log",
    "WorkingDirectory": root,
}
with Path(plist_path).open("wb") as fh:
    plistlib.dump(payload, fh)
PY
}

bootstrap() {
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
  launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
}

bootout() {
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
}

case "$ACTION" in
  install)
    require_macos
    write_plist
    bootout
    bootstrap
    echo "installed $LABEL"
    echo "Open: $POWERPACKS_CONSOLE_URL"
    ;;
  uninstall)
    require_macos
    bootout
    rm -f "$PLIST"
    echo "uninstalled $LABEL"
    ;;
  restart)
    require_macos
    bootout
    write_plist
    bootstrap
    echo "restarted $LABEL"
    echo "Open: $POWERPACKS_CONSOLE_URL"
    ;;
  status)
    require_macos
    launchctl print "gui/$(id -u)/$LABEL"
    ;;
  logs)
    tail -f "$LOG_DIR/powerpacks-console.out.log" "$LOG_DIR/powerpacks-console.err.log"
    ;;
  open)
    echo "Open: $POWERPACKS_CONSOLE_URL"
    if command -v open >/dev/null 2>&1; then
      open "$POWERPACKS_CONSOLE_URL" >/dev/null 2>&1 || true
    fi
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
