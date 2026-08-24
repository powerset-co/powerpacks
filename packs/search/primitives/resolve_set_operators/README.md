# resolve_set_operators

Resolve a Powerset `set_id` into the Auth0 user/operator IDs stored in
TurboPuffer `allowed_operator_ids`.

Resolution order:

1. `--set-id`
2. `--payload-json '{"set_id": "..."}'`
3. `--state` with `expand_search_request.output.role_search_filters.set_id`
4. `POWERPACKS_DEFAULT_SET_ID` or `POWERSET_DEFAULT_SET_ID`
5. The logged-in user's active personal set, inferred from
   `~/.powerpacks/credentials.json`

Example:

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/resolve_set_operators/resolve_set_operators.py \
  --set-id 00000000-0000-0000-0000-000000000000 \
  --env-file .env
```

The typed `$search` engine does not use task state. Its Powerset composition
boundary receives an explicit `PowersetCorpus(set_id, operator_ids)` and passes
that immutable scope to the remote runner. This CLI remains available for
explicit set-resolution operations and legacy state inspection only.
