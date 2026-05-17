#!/usr/bin/env bash
# Install Powerpacks skills into a Claude Code skills directory.
#
# By default installs to user-level skills at `~/.claude/skills/`. Pass an
# explicit target directory to install project-level instead, e.g.
# `./install.sh /path/to/repo/.claude/skills`.
#
# Each skill is installed as `<dest>/<skill-name>/SKILL.md` plus a bundled
# `powerpacks/` directory next to it that holds primitives, schemas, contracts,
# tasks, evals, and packs the skill's commands resolve relative to.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEFAULT_SKILLS_DIR="$HOME/.claude/skills"
SKILLS_DIR="${1:-$DEFAULT_SKILLS_DIR}"

mkdir -p "$SKILLS_DIR"
rm -rf "$SKILLS_DIR/import-messages" \
  "$SKILLS_DIR/import-imessage" \
  "$SKILLS_DIR/import-whatsapp" \
  "$SKILLS_DIR/import-contacts-review"
"$REPO_ROOT/bin/setup-python"

copy_powerpacks_bundle() {
  local dest="$1"
  cp "$REPO_ROOT/pyproject.toml" "$dest/powerpacks/pyproject.toml"
  if [[ -f "$REPO_ROOT/uv.lock" ]]; then
    cp "$REPO_ROOT/uv.lock" "$dest/powerpacks/uv.lock"
  fi
  # Cross-pack docs + host-install templates (no top-level primitives/skills/
  # schemas anymore — every domain lives in packs/).
  cp -R "$REPO_ROOT/docs" "$dest/powerpacks/docs"
  cp -R "$REPO_ROOT/templates" "$dest/powerpacks/templates"
  # Domain packs (powerset, search, messages, sales-nav, ...) carry their own
  # primitives, schemas, contracts, tasks, evals, and docs.
  cp -R "$REPO_ROOT/packs" "$dest/powerpacks/packs"
  # Keep only the top-level skill entrypoint; avoid nested skill duplication
  # from copied packs during discovery.
  find "$dest/powerpacks/packs" -type f -path "*/SKILL.md" -delete
}

install_skill() {
  local skill_name="$1"
  local source_skill="$2"
  local dest="$SKILLS_DIR/$skill_name"
  rm -rf "$dest"
  mkdir -p "$dest/powerpacks"

  cp -R "$source_skill" "$dest/SKILL.md"
  copy_powerpacks_bundle "$dest"

  cat > "$dest/powerpacks/README.claude-code-install.md" <<EOF
# Claude Code Powerpacks Bundle

This directory is copied by:

\`\`\`bash
$REPO_ROOT/adapters/claude-code/install.sh
\`\`\`

The installed \`$skill_name\` skill resolves \`powerpacks/...\` references
relative to this skill directory.
EOF
}

install_skill search-network "$REPO_ROOT/packs/search/skills/search-network/SKILL.md"
install_skill extract-search-query "$REPO_ROOT/packs/search/skills/extract-search-query/SKILL.md"
install_skill search-company "$REPO_ROOT/packs/search/skills/search-company/SKILL.md"
install_skill search-contacts "$REPO_ROOT/packs/contacts/skills/search-contacts/SKILL.md"
install_skill powerset "$REPO_ROOT/packs/powerset/skills/powerset/SKILL.md"
install_skill powerset-login "$REPO_ROOT/packs/powerset/skills/powerset-login/SKILL.md"
install_skill powerset-set "$REPO_ROOT/packs/powerset/skills/powerset-set/SKILL.md"
install_skill import-contacts "$REPO_ROOT/packs/messages/skills/import-contacts/SKILL.md"
install_skill import-whatsapp "$REPO_ROOT/packs/messages/skills/import-whatsapp/SKILL.md"
install_skill msgvault "$REPO_ROOT/packs/ingestion/skills/msgvault/SKILL.md"
install_skill local-msg-vault "$REPO_ROOT/packs/ingestion/skills/local-msg-vault/SKILL.md"
install_skill sales-nav-search "$REPO_ROOT/packs/sales-nav/skills/sales-nav-search/SKILL.md"

echo "installed Powerpacks skills into $SKILLS_DIR:"
echo "  search-network extract-search-query search-company search-contacts powerset powerset-login powerset-set sales-nav-search"
echo "  import-contacts import-whatsapp msgvault local-msg-vault"
echo
echo "restart Claude Code to pick up the skill list"
