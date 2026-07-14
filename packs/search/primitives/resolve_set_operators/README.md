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

The standard Powerset pipeline runs it before company/education resolution,
prefiltering, and role retrieval:

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/resolve_set_operators/resolve_set_operators.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --env-file .env \
  --write-state
```
