---
name: powerset-set
description: List the Powerset sets the signed-in user can search, with ids, roles and counts. A search's set is always passed on the command (`--set-id`); nothing is written to `.env`.
---

# Powerset Set

Use this skill when the user wants to list or inspect the Powerset sets they
can search.

The skill uses HTTP `/v2/sets` for set retrieval and name resolution. It is
read-only:

- lists sets accessible to the logged-in Powerset user
- resolves a name the user typed to its set id
- does not upload contacts, run searches, or mutate server-side set membership

The desktop app's Search-in picker chooses the set a chat searches and names it
in the chat's instructions; every search command then takes it as `--set-id`.
In a terminal, the user names the set in the ask and the agent passes the same
flag. There is no stored default.

## Canonical repo setup

Resolve and enter the installed Powerpacks repo first:

```bash
resolve_powerpacks_root() {
  for candidate in "${POWERPACKS_REPO_ROOT:-}" "$PWD" "$HOME/powerpacks" "$HOME/workspace/powerpacks"; do
    [[ -n "$candidate" ]] || continue
    [[ "$candidate" != */.codex/powerpacks && "$candidate" != */skills/*/powerpacks ]] || continue
    if [[ -d "$candidate/packs" && -f "$candidate/pyproject.toml" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}
repo="$(resolve_powerpacks_root)" || {
  echo "No installed Powerpacks repo found. Install/copy Powerpacks to ~/powerpacks first." >&2
  exit 1
}
cd "$repo"
```

## Workflow

1. From the installed checkout, refresh the accessible sets using the existing
   bootstrap request to HTTP `/v2/sets`:

```bash
bin/agent-bootstrap --json
```

2. Require `sets_refreshed: true`. If refresh fails, report the error; for
   missing credentials, follow `$powerset login`, then retry. Read the fresh
   `sets` array in `.powerpacks/memory/set_ids.json` and show concise rows with
   name, ID, role, and counts.
3. When the user names a set, resolve it against the returned ids or names. If
   several names match, ask which set; never guess. Hand the id to the search
   skill as `--set-id`.

## Notes

- `set_id` is a Powerset set UUID.
- Search primitives resolve it to `operator_ids` before applying TurboPuffer
  `allowed_operator_ids`.
- The API owns set listing; the local repo stores no default choice.
