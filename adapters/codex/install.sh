#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
SKILLS_DIR="${1:-$CODEX_HOME/skills}"
BUNDLE_DIR="${CODEX_POWERPACKS_BUNDLE_DIR:-$CODEX_HOME/powerpacks}"

mkdir -p "$SKILLS_DIR"
rm -rf "$SKILLS_DIR/import-messages" \
  "$SKILLS_DIR/import-imessage" \
  "$SKILLS_DIR/import-whatsapp" \
  "$SKILLS_DIR/import-contacts-review"
"$REPO_ROOT/bin/setup-python"

install_powerpacks_bundle() {
  local tmp="$BUNDLE_DIR.tmp"
  rm -rf "$tmp"
  mkdir -p "$tmp"
  cp "$REPO_ROOT/pyproject.toml" "$tmp/pyproject.toml"
  if [[ -f "$REPO_ROOT/uv.lock" ]]; then
    cp "$REPO_ROOT/uv.lock" "$tmp/uv.lock"
  fi
  # Cross-pack docs + host-install templates (no top-level primitives/skills/
  # schemas anymore — every domain lives in packs/).
  cp -R "$REPO_ROOT/docs" "$tmp/docs"
  cp -R "$REPO_ROOT/templates" "$tmp/templates"
  # Domain packs (powerset, search, messages, sales-nav, ...) carry their own
  # primitives, schemas, contracts, tasks, evals, and docs.
  cp -R "$REPO_ROOT/packs" "$tmp/packs"
  # The setup product path launches the local Powerpacks Console from the
  # installed bundle, so users can run $setup from any Codex cwd.
  mkdir -p "$tmp/scripts"
  cp "$REPO_ROOT/scripts/run-powerpacks-console.sh" "$tmp/scripts/run-powerpacks-console.sh"
  chmod +x "$tmp/scripts/run-powerpacks-console.sh"
  mkdir -p "$tmp/app"
  for file in README.md components.json index.html package-lock.json package.json postcss.config.js tailwind.config.ts tsconfig.app.json tsconfig.json tsconfig.node.json vite.config.ts; do
    if [[ -f "$REPO_ROOT/app/$file" ]]; then
      cp "$REPO_ROOT/app/$file" "$tmp/app/$file"
    fi
  done
  cp -R "$REPO_ROOT/app/public" "$tmp/app/public"
  cp -R "$REPO_ROOT/app/src" "$tmp/app/src"
  # Keep only the top-level skill entrypoint; avoid nested skill duplication
  # from copied packs during discovery.
  find "$tmp/packs" -type f -path "*/SKILL.md" -delete

  cat > "$tmp/README.codex-install.md" <<EOF
# Codex Powerpacks Bundle

This shared directory is copied by:

\`\`\`bash
$REPO_ROOT/adapters/codex/install.sh
\`\`\`

Installed Powerpacks skills link their local \`powerpacks/\` directory here.
The bundle includes the local setup console app and launcher so \`\$setup\` can
run from any Codex working directory.
EOF

  if [[ -d "$BUNDLE_DIR/.powerpacks" ]]; then
    mv "$BUNDLE_DIR/.powerpacks" "$tmp/.powerpacks"
  fi
  if [[ -f "$BUNDLE_DIR/.env" ]]; then
    cp "$BUNDLE_DIR/.env" "$tmp/.env"
  fi

  rm -rf "$BUNDLE_DIR"
  mv "$tmp" "$BUNDLE_DIR"
}

install_skill() {
  local skill_name="$1"
  local source_skill="$2"
  local dest="$SKILLS_DIR/$skill_name"
  rm -rf "$dest"
  mkdir -p "$dest"

  cp -R "$source_skill" "$dest/SKILL.md"
  ln -s "$BUNDLE_DIR" "$dest/powerpacks"
}

install_powerpacks_bundle

install_skill search-network "$REPO_ROOT/packs/search/skills/search-network/SKILL.md"
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
install_skill setup "$REPO_ROOT/packs/ingestion/skills/setup/SKILL.md"
install_skill msgvault "$REPO_ROOT/packs/ingestion/skills/msgvault/SKILL.md"
install_skill local-msg-vault "$REPO_ROOT/packs/ingestion/skills/local-msg-vault/SKILL.md"
install_skill import-email "$REPO_ROOT/packs/ingestion/skills/import-email/SKILL.md"
install_skill import-network "$REPO_ROOT/packs/ingestion/skills/import-network/SKILL.md"
install_skill import-twitter "$REPO_ROOT/packs/ingestion/skills/import-twitter/SKILL.md"
install_skill sales-nav-search "$REPO_ROOT/packs/sales-nav/skills/sales-nav-search/SKILL.md"

if [[ "${POWERPACKS_SKIP_AGENT_BOOTSTRAP:-}" == "1" ]]; then
  echo "skipped local Codex profile generation (POWERPACKS_SKIP_AGENT_BOOTSTRAP=1)"
elif uv run --project "$REPO_ROOT" python "$REPO_ROOT/bin/agent-bootstrap"; then
  echo "generated local Codex profile in $REPO_ROOT/.codex/AGENTS.md from $REPO_ROOT/PROFILE.md"
else
  echo "warning: agent-bootstrap failed; local Codex profile was not refreshed" >&2
fi

echo "installed Powerpacks skills into $SKILLS_DIR: search-network search-company search-contacts build-local-search-index powerset powerset-login powerset-set sales-nav-search setup import-contacts import-whatsapp ingestion-onboarding onboard msgvault local-msg-vault import-email import-network import-twitter"
echo "restart Codex to pick up the skill list"
