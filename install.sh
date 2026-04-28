#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-}"

if [[ -z "$TARGET" ]]; then
  echo "usage: ./install.sh /path/to/nanoclaw" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$(cd "$TARGET" && pwd)"

if [[ ! -f "$TARGET/package.json" ]]; then
  echo "error: target does not look like a repo: $TARGET" >&2
  exit 1
fi

if [[ ! -f "$TARGET/nanoclaw.sh" ]]; then
  echo "error: target does not look like a NanoClaw checkout: $TARGET" >&2
  exit 1
fi

mkdir -p "$TARGET/.claude/skills"
mkdir -p "$TARGET/powerpacks"

rm -rf "$TARGET/.claude/skills/add-query-decomposition"
rm -rf "$TARGET/.claude/skills/add-role-search"
rm -rf "$TARGET/.claude/skills/add-company-search"
rm -rf "$TARGET/.claude/skills/add-turbopuffer-schema-guard"
rm -rf "$TARGET/.claude/skills/add-postgres-hydration"
cp -R "$SCRIPT_DIR/skills/." "$TARGET/.claude/skills/"

rm -rf "$TARGET/powerpacks/primitives"
rm -rf "$TARGET/powerpacks/mcp"
rm -rf "$TARGET/powerpacks/templates"
rm -rf "$TARGET/powerpacks/docs"
rm -rf "$TARGET/powerpacks/schemas"
cp -R "$SCRIPT_DIR/primitives" "$TARGET/powerpacks/primitives"
cp -R "$SCRIPT_DIR/mcp" "$TARGET/powerpacks/mcp"
cp -R "$SCRIPT_DIR/templates" "$TARGET/powerpacks/templates"
cp -R "$SCRIPT_DIR/docs" "$TARGET/powerpacks/docs"
cp -R "$SCRIPT_DIR/schemas" "$TARGET/powerpacks/schemas"

cat > "$TARGET/powerpacks/install-manifest.json" <<EOF
{
  "installed_from": "$SCRIPT_DIR",
  "installed_at_utc": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}
EOF

echo "powerpacks installed into $TARGET"
