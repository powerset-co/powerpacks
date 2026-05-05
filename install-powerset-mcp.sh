#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_INSTALL="$ROOT/packs/powerset/primitives/mcp_install/mcp_install.py"
AUTH="$ROOT/packs/powerset/primitives/auth/auth.py"

HOST="all"

usage() {
  cat >&2 <<'EOF'
usage: ./install-powerset-mcp.sh [--host all|codex|claude]

Installs the powerset-search MCP and writes a fresh bearer token into the host
MCP config. If Powerset credentials are missing or expired, this script starts
the Auth0 login flow first. Re-run it to refresh the token after it expires.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="${2:-}"
      shift 2
      ;;
    --host=*)
      HOST="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument '$1'" >&2
      usage
      exit 1
      ;;
  esac
done

case "$HOST" in
  all|codex|claude) ;;
  *)
    echo "error: --host must be one of all, codex, claude" >&2
    exit 1
    ;;
esac

if [[ ! -f "$MCP_INSTALL" ]]; then
  echo "error: missing MCP installer at $MCP_INSTALL" >&2
  exit 1
fi

if [[ ! -f "$AUTH" ]]; then
  echo "error: missing auth primitive at $AUTH" >&2
  exit 1
fi

if ! python3 "$MCP_INSTALL" token-env >/dev/null 2>&1; then
  echo "Powerset credentials are missing or expired. Starting login..." >&2
  python3 "$AUTH" login
fi

if ! python3 "$MCP_INSTALL" token-env >/dev/null; then
  echo "error: could not mint POWERPACKS_POWERSET_TOKEN after login" >&2
  exit 1
fi

install_json="$(mktemp)"
trap 'rm -f "$install_json" "$redacted_json"' EXIT
redacted_json="$(mktemp)"

python3 "$MCP_INSTALL" install --host "$HOST" >"$install_json"

python3 - "$install_json" >"$redacted_json" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text())


JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+")


def redact(value):
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = BEARER_RE.sub("Bearer <redacted>", value)
        value = JWT_RE.sub("<redacted-jwt>", value)
        if value.startswith("export POWERPACKS_POWERSET_TOKEN="):
            return "export POWERPACKS_POWERSET_TOKEN='<redacted>'"
    return value


print(json.dumps(redact(payload), indent=2, sort_keys=True))
PY

cat "$redacted_json"
echo
echo "Restart Codex so it reloads the refreshed MCP config."
