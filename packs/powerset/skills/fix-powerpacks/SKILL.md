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
- “check whether operator bootstrap restored `directory.csv`, whether expected
  bootstrap files are present, and whether discovery/import manifests exist for
  each vertical”;
- “report stale run-id / temp artifact directories under `.powerpacks` so they
  can be triaged or cleaned up deliberately”;
- “clean up stale duplicate state directories so future sessions stop using
  them.”

This is a repair workflow. Its default command applies safe local repairs:
copy/adopt newer canonical state and copy `.env` only if canonical `.env` is missing, repair `accounts.json` from local msgvault,
adopt an authenticated wacli store, move aside a bad unauthenticated wacli
placeholder so the user can reauth cleanly, and repair canonical
`.powerpacks/network-import/directory.csv` from the best local operator
bootstrap bundle when that bundle has strictly more LinkedIn mappings. It must
not run imports, msgvault sync, WhatsApp sync, enrichment, processing, GCS
pulls, uploads, provider-spend operations, or destructive cleanup unless the
user separately and explicitly requests that.

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
8. When files are missing or bootstrap contents do not match expectations,
   report the exact missing path and observed counts. Do not invent substitute
   files or silently treat an old artifact as equivalent.
9. Stale run-id/temp directories are cleanup candidates, not source of truth.
   Report them by path and size/count first; quarantine/remove them only after a
   separate explicit cleanup request.

## State path contract

The tracked path contract is:

```text
config/powerpacks-state-paths.json
```

The fixer script reads that contract and knows the managed paths needed for
setup/import/enrichment to work, including:

- `.env` local runtime config/credentials, copied only if canonical `.env` is missing and never printed
- `.powerpacks/ingestion/accounts.json`
- `.powerpacks/messages/research_review.csv`
- `.powerpacks/messages/contacts.csv`
- `.powerpacks/messages/wacli/`
- `.powerpacks/messages/wacli.contacts.csv`
- `.powerpacks/network-import/directory.csv`
- `.powerpacks/network-import/discover/`
- `.powerpacks/network-import/import/`
- `.powerpacks/network-import/profile_cache_v2/`
- `.powerpacks/network-import/merged/people.csv`
- `.powerpacks/operator-bootstrap/bundles/`

Report-only paths include dirty ledgers, rebuildable DuckDB files, and stale
run-id/temp artifact directories. The canonical current paths are stable and
should not include arbitrary run IDs:

```text
.powerpacks/network-import/discover/<vertical>/manifest.json
.powerpacks/network-import/import/<vertical>/manifest.json
.powerpacks/network-import/final/
.powerpacks/network-import/merged/
.powerpacks/network-import/directory.csv
.powerpacks/network-import/profile_cache_v2/
```

Known stale/legacy cleanup candidates include paths like:

```text
.powerpacks/network-import/network-runs/<run-id>/
.powerpacks/network-import/import-network-run*.json
.powerpacks/network-import/<timestamp-or-run-id>/
```

Do not delete or quarantine these during the normal fixer run. Report them so
the user can decide whether to clean them up.

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

Run the default fixer. This applies safe repairs and scrubs a bad canonical
wacli placeholder if no authenticated store is available:

```bash
uv run --project . python scripts/fix-powerpacks-state.py --json
```

Summarize:

- canonical repo path;
- current working directory;
- legacy `.powerpacks` roots found;
- managed paths copied/adopted, including whether `.env` was copied or kept without showing contents;
- linked source checks that failed;
- operator bootstrap bundle health, including whether local bundles contain
  `.powerpacks/network-import/directory.csv`, how many usable LinkedIn mappings
  they contain, and whether canonical `directory.csv` was repaired from one;
- exact missing bootstrap files/artifacts. For each expected bootstrap artifact,
  say `present`, `missing`, or `stale`, with the observed path. Do not infer that
  another similarly named file is acceptable unless the code explicitly supports
  that fallback;
- per-source discovery/import manifest health:
  `.powerpacks/network-import/discover/<vertical>/manifest.json` and
  `.powerpacks/network-import/import/<vertical>/manifest.json`;
- stale run-id/temp artifact directories under `.powerpacks/network-import/`,
  with path, approximate size, and why they are not part of the current stable
  contract;
- local gcloud active-account/config status. This is only a local auth snapshot;
  it does not prove the latest GCS bootstrap was pulled. If the local bundle is
  stale or missing, run `$powerset setup` and then rerun `$fix-powerpacks`;
- whether Gmail accounts were repaired from msgvault;
- whether WhatsApp/wacli was authenticated, copied from a better store, or
  scrubbed for reauth;
- root cause for anything still failing.

For debugging only, inspect without changing files:

```bash
uv run --project . python scripts/fix-powerpacks-state.py --dry-run --json
```

If the user explicitly asks to clean up stale `.codex` state after adoption,
quarantine it instead of deleting:

```bash
uv run --project . python scripts/fix-powerpacks-state.py \
  --quarantine-legacy-state \
  --json
```

## What “fix” is allowed to do

Allowed by default:

- copy newer/missing managed files from legacy installs into the canonical repo,
  including managed `.powerpacks` state;
- copy legacy `.env` only when canonical `.env` is missing; never overwrite an
  existing canonical `.env`;
- update only Powerpacks-owned local state under canonical `.powerpacks`;
- read `~/.msgvault/msgvault.db` and repair `accounts.json` Gmail linkage when
  the local DB clearly contains the selected Gmail accounts;
- read-only test WhatsApp/wacli auth using the canonical store;
- inspect local operator bootstrap tarballs under
  `.powerpacks/operator-bootstrap/bundles/`;
- replace canonical `.powerpacks/network-import/directory.csv` from the best
  local operator bootstrap bundle when the bundle has strictly more nonempty
  `linkedin_url` mappings than the canonical file, with backup;
- report discovery/import manifest status for Gmail, LinkedIn, and Messages;
- report stale run-id/temp artifact directories so the harness can propose a
  cleanup plan;
- compare legacy wacli stores and copy a better authenticated store into the
  canonical repo when the canonical store is missing or unauthenticated;
- move aside a bad unauthenticated canonical wacli placeholder when no better
  authenticated store exists, so the user can reauth cleanly;
- run read-only sqlite/status checks against msgvault and wacli stores.

Requires explicit approval:

- overwrite canonical files with older/equal legacy files;
- quarantine/rename legacy `.powerpacks` directories;
- quarantine/remove stale run-id/temp artifact directories;
- move aside dirty ledgers;
- delete anything;
- run browser auth, WhatsApp QR linking, msgvault sync, imports, enrichment,
  processing, GCS bootstrap pulls, or uploads.

Never do:

- run product commands from `~/.codex/powerpacks`;
- keep `.codex/powerpacks/.powerpacks` as the long-term runtime state root;
- move `~/.msgvault/msgvault.db` into `.powerpacks`;
- delete ledgers or stores without a backup and explicit user approval.

## Response format

End with a concise summary:

```text
Canonical repo: ...
Copied/adopted: N files / M directories
Kept target because newer: ...
Linked source checks: Gmail ok, WhatsApp store present/auth unknown, LinkedIn CSV missing, ...
Bootstrap directory: canonical X LinkedIn URLs, best bundle Y LinkedIn URLs, repaired yes/no
Source stages: Gmail discovery/import ok, LinkedIn discovery/import ok, Messages discovery/import missing/stale
Missing bootstrap files: ...
Stale run dirs: ...
Quarantined legacy state: yes/no
Next: open /setup from canonical repo
```
