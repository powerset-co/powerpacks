#!/usr/bin/env bash
# Install Powerpacks skills into Pi's skill directory.
#
# Pi discovers skills from ~/.pi/agent/skills/ by default. Each skill is
# installed as <dest>/<skill-name>/SKILL.md plus a bundled powerpacks/
# directory next to it, matching the Codex / Claude Code adapter layout.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PI_HOME="${PI_HOME:-$HOME/.pi/agent}"
SKILLS_DIR="${1:-$PI_HOME/skills}"

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

  cat > "$dest/powerpacks/README.pi-install.md" <<EOF
# Pi Powerpacks Bundle

This directory is copied by:

\`\`\`bash
$REPO_ROOT/adapters/pi/install.sh
\`\`\`

The installed \`$skill_name\` skill resolves \`powerpacks/...\` references
relative to this skill directory.

Pi loads skills from \`~/.pi/agent/skills\` at startup and exposes them as
\`/skill:<name>\` commands. Restart Pi or run \`/reload\` after reinstalling.
EOF
}

install_skill search-network "$REPO_ROOT/packs/search/skills/search-network/SKILL.md"
install_skill extract-search-query "$REPO_ROOT/packs/search/skills/extract-search-query/SKILL.md"
install_skill search-company "$REPO_ROOT/packs/search/skills/search-company/SKILL.md"
install_skill search-contacts "$REPO_ROOT/packs/contacts/skills/search-contacts/SKILL.md"
install_skill build-local-search-index "$REPO_ROOT/packs/indexing/skills/build-local-search-index/SKILL.md"
install_skill powerset "$REPO_ROOT/packs/powerset/skills/powerset/SKILL.md"
install_skill powerset-login "$REPO_ROOT/packs/powerset/skills/powerset-login/SKILL.md"
install_skill powerset-set "$REPO_ROOT/packs/powerset/skills/powerset-set/SKILL.md"
install_skill import-contacts "$REPO_ROOT/packs/messages/skills/import-contacts/SKILL.md"
install_skill import-whatsapp "$REPO_ROOT/packs/messages/skills/import-whatsapp/SKILL.md"
install_skill ingestion-onboarding "$REPO_ROOT/packs/ingestion/skills/ingestion-onboarding/SKILL.md"
install_skill onboard "$REPO_ROOT/packs/ingestion/skills/onboard/SKILL.md"
install_skill msgvault "$REPO_ROOT/packs/ingestion/skills/msgvault/SKILL.md"
install_skill local-msg-vault "$REPO_ROOT/packs/ingestion/skills/local-msg-vault/SKILL.md"
install_skill import-email "$REPO_ROOT/packs/ingestion/skills/import-email/SKILL.md"
install_skill import-network "$REPO_ROOT/packs/ingestion/skills/import-network/SKILL.md"
install_skill import-twitter "$REPO_ROOT/packs/ingestion/skills/import-twitter/SKILL.md"
install_skill sales-nav-search "$REPO_ROOT/packs/sales-nav/skills/sales-nav-search/SKILL.md"

printf 'installed Powerpacks skills into %s:\n' "$SKILLS_DIR"
printf '  search-network extract-search-query search-company search-contacts build-local-search-index powerset powerset-login powerset-set sales-nav-search\n'
printf '  import-contacts import-whatsapp ingestion-onboarding onboard msgvault local-msg-vault import-email import-network import-twitter\n'
printf '\nrestart Pi or run /reload to pick up the skill list\n'
