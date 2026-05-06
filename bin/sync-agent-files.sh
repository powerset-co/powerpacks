#!/usr/bin/env bash
# Make every agent harness's bootup file a symlink to AGENTS.md so we have
# one source of truth.
#
# Adds new flavors here as harnesses appear. Idempotent: if the symlink
# already points to AGENTS.md it does nothing; if a regular file exists at
# the target, it warns and skips so we don't clobber human-edited content.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="AGENTS.md"

# Files that should mirror AGENTS.md. Add to this list when a new harness
# starts looking at a new filename.
TARGETS=(
  "CLAUDE.md"          # Claude Code
  ".cursorrules"       # Cursor (legacy single-file)
  ".github/copilot-instructions.md"  # GitHub Copilot
)

cd "$ROOT"

if [[ ! -f "$SOURCE" ]]; then
  echo "error: $SOURCE missing at repo root" >&2
  exit 1
fi

for target in "${TARGETS[@]}"; do
  parent="$(dirname "$target")"
  [[ "$parent" == "." ]] || mkdir -p "$parent"

  # Compute the relative source path so the link works on every checkout.
  rel="$(python3 -c "import os,sys; print(os.path.relpath('$SOURCE', start='$parent'))")"

  if [[ -L "$target" ]]; then
    current="$(readlink "$target")"
    if [[ "$current" == "$rel" ]]; then
      echo "ok    $target -> $rel"
      continue
    fi
    echo "fix   $target was -> $current, retargeting to $rel"
    rm -f "$target"
    ln -s "$rel" "$target"
    continue
  fi

  if [[ -e "$target" ]]; then
    echo "skip  $target exists as regular file. Move/delete it first if you want it linked."
    continue
  fi

  ln -s "$rel" "$target"
  echo "link  $target -> $rel"
done

echo
echo "done. Source of truth: $SOURCE"
