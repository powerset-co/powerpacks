#!/usr/bin/env bash
# Install Powerpacks skills into a Claude Code skills directory.
#
# By default installs to user-level skills at `~/.claude/skills/`. Pass an
# explicit target directory to install project-level instead, e.g.
# `./install.sh /path/to/repo/.claude/skills`.
#
# Each skill is installed as `<dest>/<skill-name>/SKILL.md` with its checkout
# location so commands resolve independently of the chat's directory.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEFAULT_SKILLS_DIR="$HOME/.claude/skills"
SKILLS_DIR="${1:-$DEFAULT_SKILLS_DIR}"

mkdir -p "$SKILLS_DIR"
"$REPO_ROOT/bin/setup-python"

# Skills that once shipped but no longer exist in the repo. Scrubbed from the
# user's skills dir on update so retired routes can't dispatch deleted primitives.
RETIRED_SKILLS=(
  search-network search-network-jd search-profile search-highlight extract-search-query recruit
  deep-setup enrich-email-markers import-contacts import-email import-imessage import-contacts-review
  import-whatsapp ingestion-onboarding onboard local-msg-vault discover-contacts
  import-gmail-network import-linkedin-network import-twitter-network
  linkedin-sync-mcp linkedin-sync-csv setup
)
for skill in "${RETIRED_SKILLS[@]}"; do
  rm -f "$SKILLS_DIR/$skill/SKILL.md"
done

install_skill() {
  local skill_name="$1"
  local source_skill="$2"
  python3 "$REPO_ROOT/bin/install-skill" "$REPO_ROOT" "$source_skill" "$SKILLS_DIR/$skill_name/SKILL.md"
}

install_skill search "$REPO_ROOT/packs/search/skills/search/SKILL.md"
install_skill gtm "$REPO_ROOT/packs/gtm/skills/gtm/SKILL.md"
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
install_skill install-powerpacks "$REPO_ROOT/packs/powerset/skills/install-powerpacks/SKILL.md"
install_skill fix-powerpacks "$REPO_ROOT/packs/powerset/skills/fix-powerpacks/SKILL.md"
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

# Install stamp: which Powerpacks these skills came from (auto-generated, never
# hand-bumped). Lets update-powerpacks/doctor detect stale installs.
"$REPO_ROOT/bin/powerpacks-install-stamp" "$REPO_ROOT" claude-code "$SKILLS_DIR/.powerpacks-install.json"

echo "installed Powerpacks skills into $SKILLS_DIR:"
echo "  search gtm search-company search-sql search-contacts build-local-search-index powerset powerset-login powerset-set feedback update-powerpacks install-powerpacks fix-powerpacks powerpacks-doctor sales-nav-search build-outbound"
echo "  import-messages msgvault import-gmail deep-context clean-slate logbook import-twitter"
echo
