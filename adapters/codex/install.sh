#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
SKILLS_DIR="${1:-$CODEX_HOME/skills}"

mkdir -p "$SKILLS_DIR"
rm -rf "$SKILLS_DIR/import-messages"

copy_powerpacks_bundle() {
  local dest="$1"
  cp -R "$REPO_ROOT/docs" "$dest/powerpacks/docs"
  cp -R "$REPO_ROOT/primitives" "$dest/powerpacks/primitives"
  cp -R "$REPO_ROOT/schemas" "$dest/powerpacks/schemas"
  cp -R "$REPO_ROOT/contracts" "$dest/powerpacks/contracts"
  cp -R "$REPO_ROOT/tasks" "$dest/powerpacks/tasks"
  cp -R "$REPO_ROOT/evals" "$dest/powerpacks/evals"
  if [[ -d "$REPO_ROOT/packs" ]]; then
    cp -R "$REPO_ROOT/packs" "$dest/powerpacks/packs"

    # Keep only the top-level skill entrypoint; avoid nested skill duplication
    # from copied packs during discovery.
    find "$dest/powerpacks/packs" -type f -path "*/SKILL.md" -delete
  fi
}

install_skill() {
  local skill_name="$1"
  local source_skill="$2"
  local dest="$SKILLS_DIR/$skill_name"
  rm -rf "$dest"
  mkdir -p "$dest/powerpacks"

  cp -R "$source_skill" "$dest/SKILL.md"
  copy_powerpacks_bundle "$dest"

  cat > "$dest/powerpacks/README.codex-install.md" <<EOF
# Codex Powerpacks Bundle

This directory is copied by:

\`\`\`bash
$REPO_ROOT/adapters/codex/install.sh
\`\`\`

The installed \`$skill_name\` skill resolves \`powerpacks/...\` references
relative to this skill directory.
EOF
}

install_skill search-network "$REPO_ROOT/skills/search-network/SKILL.md"
install_skill extract-search-query "$REPO_ROOT/skills/extract-search-query/SKILL.md"
install_skill search-company "$REPO_ROOT/skills/search-company/SKILL.md"
install_skill import-imessage "$REPO_ROOT/packs/messages/skills/import-imessage/SKILL.md"
install_skill import-whatsapp "$REPO_ROOT/packs/messages/skills/import-whatsapp/SKILL.md"
install_skill import-contacts-review "$REPO_ROOT/packs/messages/skills/import-contacts-review/SKILL.md"

echo "installed Powerpacks skills into $SKILLS_DIR: search-network extract-search-query search-company import-imessage import-whatsapp import-contacts-review"
echo "restart Codex to pick up the skill list"
