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

MANAGED_SKILLS=(
  search-network search-network-jd search-profile search-company search-contacts build-local-search-index
  powerset powerset-login powerset-set update-powerpacks fix-powerpacks sales-nav-search build-outbound
  setup import-contacts import-whatsapp ingestion-onboarding onboard msgvault local-msg-vault
  import-email discover-contacts import-twitter
  import-messages import-imessage import-contacts-review
)

mkdir -p "$SKILLS_DIR"
for skill in "${MANAGED_SKILLS[@]}"; do
  rm -rf "$SKILLS_DIR/$skill"
done
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
  cp -R "$REPO_ROOT/config" "$dest/powerpacks/config"
  # Domain packs (powerset, search, messages, sales-nav, ...) carry their own
  # primitives, schemas, contracts, tasks, evals, and docs.
  cp -R "$REPO_ROOT/packs" "$dest/powerpacks/packs"
  mkdir -p "$dest/powerpacks/scripts"
  for script in run-powerpacks-console.sh build-local-duckdb-shim.py adopt-powerpacks-state.py fix-powerpacks-state.py; do
    cp "$REPO_ROOT/scripts/$script" "$dest/powerpacks/scripts/$script"
    chmod +x "$dest/powerpacks/scripts/$script"
  done
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
install_skill search-profile "$REPO_ROOT/packs/search/skills/search-profile/SKILL.md"
install_skill search-company "$REPO_ROOT/packs/search/skills/search-company/SKILL.md"
install_skill search-contacts "$REPO_ROOT/packs/contacts/skills/search-contacts/SKILL.md"
install_skill build-local-search-index "$REPO_ROOT/packs/indexing/skills/build-local-search-index/SKILL.md"
install_skill powerset "$REPO_ROOT/packs/powerset/skills/powerset/SKILL.md"
install_skill powerset-login "$REPO_ROOT/packs/powerset/skills/powerset-login/SKILL.md"
install_skill powerset-set "$REPO_ROOT/packs/powerset/skills/powerset-set/SKILL.md"
install_skill update-powerpacks "$REPO_ROOT/packs/powerset/skills/update-powerpacks/SKILL.md"
install_skill fix-powerpacks "$REPO_ROOT/packs/powerset/skills/fix-powerpacks/SKILL.md"
install_skill import-contacts "$REPO_ROOT/packs/messages/skills/import-contacts/SKILL.md"
install_skill import-whatsapp "$REPO_ROOT/packs/messages/skills/import-whatsapp/SKILL.md"
install_skill ingestion-onboarding "$REPO_ROOT/packs/ingestion/skills/ingestion-onboarding/SKILL.md"
install_skill onboard "$REPO_ROOT/packs/ingestion/skills/onboard/SKILL.md"
install_skill setup "$REPO_ROOT/packs/ingestion/skills/setup/SKILL.md"
install_skill msgvault "$REPO_ROOT/packs/ingestion/skills/msgvault/SKILL.md"
install_skill local-msg-vault "$REPO_ROOT/packs/ingestion/skills/local-msg-vault/SKILL.md"
install_skill import-email "$REPO_ROOT/packs/ingestion/skills/import-email/SKILL.md"
install_skill discover-contacts "$REPO_ROOT/packs/ingestion/skills/discover-contacts/SKILL.md"
install_skill import-twitter "$REPO_ROOT/packs/ingestion/skills/import-twitter/SKILL.md"
install_skill sales-nav-search "$REPO_ROOT/packs/sales-nav/skills/sales-nav-search/SKILL.md"
install_skill build-outbound "$REPO_ROOT/packs/apollo/skills/build-outbound/SKILL.md"

printf 'installed Powerpacks skills into %s:\n' "$SKILLS_DIR"
printf '  search-network search-profile search-company search-contacts build-local-search-index powerset powerset-login powerset-set update-powerpacks fix-powerpacks sales-nav-search build-outbound\n'
printf '  setup import-contacts import-whatsapp ingestion-onboarding onboard msgvault local-msg-vault import-email discover-contacts import-twitter\n'
printf '\nrestart Pi or run /reload to pick up the skill list\n'
