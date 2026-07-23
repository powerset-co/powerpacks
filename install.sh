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
  claude-code|claude)
    shift
    exec "$ROOT/adapters/claude-code/install.sh" "$@"
    ;;
  pi)
    shift
    exec "$ROOT/adapters/pi/install.sh" "$@"
    ;;
  "")
    echo "usage: ./install.sh codex [skills-dir]" >&2
    echo "       ./install.sh claude-code [skills-dir]" >&2
    echo "       ./install.sh pi [skills-dir]" >&2
    echo "       ./install.sh nanoclaw /path/to/nanoclaw" >&2
    exit 1
    ;;
  *)
    echo "error: unknown adapter '$TARGET'" >&2
    echo "usage: ./install.sh codex [skills-dir]" >&2
    echo "       ./install.sh claude-code [skills-dir]" >&2
    echo "       ./install.sh pi [skills-dir]" >&2
    echo "       ./install.sh nanoclaw /path/to/nanoclaw" >&2
    exit 1
    ;;
esac
