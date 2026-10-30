---
name: powerset-set
description: List accessible Powerset sets, show the active default set, or set POWERPACKS_DEFAULT_SET_ID so local Powerpacks search primitives inherit the desired set scope.
---

# Powerset Set

Use this skill when the user wants to choose, inspect, retrieve, list, switch,
or set the active Powerset set for local Powerpacks search.

The skill uses HTTP `/v2/sets` for set retrieval and name resolution.
It only manages local set scope:

- lists sets accessible to the logged-in Powerset user
- shows the resolved default set and operator IDs
- writes `POWERPACKS_DEFAULT_SET_ID` into the local ignored `.env`
- does not upload contacts, run searches, or mutate server-side set membership

## Canonical repo setup

For `$powerset sets use <id|name>`, resolve and enter the installed
Powerpacks repo before writing `.env`:

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

For read-only set listing, still prefer the canonical repo when available.

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
3. Show the current default by reading `POWERPACKS_DEFAULT_SET_ID` from local
   `.env` and matching that ID against the returned sets. If unset, check
   legacy `POWERSET_DEFAULT_SET_ID`.
4. For `sets use <id|name>`, resolve the requested value against returned set
   IDs or names. If multiple names match, ask which set; never guess. Upsert
   only `POWERPACKS_DEFAULT_SET_ID` in local `.env` using the existing writer:

```python
from pathlib import Path
from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import write_env
write_env(Path(".env"), {"POWERPACKS_DEFAULT_SET_ID": "<selected set id>"})
```

5. Read `.env` back and report the selected set name, `set_id`, `person_count`,
   `member_count`, and `sales_nav_account_count` when available. Only run
   `resolve_set_operators` if the user specifically asks to validate local
   TurboPuffer operator scoping.

## Notes

- `set_id` is a Powerset set UUID.
- Local search primitives resolve it to `operator_ids` before applying
  TurboPuffer `allowed_operator_ids`.
- `POWERPACKS_DEFAULT_SET_ID` is preferred. `POWERSET_DEFAULT_SET_ID` is still
  accepted as a legacy alias by search primitives.
- The API owns set listing. The local repo owns only the default choice in `.env`.
