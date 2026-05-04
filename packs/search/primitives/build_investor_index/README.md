# build_investor_index

Builds the Powerpacks TurboPuffer investor resolver namespace.

Input is a CSV with:

- `urn`
- `name`
- `type`
- `investment_count`

Default namespace: `aleph_investors_v1`.

Example:

```bash
python3 primitives/build_investor_index/build_investor_index.py \
  --csv /path/to/data/investors/investors_full.csv \
  --env-file /path/to/.env
```

`resolve_investors` expects this namespace and uses it before any fallback.
