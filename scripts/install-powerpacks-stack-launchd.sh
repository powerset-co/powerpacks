#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-install}"
WITH_LOOPS="0"

usage() {
  cat <<'USAGE'
Usage: scripts/install-powerpacks-stack-launchd.sh [install|uninstall|restart|status|logs|open|help] [--with-loops]

Installs the local Powerpacks stack with macOS launchd.

Default install keeps the Powerpacks Vite console running at login/restart. Add
`--with-loops` to also install the Codex heartbeat scheduler LaunchAgent.
LaunchAgents use RunAtLoad + KeepAlive, so they start after user login and are
restarted by launchd if they exit.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    install|uninstall|restart|status|logs|open)
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

run_console() {
  "$ROOT/scripts/install-powerpacks-console-launchd.sh" "$1"
}

run_loops() {
  "$ROOT/scripts/install-codex-heartbeat-launchd.sh" "$1"
}

case "$ACTION" in
  install|restart)
    run_console "$ACTION"
    if [[ "$WITH_LOOPS" == "1" ]]; then
      run_loops "$ACTION"
    else
      echo "Loops LaunchAgent not changed (use --with-loops to manage it)."
    fi
    ;;
  uninstall)
    if [[ "$WITH_LOOPS" == "1" ]]; then
      run_loops uninstall || true
    fi
    run_console uninstall
    ;;
  status)
    run_console status
    if [[ "$WITH_LOOPS" == "1" ]]; then
      run_loops status
    fi
    ;;
  logs)
    if [[ "$WITH_LOOPS" == "1" ]]; then
      echo "Showing console logs. In another shell, run scripts/install-codex-heartbeat-launchd.sh logs for loop logs."
    fi
    run_console logs
    ;;
  open)
    run_console open
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
