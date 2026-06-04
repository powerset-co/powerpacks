#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-print}"
HOSTNAMES="${POWERPACKS_CONSOLE_HOSTNAMES:-powerpacks.test powerpacks}"
IP="${POWERPACKS_CONSOLE_HOST_IP:-127.0.0.1}"
PORT="${PORT:-5177}"
MARKER_BEGIN="# BEGIN Powerpacks Console hosts"
MARKER_END="# END Powerpacks Console hosts"

usage() {
  cat <<'USAGE'
Usage: scripts/install-powerpacks-console-hostname.sh [print|install|uninstall|help]

Adds/removes a local /etc/hosts block for the Powerpacks Console hostnames.
This requires sudo because /etc/hosts is system-owned.

Important: /etc/hosts maps names to IPs only; it cannot map ports. After install,
use http://powerpacks.test:5177 or http://powerpacks:5177. A no-port URL like
http://powerpacks requires a separate port-80 reverse proxy or a server bound to
port 80, which this script intentionally does not install.

Environment:
  POWERPACKS_CONSOLE_HOSTNAMES   Space-separated aliases (default: powerpacks.test powerpacks)
  POWERPACKS_CONSOLE_HOST_IP     IP address (default: 127.0.0.1)
  PORT                           Console port to show in output (default: 5177)
USAGE
}

hosts_block() {
  printf '%s\n' "$MARKER_BEGIN"
  printf '%s %s\n' "$IP" "$HOSTNAMES"
  printf '%s\n' "$MARKER_END"
}

print_urls() {
  local name
  for name in $HOSTNAMES; do
    echo "Open: http://$name:$PORT"
  done
}

remove_block() {
  sudo python3 - "$MARKER_BEGIN" "$MARKER_END" <<'PY'
import sys
from pathlib import Path
begin, end = sys.argv[1:]
path = Path('/etc/hosts')
text = path.read_text()
lines = text.splitlines()
out = []
skip = False
for line in lines:
    if line.strip() == begin:
        skip = True
        continue
    if line.strip() == end:
        skip = False
        continue
    if not skip:
        out.append(line)
path.write_text('\n'.join(out).rstrip() + '\n')
PY
}

case "$ACTION" in
  help|--help|-h)
    usage
    ;;
  print)
    echo "Would add this /etc/hosts block:"
    hosts_block
    echo "Note: /etc/hosts maps names to IPs only; it cannot map ports."
    echo "A no-port URL like http://powerpacks requires a separate port-80 reverse proxy or privileged port binding."
    print_urls
    ;;
  install)
    remove_block
    hosts_block | sudo tee -a /etc/hosts >/dev/null
    echo "installed Powerpacks Console hostnames"
    print_urls
    ;;
  uninstall)
    remove_block
    echo "removed Powerpacks Console hostnames"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
