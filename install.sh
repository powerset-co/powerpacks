#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-}"

case "$TARGET" in
  nanoclaw)
    shift
    exec "$ROOT/adapters/nanoclaw/install.sh" "$@"
    ;;
  codex)
    shift
    exec "$ROOT/adapters/codex/install.sh" "$@"
    ;;
  claude-code)
    echo "error: $TARGET adapter is not implemented yet" >&2
    exit 2
    ;;
  "")
    echo "usage: ./install.sh nanoclaw /path/to/nanoclaw | codex [skills-dir]" >&2
    exit 1
    ;;
  *)
    echo "error: unknown adapter '$TARGET'" >&2
    echo "usage: ./install.sh nanoclaw /path/to/nanoclaw | codex [skills-dir]" >&2
    exit 1
    ;;
esac
