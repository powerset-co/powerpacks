# Contracts

Inspect, dump, and validate the checked-in Powerpacks data contracts.

This primitive is the only place a run should inspect live Postgres schema. The
normal search primitives should use checked-in contracts under
`packs/search/contracts/` and fail closed if code references fields outside those
contracts.

## List Contracts

```bash
uv run --project . python packs/search/primitives/contracts/contracts.py list
```

## Show A Contract

```bash
uv run --project . python packs/search/primitives/contracts/contracts.py show postgres/persons.table.json
uv run --project . python packs/search/primitives/contracts/contracts.py show turbopuffer/people.namespace.json
```

## Check Postgres

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/contracts/contracts.py check-postgres
```

Checks that live Postgres contains the required columns declared in
`contracts/postgres/*.table.json`.

## Dump Live Postgres Schema

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/contracts/contracts.py dump-postgres \
  --out .powerpacks/schema-dumps/postgres-live.json
```

The dump is diagnostic output. Do not replace checked-in contracts with live
dumps without human review.
