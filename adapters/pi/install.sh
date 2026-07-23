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
  search search-company search-sql search-contacts build-local-search-index
  powerset powerset-login powerset-set update-powerpacks fix-powerpacks sales-nav-search build-outbound
  setup msgvault import-gmail import-twitter
  import-messages
)

# Skills that once shipped but no longer exist in the repo. Scrubbed from the
# user's skills dir on update so retired routes can't dispatch deleted primitives.
RETIRED_SKILLS=(
  search-network search-network-jd search-profile search-highlight extract-search-query recruit
  deep-setup enrich-email-markers import-contacts import-email import-imessage import-contacts-review
  import-whatsapp ingestion-onboarding onboard local-msg-vault discover-contacts
  import-gmail-network import-linkedin-network import-twitter-network
  linkedin-sync-mcp linkedin-sync-csv
)

mkdir -p "$SKILLS_DIR"
for skill in "${MANAGED_SKILLS[@]}" "${RETIRED_SKILLS[@]}"; do
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
  # Domain packs (powerset, search, ingestion, sales-nav, ...) carry their own
  # primitives, schemas, contracts, tasks, evals, and docs.
  cp -R "$REPO_ROOT/packs" "$dest/powerpacks/packs"
  mkdir -p "$dest/powerpacks/scripts"
  for script in build-local-duckdb-shim.py adopt-powerpacks-state.py fix-powerpacks-state.py; do
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

install_skill search "$REPO_ROOT/packs/search/skills/search/SKILL.md"
install_skill search-company "$REPO_ROOT/packs/search/skills/search-company/SKILL.md"
install_skill search-sql "$REPO_ROOT/packs/search/skills/search-sql/SKILL.md"
install_skill search-contacts "$REPO_ROOT/packs/contacts/skills/search-contacts/SKILL.md"
install_skill build-local-search-index "$REPO_ROOT/packs/indexing/skills/build-local-search-index/SKILL.md"
install_skill powerset "$REPO_ROOT/packs/powerset/skills/powerset/SKILL.md"
install_skill powerset-login "$REPO_ROOT/packs/powerset/skills/powerset-login/SKILL.md"
install_skill powerset-set "$REPO_ROOT/packs/powerset/skills/powerset-set/SKILL.md"
install_skill update-powerpacks "$REPO_ROOT/packs/powerset/skills/update-powerpacks/SKILL.md"
install -m 755 "$REPO_ROOT/bin/update-powerpacks" "$SKILLS_DIR/update-powerpacks/update-powerpacks"
install_skill fix-powerpacks "$REPO_ROOT/packs/powerset/skills/fix-powerpacks/SKILL.md"
install_skill import-messages "$REPO_ROOT/packs/ingestion/skills/import-messages/SKILL.md"
install_skill setup "$REPO_ROOT/packs/ingestion/skills/setup/SKILL.md"
install_skill msgvault "$REPO_ROOT/packs/ingestion/skills/msgvault/SKILL.md"
install_skill import-gmail "$REPO_ROOT/packs/ingestion/skills/import-gmail/SKILL.md"
install_skill import-twitter "$REPO_ROOT/packs/ingestion/skills/import-twitter/SKILL.md"
install_skill sales-nav-search "$REPO_ROOT/packs/sales-nav/skills/sales-nav-search/SKILL.md"
install_skill build-outbound "$REPO_ROOT/packs/apollo/skills/build-outbound/SKILL.md"

version="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["."])' "$REPO_ROOT/.release-please-manifest.json" 2>/dev/null || echo unknown)"
commit="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
cat > "$SKILLS_DIR/.powerpacks-install.json" <<EOF
{
  "package": "powerpacks",
  "version": "$version",
  "commit": "$commit",
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "harness": "pi",
  "repo_root": "$REPO_ROOT"
}
EOF

printf 'installed Powerpacks skills into %s:\n' "$SKILLS_DIR"
printf '  search search-company search-contacts build-local-search-index powerset powerset-login powerset-set update-powerpacks fix-powerpacks sales-nav-search build-outbound\n'
printf '  setup import-messages msgvault import-gmail import-twitter\n'
printf '\nrestart Pi or run /reload to pick up the skill list\n'
