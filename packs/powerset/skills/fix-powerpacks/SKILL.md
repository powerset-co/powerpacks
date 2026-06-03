---
name: fix-powerpacks
description: Diagnose and repair local Powerpacks install/state path problems. Use for $fix-powerpacks, canonical repo enforcement, moving newer .powerpacks state out of .codex, validating linked msgvault/messages/WhatsApp/LinkedIn paths, and cleaning stale duplicate state roots after approval.
---

# fix-powerpacks

Use this skill for `$fix-powerpacks` and for requests like:

- “Powerpacks is running from the wrong install path”;
- “Codex used `.codex/powerpacks` and now setup is brittle”;
- “move/copy the right local data into the real Powerpacks install”;
- “check if Gmail/msgvault, WhatsApp/wacli, Messages review CSV, and network
  artifacts are wired correctly”;
- “clean up stale duplicate state directories so future sessions stop using
  them.”

This is a repair workflow. It may move/copy local state only after showing a
plan. It must not run imports, msgvault sync, WhatsApp sync, enrichment,
processing, uploads, or provider-spend operations unless the user separately and
explicitly requests that.

## Principles

1. Product commands run from the canonical non-`.codex` Powerpacks repo.
2. Powerpacks-owned local state lives under that repo’s `.powerpacks/`.
3. `~/.msgvault/msgvault.db` is external local app state; Powerpacks should
   point to it, not move it into `.powerpacks`.
4. `.codex/powerpacks/.powerpacks` is a legacy/accidental state source, not the
   runtime root.
5. Copy/adopt only files that are missing or newer than the canonical target.
6. Rename/quarantine stale bad directories instead of deleting them.
7. Ledgers are logs/checkpoints; current artifacts and live read-only checks are
   more authoritative than stale ledger statuses.

## State path contract

The tracked path contract is:

```text
config/powerpacks-state-paths.json
```

The fixer script reads that contract and knows the managed paths needed for
setup/import/enrichment to work, including:

- `.powerpacks/ingestion/accounts.json`
- `.powerpacks/messages/research_review.csv`
- `.powerpacks/messages/contacts.csv`
- `.powerpacks/messages/wacli/`
- `.powerpacks/messages/wacli.contacts.csv`
- `.powerpacks/network-import/directory.csv`
- `.powerpacks/network-import/profile_cache_v2/`
- `.powerpacks/network-import/merged/people.csv`
- `.powerpacks/operator-bootstrap/bundles/`

Report-only paths include dirty ledgers and rebuildable DuckDB files.

## Standard flow

Resolve and enter canonical repo:

```bash
resolve_powerpacks_root() {
  for candidate in "${POWERPACKS_REPO_ROOT:-}" "$PWD" "$HOME/powerpacks" "$HOME/workspace/powerpacks"; do
    [[ -n "$candidate" ]] || continue
    [[ "$candidate" != *"/.codex/"* ]] || continue
    if [[ -d "$candidate/packs" && -f "$candidate/pyproject.toml" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}
repo="$(resolve_powerpacks_root)" || {
  echo "No canonical non-.codex Powerpacks repo found. Install/copy Powerpacks to ~/powerpacks first." >&2
  exit 1
}
cd "$repo"
```

Before running repair, verify the canonical checkout actually contains the fixer.
If this file is missing, the installed skill is newer than the repo that Codex is
using; stop and run `$update-powerpacks` from a checkout/branch that contains the
fixer, or update/merge the canonical repo first.

```bash
test -f scripts/fix-powerpacks-state.py || {
  echo "Missing scripts/fix-powerpacks-state.py in $repo; update the canonical checkout first." >&2
  exit 1
}
```

Run a dry-run diagnosis first:

```bash
uv run --project . python scripts/fix-powerpacks-state.py --json
```

Summarize:

- canonical repo path;
- current working directory;
- legacy `.powerpacks` roots found;
- which managed paths would be copied;
- linked source checks that failed;
- whether Gmail selected accounts exist in msgvault;
- whether a WhatsApp/wacli store exists and its local row counts;
- whether stale duplicate ledgers or state roots exist.

If the plan is safe, apply missing/newer file adoption:

```bash
uv run --project . python scripts/fix-powerpacks-state.py --apply --json
```

If the user explicitly asks to clean up stale `.codex` state after adoption,
quarantine it instead of deleting:

```bash
uv run --project . python scripts/fix-powerpacks-state.py \
  --apply \
  --quarantine-legacy-state \
  --json
```

If the canonical target has older placeholder files and the user confirms the
legacy state is correct, use backups:

```bash
uv run --project . python scripts/fix-powerpacks-state.py \
  --apply \
  --backup \
  --json
```

## What “fix” is allowed to do

Allowed without extra approval after showing the dry-run plan:

- copy newer/missing managed files from legacy `.powerpacks` into canonical
  `.powerpacks`;
- update only Powerpacks-owned local state under canonical `.powerpacks`;
- run read-only sqlite checks against msgvault and wacli stores;
- run `setup.py status` from the canonical repo;
- start the console from the canonical repo.

Requires explicit approval:

- overwrite canonical files with older/equal legacy files;
- quarantine/rename legacy `.powerpacks` directories;
- move aside dirty ledgers;
- delete anything;
- run browser auth, WhatsApp QR linking, msgvault sync, imports, enrichment,
  processing, or uploads.

Never do:

- run product commands from `~/.codex/powerpacks`;
- keep `.codex/powerpacks/.powerpacks` as the long-term runtime state root;
- move `~/.msgvault/msgvault.db` into `.powerpacks`;
- delete ledgers or stores without a backup and explicit user approval.

## Post-fix checks

After applying fixes, run read-only status from canonical repo:

```bash
uv run --project . python packs/ingestion/primitives/setup/setup.py status \
  --operator-id <operator-id> \
  --accounts .powerpacks/ingestion/accounts.json \
  --setup-ledger .powerpacks/setup/setup-run.json
```

Then start the app from canonical repo:

```bash
scripts/run-powerpacks-console.sh start --path /setup --open
```

Confirm the launcher prints a non-`.codex` `Repo:` path. If it prints `.codex`,
stop and fix `POWERPACKS_REPO_ROOT` / installation paths before continuing.

## Response format

End with a concise summary:

```text
Canonical repo: ...
Copied/adopted: N files / M directories
Kept target because newer: ...
Linked source checks: Gmail ok, WhatsApp store present/auth unknown, LinkedIn CSV missing, ...
Quarantined legacy state: yes/no
Next: open /setup from canonical repo
```
