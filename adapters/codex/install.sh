#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
# Codex reads user skills from ~/.agents/skills (the Agent Skills standard).
# $CODEX_HOME/skills is its deprecated location; installs before 2026-09-30
# wrote there, and Codex still reads it, so we clean our skills out of it or
# each one would load twice.
SKILLS_DIR="${1:-$HOME/.agents/skills}"
LEGACY_SKILLS_DIR="$CODEX_HOME/skills"

MANAGED_SKILLS=(
  search search-company search-sql search-contacts build-local-search-index
  powerset powerset-login powerset-set feedback update-powerpacks fix-powerpacks install-powerpacks powerpacks-doctor sales-nav-search build-outbound
  msgvault import-gmail import-twitter deep-context logbook
  import-messages clean-slate
)

# Skills that once shipped but no longer exist in the repo. Scrubbed from the
# user's skills dir on update so retired routes can't dispatch deleted primitives.
RETIRED_SKILLS=(
  search-network search-network-jd search-profile search-highlight extract-search-query recruit
  deep-setup enrich-email-markers import-contacts import-email import-imessage import-contacts-review
  import-whatsapp ingestion-onboarding onboard local-msg-vault discover-contacts
  import-gmail-network import-linkedin-network import-twitter-network
  linkedin-sync-mcp linkedin-sync-csv setup
)

"$REPO_ROOT/bin/setup-python"
mkdir -p "$SKILLS_DIR"
for skill in "${MANAGED_SKILLS[@]}" "${RETIRED_SKILLS[@]}"; do
  rm -f "$LEGACY_SKILLS_DIR/$skill/SKILL.md"
done
for skill in "${RETIRED_SKILLS[@]}"; do
  rm -f "$SKILLS_DIR/$skill/SKILL.md"
done
rm -f "$LEGACY_SKILLS_DIR/.powerpacks-install.json"

install_skill() {
  local skill_name="$1"
  local source_skill="$2"
  local dest="$SKILLS_DIR/$skill_name"
  python3 "$REPO_ROOT/bin/install-skill" "$REPO_ROOT" "$source_skill" "$dest/SKILL.md"
}

install_skill search "$REPO_ROOT/packs/search/skills/search/SKILL.md"
install_skill search-company "$REPO_ROOT/packs/search/skills/search-company/SKILL.md"
install_skill search-sql "$REPO_ROOT/packs/search/skills/search-sql/SKILL.md"
install_skill search-contacts "$REPO_ROOT/packs/contacts/skills/search-contacts/SKILL.md"
install_skill build-local-search-index "$REPO_ROOT/packs/indexing/skills/build-local-search-index/SKILL.md"
install_skill powerset "$REPO_ROOT/packs/powerset/skills/powerset/SKILL.md"
install_skill powerset-login "$REPO_ROOT/packs/powerset/skills/powerset-login/SKILL.md"
install_skill powerset-set "$REPO_ROOT/packs/powerset/skills/powerset-set/SKILL.md"
install_skill feedback "$REPO_ROOT/packs/powerset/skills/feedback/SKILL.md"
install_skill update-powerpacks "$REPO_ROOT/packs/powerset/skills/update-powerpacks/SKILL.md"
install -m 755 "$REPO_ROOT/bin/update-powerpacks" "$SKILLS_DIR/update-powerpacks/update-powerpacks"
install_skill fix-powerpacks "$REPO_ROOT/packs/powerset/skills/fix-powerpacks/SKILL.md"
install_skill install-powerpacks "$REPO_ROOT/packs/powerset/skills/install-powerpacks/SKILL.md"
install_skill powerpacks-doctor "$REPO_ROOT/packs/powerset/skills/powerpacks-doctor/SKILL.md"
install_skill import-messages "$REPO_ROOT/packs/ingestion/skills/import-messages/SKILL.md"
install_skill msgvault "$REPO_ROOT/packs/ingestion/skills/msgvault/SKILL.md"
install_skill import-gmail "$REPO_ROOT/packs/ingestion/skills/import-gmail/SKILL.md"
install_skill deep-context "$REPO_ROOT/packs/ingestion/skills/deep-context/SKILL.md"
install_skill clean-slate "$REPO_ROOT/packs/ingestion/skills/clean-slate/SKILL.md"
install_skill logbook "$REPO_ROOT/packs/ingestion/skills/logbook/SKILL.md"
install_skill import-twitter "$REPO_ROOT/packs/ingestion/skills/import-twitter/SKILL.md"
install_skill sales-nav-search "$REPO_ROOT/packs/sales-nav/skills/sales-nav-search/SKILL.md"
install_skill build-outbound "$REPO_ROOT/packs/apollo/skills/build-outbound/SKILL.md"

# Install stamp: which Powerpacks these skills came from. Auto-generated (never
# hand-bumped, so it can't drift): release version from the Release Please
# manifest + the exact commit + when. Lets update-powerpacks/doctor detect stale
# installs instead of discovering them by zombie skills.
"$REPO_ROOT/bin/powerpacks-install-stamp" "$REPO_ROOT" codex \
  "$SKILLS_DIR/.powerpacks-install.json"

# SessionEnd hook: after a Powerpacks session ends, writes
# .powerpacks/reflect/<session>/ locally; nothing leaves the machine.
# POWERPACKS_REFLECT=off disables it.
uv run --project "$REPO_ROOT" python "$REPO_ROOT/packs/powerset/primitives/reflect/hooks_install.py" --harness codex --config-dir "$CODEX_HOME" --command "$REPO_ROOT/bin/reflect hook end" || echo "warning: could not register the reflect hook" >&2

if [[ "${POWERPACKS_SKIP_AGENT_BOOTSTRAP:-}" == "1" ]]; then
  echo "skipped local Codex profile generation (POWERPACKS_SKIP_AGENT_BOOTSTRAP=1)"
elif uv run --project "$REPO_ROOT" python "$REPO_ROOT/bin/agent-bootstrap"; then
  echo "generated local Codex profile in $REPO_ROOT/.codex/AGENTS.md from $REPO_ROOT/PROFILE.md"
else
  echo "warning: agent-bootstrap failed; local Codex profile was not refreshed" >&2
fi

echo "installed Powerpacks skills into $SKILLS_DIR: search search-company search-sql search-contacts build-local-search-index powerset powerset-login powerset-set feedback update-powerpacks fix-powerpacks install-powerpacks powerpacks-doctor sales-nav-search build-outbound import-messages msgvault import-gmail deep-context clean-slate logbook import-twitter"
