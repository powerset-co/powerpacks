# build_investor_index

Rebuilds the checked-in, operator-scoped `aleph_investors_v1` contract used by
typed Powerset investor resolution.

```bash
python packs/indexing/primitives/build_investor_index/build_investor_index.py \
  --csv /path/to/investors_full.csv \
  --operator-id <operator-id>
```

The CSV must contain `urn`, `name`, `type`, and `investment_count`. Repeat
`--operator-id` when the same source is authorized for more than one operator.
The builder rejects a source with no valid canonical investor rows before any
remote write.
Each rebuild replaces the complete Powerpacks-owned investor namespace in one
TurboPuffer write request. The request deletes the prior snapshot by filter and
then upserts the validated replacement, so removed identities, aliases, and
prior operator scopes do not remain resolvable and a rejected write cannot
leave a delete-only or partial snapshot.
