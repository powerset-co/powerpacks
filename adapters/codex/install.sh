#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
SKILLS_DIR="${1:-$CODEX_HOME/skills}"
BUNDLE_DIR="${CODEX_POWERPACKS_BUNDLE_DIR:-$CODEX_HOME/powerpacks}"

MANAGED_SKILLS=(
  search search-company search-sql search-contacts build-local-search-index
  powerset powerset-login powerset-set update-powerpacks fix-powerpacks install-powerpacks sales-nav-search build-outbound
  setup msgvault import-gmail discover-contacts import-twitter deep-context logbook
  import-messages clean-slate
)

# Skills that once shipped but no longer exist in the repo. Scrubbed from the
# user's skills dir on update so retired routes can't dispatch deleted primitives.
RETIRED_SKILLS=(
  search-network search-network-jd search-profile search-highlight extract-search-query recruit
  deep-setup enrich-email-markers import-contacts import-email import-imessage import-contacts-review
  import-whatsapp ingestion-onboarding onboard local-msg-vault
  import-gmail-network import-linkedin-network import-twitter-network
  linkedin-sync-mcp linkedin-sync-csv
)

mkdir -p "$SKILLS_DIR"
for skill in "${MANAGED_SKILLS[@]}" "${RETIRED_SKILLS[@]}"; do
  rm -rf "$SKILLS_DIR/$skill"
done
"$REPO_ROOT/bin/setup-python"

install_powerpacks_bundle() {
  local tmp="$BUNDLE_DIR.tmp"
  local backup="$BUNDLE_DIR.backup"
  if [[ ! -e "$BUNDLE_DIR" && -e "$backup" ]]; then
    mv "$backup" "$BUNDLE_DIR"
  fi
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
  cp -R "$REPO_ROOT/config" "$tmp/config"
  # Domain packs (powerset, search, ingestion, sales-nav, ...) carry their own
  # primitives, schemas, contracts, tasks, evals, and docs.
  cp -R "$REPO_ROOT/packs" "$tmp/packs"
  # The setup product path launches the local Powerpacks Console from the
  # installed bundle, so users can run $setup from any Codex cwd. Keep setup
  # sidecars here too: setup/index runs from this installed bundle and expects
  # scripts/build-local-duckdb-shim.py to materialize restored bootstrap records
  # into .powerpacks/search-index/local-search.duckdb.
  mkdir -p "$tmp/scripts"
  for script in run-powerpacks-console.sh build-local-duckdb-shim.py adopt-powerpacks-state.py fix-powerpacks-state.py; do
    cp "$REPO_ROOT/scripts/$script" "$tmp/scripts/$script"
    chmod +x "$tmp/scripts/$script"
  done
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
    cp -R "$BUNDLE_DIR/.powerpacks" "$tmp/.powerpacks"
  fi
  if [[ -f "$BUNDLE_DIR/.env" ]]; then
    cp "$BUNDLE_DIR/.env" "$tmp/.env"
  fi

  rm -rf "$backup"
  if [[ -e "$BUNDLE_DIR" ]]; then
    mv "$BUNDLE_DIR" "$backup"
  fi
  if mv "$tmp" "$BUNDLE_DIR"; then
    rm -rf "$backup"
  else
    status=$?
    if [[ ! -e "$BUNDLE_DIR" && -e "$backup" ]]; then
      mv "$backup" "$BUNDLE_DIR"
    fi
    return "$status"
  fi
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
install_skill install-powerpacks "$REPO_ROOT/packs/powerset/skills/install-powerpacks/SKILL.md"
install_skill import-messages "$REPO_ROOT/packs/ingestion/skills/import-messages/SKILL.md"
install_skill setup "$REPO_ROOT/packs/ingestion/skills/setup/SKILL.md"
install_skill msgvault "$REPO_ROOT/packs/ingestion/skills/msgvault/SKILL.md"
install_skill import-gmail "$REPO_ROOT/packs/ingestion/skills/import-gmail/SKILL.md"
install_skill deep-context "$REPO_ROOT/packs/ingestion/skills/deep-context/SKILL.md"
install_skill clean-slate "$REPO_ROOT/packs/ingestion/skills/clean-slate/SKILL.md"
install_skill logbook "$REPO_ROOT/packs/ingestion/skills/logbook/SKILL.md"
install_skill discover-contacts "$REPO_ROOT/packs/ingestion/skills/discover-contacts/SKILL.md"
install_skill import-twitter "$REPO_ROOT/packs/ingestion/skills/import-twitter/SKILL.md"
install_skill sales-nav-search "$REPO_ROOT/packs/sales-nav/skills/sales-nav-search/SKILL.md"
install_skill build-outbound "$REPO_ROOT/packs/apollo/skills/build-outbound/SKILL.md"

# Install stamp: which Powerpacks these skills came from. Auto-generated (never
# hand-bumped, so it can't drift): release version from the Release Please
# manifest + the exact commit + when. Lets update-powerpacks/doctor detect stale
# installs instead of discovering them by zombie skills.
write_install_stamp() {
  local harness="$1" dest="$2"
  local version commit
  version="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["."])' "$REPO_ROOT/.release-please-manifest.json" 2>/dev/null || echo unknown)"
  commit="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  cat > "$dest" <<EOF
{
  "package": "powerpacks",
  "version": "$version",
  "commit": "$commit",
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "harness": "$harness",
  "repo_root": "$REPO_ROOT"
}
EOF
}
write_install_stamp codex "$SKILLS_DIR/.powerpacks-install.json"
write_install_stamp codex "$BUNDLE_DIR/.powerpacks-install.json"

if [[ "${POWERPACKS_SKIP_AGENT_BOOTSTRAP:-}" == "1" ]]; then
  echo "skipped local Codex profile generation (POWERPACKS_SKIP_AGENT_BOOTSTRAP=1)"
elif uv run --project "$REPO_ROOT" python "$REPO_ROOT/bin/agent-bootstrap"; then
  echo "generated local Codex profile in $REPO_ROOT/.codex/AGENTS.md from $REPO_ROOT/PROFILE.md"
else
  echo "warning: agent-bootstrap failed; local Codex profile was not refreshed" >&2
fi

echo "installed Powerpacks skills into $SKILLS_DIR: search search-company search-sql search-contacts build-local-search-index powerset powerset-login powerset-set update-powerpacks fix-powerpacks sales-nav-search build-outbound setup import-messages msgvault import-gmail deep-context clean-slate logbook discover-contacts import-twitter"
