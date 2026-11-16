---
name: search-company
description: Find companies by name, sector or entity type in a Powerset set (through the Powerset API) or in this Mac's local index, optionally with the people there.
---

# Search Company

Use this when the user asks for company search directly, or wants the
companies behind a people search inspected first.

Examples:

- `/search-company stripe`
- `/search-company fintech startups`
- `/search-company vc firms`

## Workflow

Resolve the company set from the user's current request every time; do not
reuse earlier company search artifacts.

1. Scope: the set the chat's instructions name (`--set-id`), or `--local` when
   they name this Mac's local index. A set is searched through the Powerset API
   (`POST /v2/companies/search`), the local index in DuckDB. Nothing reads
   Postgres or TurboPuffer.
2. Turn the request into a company name (`--name`) and, for a set, canonical
   `--sector-types` (e.g. `fintech,ai_ml`) and `--entity-types`
   (e.g. `vc_firm,venture_backed_startup`).
3. Run the command below. Add `--include-people` when the user wants who works
   there.
4. Present the company count, sample companies with their people counts, and
   the scope.
5. For people at companies described by a domain, investors, funding or
   geography ("engineers at Series B fintechs backed by Sequoia"), use the
   `search` skill instead: its set search resolves those filters itself.

## Command

```bash
uv run --project . python packs/search/primitives/cloud_search/search_companies.py \
  --set-id <set id> --name "stripe" --sector-types fintech --include-people
```
