## Powerpacks Search Rules

Use the Powerpacks docs as a hard contract when calling search tools.

- Decompose free text first.
- Prefer TurboPuffer for retrieval.
- Prefer Postgres for hydration after retrieval.
- Do not invent field names, operators, or enum values.
- If a filter shape is unclear, consult:
  - `powerpacks/docs/turbopuffer-contract.md`
  - `powerpacks/schemas/role-search-filters.schema.json`
  - `powerpacks/schemas/company-search-filters.schema.json`

V1 public verticals:

- `people_by_role`
- `people_by_company`
