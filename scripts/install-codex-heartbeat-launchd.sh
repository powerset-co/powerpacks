#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-install}"
LABEL="${POWERPACKS_CODEX_HEARTBEAT_LABEL:-com.powerset.powerpacks.codex-heartbeat}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CONFIG_PATH="${POWERPACKS_HEARTBEAT_CONFIG:-$ROOT/.powerpacks/codex-heartbeat.json}"
STATE_DIR="${POWERPACKS_HEARTBEAT_STATE_DIR:-$HOME/Library/Application Support/Powerpacks}"
STATE_PATH="${POWERPACKS_HEARTBEAT_STATE:-$STATE_DIR/codex-heartbeat-state.json}"
LOG_DIR="${POWERPACKS_HEARTBEAT_LOG_DIR:-$HOME/Library/Logs/Powerpacks}"
POLL_SECONDS="${HEARTBEAT_POLL_SECONDS:-${HEARTBEAT_INTERVAL_SECONDS:-300}}"

usage() {
  cat <<'USAGE'
Usage: scripts/install-codex-heartbeat-launchd.sh [install|uninstall|restart|status|logs|init-config|help]

Installs a macOS launchd LaunchAgent for the Powerpacks Codex heartbeat. Use
this when you want the local shell Codex OAuth login and do not want Docker.

The LaunchAgent runs scripts/codex-heartbeat.sh continuously. Each wakeup is a
local config/state check only; Codex is invoked only when the JSON config says a
run is due.

Environment:
  POWERPACKS_HEARTBEAT_CONFIG       Config path (default: <repo>/.powerpacks/codex-heartbeat.json)
  POWERPACKS_HEARTBEAT_STATE        State path (default: ~/Library/Application Support/Powerpacks/codex-heartbeat-state.json)
  HEARTBEAT_POLL_SECONDS            Local due-check poll interval (default: 300)
  POWERPACKS_HEARTBEAT_SKIP_INSTALL Skip Powerpacks install on agent startup when set to 1
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

init_config() {
  mkdir -p "$(dirname "$CONFIG_PATH")"
  python3 "$ROOT/scripts/codex-heartbeat-runner.py" --config "$CONFIG_PATH" --init-config
}

write_plist() {
  mkdir -p "$(dirname "$PLIST")" "$STATE_DIR" "$LOG_DIR"
  init_config
  python3 - "$PLIST" "$LABEL" "$ROOT" "$CONFIG_PATH" "$STATE_PATH" "$POLL_SECONDS" "$HOME" "$LOG_DIR" "${POWERPACKS_HEARTBEAT_SKIP_INSTALL:-0}" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path, label, root, config_path, state_path, poll_seconds, home, log_dir, skip_install = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": [f"{root}/scripts/codex-heartbeat.sh"],
    "EnvironmentVariables": {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "POWERPACKS_REPO_ROOT": root,
        "POWERPACKS_HEARTBEAT_CONFIG": config_path,
        "POWERPACKS_HEARTBEAT_STATE": state_path,
        "HEARTBEAT_POLL_SECONDS": str(poll_seconds),
        "CODEX_HOME": f"{home}/.codex",
        "HOST_CODEX_HOME": f"{home}/.codex",
        "POWERPACKS_SYNC_HOST_CODEX_HOME": "0",
        "POWERPACKS_SKIP_AGENT_BOOTSTRAP": "1",
        "POWERPACKS_HEARTBEAT_SKIP_INSTALL": str(skip_install),
    },
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": f"{log_dir}/codex-heartbeat.out.log",
    "StandardErrorPath": f"{log_dir}/codex-heartbeat.err.log",
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
  init-config)
    init_config
    ;;
  install)
    require_macos
    write_plist
    bootout
    bootstrap
    echo "installed $LABEL"
    echo "config: $CONFIG_PATH"
    echo "logs: $LOG_DIR/codex-heartbeat.out.log"
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
    ;;
  status)
    require_macos
    launchctl print "gui/$(id -u)/$LABEL"
    ;;
  logs)
    tail -f "$LOG_DIR/codex-heartbeat.out.log" "$LOG_DIR/codex-heartbeat.err.log"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
