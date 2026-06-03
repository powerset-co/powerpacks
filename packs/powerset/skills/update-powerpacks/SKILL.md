---
name: update-powerpacks
description: Update/reinstall Powerpacks for Codex/Pi and normalize local state into the canonical non-.codex Powerpacks install. Use for $update-powerpacks, reinstalling skills, cleaning duplicate .codex installs, adopting .powerpacks state, and post-update setup checks.
---

# update-powerpacks

Use this skill for `$update-powerpacks` and for repair requests like:

- reinstall/update Powerpacks skills;
- stop Codex from running setup under `~/.codex/powerpacks`;
- clean up duplicate `.powerpacks` state directories;
- move accidentally-created `.codex/powerpacks/.powerpacks` state into the real
  Powerpacks install;
- rerun lightweight setup checks after an update.

For deeper state repair and duplicate-root cleanup, route to `$fix-powerpacks`
after updating/reinstalling skills.

`$update-powerpacks` is an installation/state-normalization workflow. It should
not run network imports, message syncs, provider enrichment, processing, uploads,
or any spend-bearing work.

## Canonical root rule

All product setup/import/index commands must run from a canonical Powerpacks repo
outside `.codex`.

Resolve the canonical repo in this order:

1. `$POWERPACKS_REPO_ROOT` if it is a Powerpacks repo and is not under `.codex`;
2. current working directory if it is a Powerpacks repo and is not under `.codex`;
3. `~/powerpacks`;
4. `~/workspace/powerpacks`.

Treat `~/.codex/powerpacks` as a **legacy skill bundle / accidental state source
only**. Do not use it as the canonical runtime root for setup.

Quick resolver:

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
```

If no canonical repo exists, ask the user where Powerpacks should be installed.
Prefer `~/powerpacks`. Do not silently create a runtime install under `.codex`.

## Update/reinstall steps

1. Resolve and enter the canonical repo:

```bash
repo="$(resolve_powerpacks_root)" || exit 1
cd "$repo"
```

2. Pull/update the repo when it is safe:

```bash
git status --short
git fetch --quiet || true
git pull --ff-only || true
```

If local changes block the pull, stop and report the changed files. Do not stash
or overwrite user changes without explicit approval.

3. Install/update Python deps and skills from the canonical repo:

```bash
bin/setup-python
bin/update-codex
# or, for Pi:
# adapters/pi/install.sh
```

4. Restart the agent/session if the skill list changed. For Pi, `/reload` or a
new session may be required. For Codex, restart Codex.

## Adopt state from accidental `.codex` installs

If useful state exists under `~/.codex/powerpacks/.powerpacks`, use the state
contract fixer before running setup/import/index:

```bash
cd "$repo"
uv run --project . python scripts/fix-powerpacks-state.py --json
uv run --project . python scripts/fix-powerpacks-state.py --apply --json
```

Only use backup/quarantine flags when the user explicitly says the `.codex` state
is the correct one, for example an authenticated WhatsApp/wacli store was made
under `.codex` and the canonical store is an unauthenticated placeholder:

```bash
uv run --project . python scripts/fix-powerpacks-state.py \
  --apply \
  --backup \
  --quarantine-legacy-state \
  --json
```

Do not delete `.codex/powerpacks` during normal update. It may still be used as a
skill-side compatibility bundle. The important invariant is that setup/product
commands run from the canonical repo and use the canonical repo's `.powerpacks/`.

## Clean duplicate state directories

After adoption, inspect likely duplicate state roots:

```bash
for p in "$repo/.powerpacks" "$HOME/.codex/powerpacks/.powerpacks" "$HOME/workspace/powerpacks/.powerpacks" "$HOME/powerpacks/.powerpacks"; do
  [[ -d "$p" ]] && echo "$p"
done
```

Report duplicates to the user. Do not remove them automatically. If the user
asks to clean up, prefer renaming stale non-canonical state roots:

```bash
mv ~/.codex/powerpacks/.powerpacks ~/.codex/powerpacks/.powerpacks.stale-$(date -u +%Y%m%dT%H%M%SZ)
```

Never delete msgvault DBs, WhatsApp stores, ledgers, or imported network data
without explicit user approval and a backup.

## Post-update checks

Run read-only/local checks from the canonical repo:

```bash
cd "$repo"
uv run --project . python packs/ingestion/primitives/setup/setup.py status \
  --operator-id <operator-id> \
  --accounts .powerpacks/ingestion/accounts.json \
  --setup-ledger .powerpacks/setup/setup-run.json
```

If the user is working through the app, start it from the canonical repo:

```bash
scripts/run-powerpacks-console.sh start --path /setup --open
```

The app should print `Repo: <canonical repo>`. If it prints a `.codex` path, stop
and fix `POWERPACKS_REPO_ROOT` / installation paths before proceeding.

## Ledger guidance

Ledgers are logs/checkpoints, not the ultimate source of truth. A dirty ledger
can explain what happened, but it should not force agents to keep using the wrong
checkout or stale state.

When troubleshooting setup after update:

- prefer current artifacts and live read-only checks over stale ledger status;
- if a ledger says `failed`, inspect the failed step and current artifacts before
  rerunning;
- do not blindly delete ledgers;
- if a ledger is clearly from the wrong install path, adopt/copy useful artifacts
  into the canonical repo, then rerun `setup.py status` from the canonical repo;
- if a stale ledger blocks the app, move it aside only with user approval:

```bash
mv .powerpacks/setup/setup-run.json .powerpacks/setup/setup-run.json.stale-$(date -u +%Y%m%dT%H%M%SZ)
```

## What not to do

- Do not run `$setup`, `setup.py import`, `import_contacts_pipeline.py`, or
  processing from `~/.codex/powerpacks`.
- Do not use `.codex/powerpacks/.powerpacks` as the long-term state root.
- Do not run doctor/fix, browser auth, WhatsApp QR linking, msgvault sync,
  imports, provider calls, processing, or uploads as part of `$update-powerpacks`
  unless the user explicitly asks for that separate operation.
