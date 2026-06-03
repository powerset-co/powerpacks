---
name: update-powerpacks
description: Update/reinstall Powerpacks agent skills from the canonical Powerpacks checkout. Use for $update-powerpacks when the user wants Codex, Claude Code, or Pi skills refreshed after code changes.
---

# update-powerpacks

Use this skill for `$update-powerpacks` only when the user wants to update the
Powerpacks code checkout and reinstall the agent-facing skills.

This skill does exactly two things:

1. Pull/update the canonical Powerpacks repo when safe.
2. Reinstall Powerpacks skills for the current agent environment.

It must not run setup, inspect/import sources, sync msgvault, authenticate
WhatsApp, run enrichment, run processing, run doctor/fix, repair state, or move
`.powerpacks` data.

## Resolve canonical repo

Run from a canonical non-`.codex` Powerpacks checkout. Prefer, in order:

1. `$POWERPACKS_REPO_ROOT` if it points to a Powerpacks repo;
2. current working directory if it is a Powerpacks repo and not under `.codex`;
3. `~/powerpacks`;
4. `~/workspace/powerpacks`.

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

If no canonical checkout exists, ask the user where it should live. Do not create
or use `~/.codex/powerpacks` as the runtime checkout.

## Update code

If the repo has no local changes, pull fast-forward:

```bash
git status --short
git fetch --quiet || true
git pull --ff-only || true
```

If local changes are present or `git pull --ff-only` fails, stop and report the
status. Do not stash, reset, merge, or overwrite user changes without explicit
approval.

## Reinstall skills

For Codex:

```bash
bin/update-codex
```

For Claude Code:

```bash
bin/update-claude-code
```

For Pi:

```bash
adapters/pi/install.sh
```

If the user did not specify the agent, infer it from context when obvious. If not
obvious, run the installer for the current agent only.

## Finish

Tell the user:

- which repo path was updated;
- which installer ran;
- the current git commit;
- that they must restart/reload the agent to pick up changed skills.

Do not run any post-update setup/status/import checks. `$update-powerpacks` is
only a code/skill refresh command.
