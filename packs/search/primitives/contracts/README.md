# Contracts

Inspect, dump, and validate the checked-in Powerpacks data contracts.

This primitive is the only place a run should inspect live Postgres schema. The
normal search primitives should use checked-in contracts under
`powerpacks/contracts/` and fail closed if code references fields outside those
contracts.

## List Contracts

```bash
python powerpacks/primitives/contracts/contracts.py list
```

## Show A Contract

```bash
python powerpacks/primitives/contracts/contracts.py show postgres/persons.table.json
python powerpacks/primitives/contracts/contracts.py show turbopuffer/people.namespace.json
```

## Check Postgres

```bash
python powerpacks/primitives/contracts/contracts.py check-postgres --env-file .env
```

Checks that live Postgres contains the required columns declared in
`contracts/postgres/*.table.json`.

## Dump Live Postgres Schema

```bash
python powerpacks/primitives/contracts/contracts.py dump-postgres \
  --env-file .env \
  --out .powerpacks/schema-dumps/postgres-live.json
```

The dump is diagnostic output. Do not replace checked-in contracts with live
dumps without human review.
