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
  "$SKILLS_DIR/import-contacts-review" \
  "$SKILLS_DIR/import-contacts" \
  "$SKILLS_DIR/import-email" \
  "$SKILLS_DIR/import-gmail" \
  "$SKILLS_DIR/import-network" \
  "$SKILLS_DIR/search-network" \
  "$SKILLS_DIR/search-profile" \
  "$SKILLS_DIR/search-highlight" \
  "$SKILLS_DIR/extract-search-query" \
  "$SKILLS_DIR/recruit"
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
  # Domain packs (powerset, search, ingestion, sales-nav, ...) carry their own
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
install_skill install-powerpacks "$REPO_ROOT/packs/powerset/skills/install-powerpacks/SKILL.md"
install_skill import-messages "$REPO_ROOT/packs/ingestion/skills/import-messages/SKILL.md"
install_skill import-whatsapp "$REPO_ROOT/packs/ingestion/skills/import-whatsapp/SKILL.md"
install_skill ingestion-onboarding "$REPO_ROOT/packs/ingestion/skills/ingestion-onboarding/SKILL.md"
install_skill onboard "$REPO_ROOT/packs/ingestion/skills/onboard/SKILL.md"
install_skill setup "$REPO_ROOT/packs/ingestion/skills/setup/SKILL.md"
install_skill msgvault "$REPO_ROOT/packs/ingestion/skills/msgvault/SKILL.md"
install_skill local-msg-vault "$REPO_ROOT/packs/ingestion/skills/local-msg-vault/SKILL.md"
install_skill import-gmail "$REPO_ROOT/packs/ingestion/skills/import-gmail/SKILL.md"
install_skill enrich-email-markers "$REPO_ROOT/packs/ingestion/skills/enrich-email-markers/SKILL.md"
install_skill deep-context "$REPO_ROOT/packs/ingestion/skills/deep-context/SKILL.md"
install_skill deep-setup "$REPO_ROOT/packs/ingestion/skills/deep-setup/SKILL.md"
install_skill discover-contacts "$REPO_ROOT/packs/ingestion/skills/discover-contacts/SKILL.md"
install_skill import-twitter "$REPO_ROOT/packs/ingestion/skills/import-twitter/SKILL.md"
install_skill sales-nav-search "$REPO_ROOT/packs/sales-nav/skills/sales-nav-search/SKILL.md"
install_skill build-outbound "$REPO_ROOT/packs/apollo/skills/build-outbound/SKILL.md"

# Install stamp: which Powerpacks these skills came from (auto-generated, never
# hand-bumped). Lets update-powerpacks/doctor detect stale installs.
version="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["."])' "$REPO_ROOT/.release-please-manifest.json" 2>/dev/null || echo unknown)"
commit="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
cat > "$SKILLS_DIR/.powerpacks-install.json" <<EOF
{
  "package": "powerpacks",
  "version": "$version",
  "commit": "$commit",
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "harness": "claude-code",
  "repo_root": "$REPO_ROOT"
}
EOF

echo "installed Powerpacks skills into $SKILLS_DIR:"
echo "  search search-company search-sql search-contacts build-local-search-index powerset powerset-login powerset-set update-powerpacks sales-nav-search build-outbound"
echo "  setup import-messages import-whatsapp ingestion-onboarding onboard msgvault local-msg-vault import-gmail enrich-email-markers deep-context deep-setup discover-contacts import-twitter"
echo
