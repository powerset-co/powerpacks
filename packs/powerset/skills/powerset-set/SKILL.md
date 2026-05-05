---
name: powerset-set
description: List accessible Powerset sets, show the active default set, or set POWERPACKS_DEFAULT_SET_ID so local Powerpacks search primitives inherit the desired set scope.
---

# Powerset Set

Use this skill when the user wants to choose, inspect, retrieve, list, switch,
or set the active Powerset set for local Powerpacks search.

The skill uses the Powerset Search MCP for set retrieval and name resolution.
It only manages local set scope:

- lists sets accessible to the logged-in Powerset user
- shows the resolved default set and operator IDs
- writes `POWERPACKS_DEFAULT_SET_ID` into the local ignored `.env`
- does not upload contacts, run searches, or mutate server-side set membership

## Workflow

1. Ensure Powerset login exists. If credentials are missing, route the user to
   `$powerset-login`.
2. Confirm the Powerset Search MCP is installed when needed:

```bash
python packs/powerset/primitives/mcp_install/mcp_install.py status --host all
```

3. List sets by calling the Powerset Search MCP `list_sets` tool directly.
   Do not use local SQL or `set_scope.py` for normal set retrieval.

4. Show the current default by reading `POWERPACKS_DEFAULT_SET_ID` from the
   local `.env`, then matching that ID against the MCP `list_sets` result.
   If `POWERPACKS_DEFAULT_SET_ID` is unset, check legacy
   `POWERSET_DEFAULT_SET_ID`.

5. Set the default by ID or name:

- call MCP `list_sets`
- resolve the user's `<name>` or `<set_id>` against the returned sets
- upsert only this local `.env` line. If `POWERPACKS_DEFAULT_SET_ID=` already
  exists, replace that line; otherwise append it. Do not append duplicates.

```dotenv
POWERPACKS_DEFAULT_SET_ID=<set_id>
```

6. After setting, report the selected set name, `set_id`, `person_count`,
   `member_count`, and `sales_nav_account_count` from MCP. Only run
   `resolve_set_operators` if the user specifically asks to validate local
   TurboPuffer operator scoping.

## Notes

- `set_id` is a Powerset set UUID.
- Local search primitives resolve it to `operator_ids` before applying
  TurboPuffer `allowed_operator_ids`.
- `POWERPACKS_DEFAULT_SET_ID` is preferred. `POWERSET_DEFAULT_SET_ID` is still
  accepted as a legacy alias by search primitives.
- The remote MCP owns set listing. The local repo owns only the default choice
  stored in `.env`.
