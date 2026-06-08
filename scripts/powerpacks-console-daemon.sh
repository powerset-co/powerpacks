#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR/app"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5177}"

is_powerpacks_root() {
  local candidate="$1"
  [[ -d "$candidate/packs" && -f "$candidate/pyproject.toml" ]]
}

is_codex_root() {
  local candidate="$1"
  [[ "$candidate" == *"/.codex/"* || "$candidate" == "$HOME/.codex" || "$candidate" == "$HOME/.codex/"* ]]
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
  for candidate in "$HOME/powerpacks" "$HOME/workspace/powerpacks" "$ROOT_DIR"; do
    if is_powerpacks_root "$candidate" && ! is_codex_root "$candidate"; then
      printf "%s\n" "$candidate"
      return
    fi
  done
  printf "%s\n" "$ROOT_DIR"
}

POWERPACKS_REPO_ROOT="$(resolve_repo_root)"
export POWERPACKS_REPO_ROOT

echo "Starting persistent Powerpacks Console"
echo "Repo: $POWERPACKS_REPO_ROOT"
echo "URL: http://$HOST:$PORT"

"$ROOT_DIR/bin/setup-app" --repair-only --no-build
cd "$APP_DIR"
exec npm run dev -- --host "$HOST" --port "$PORT"
