## Powerpacks Search Rules

Use the Powerpacks docs as a hard contract when calling search tools.

- Decompose free text first.
- Treat that decomposition as the `expand` phase.
- Use `$search-network` as the top-level operational entrypoint.
- Choose a strategy before retrieval: direct, count-first, slices, or clarify.
- Generate multiple bounded retrieval slices only when the query warrants it.
- Assess the frontier before hydration or presentation.
- Prefer TurboPuffer for retrieval.
- Prefer Postgres for hydration after retrieval.
- Do not run expensive scoring in V1.
- Do not invent field names, operators, or enum values.
- If a filter shape is unclear, consult:
  - `powerpacks/docs/turbopuffer-contract.md`
  - `powerpacks/schemas/role-search-filters.schema.json`
  - `powerpacks/schemas/search-slice.schema.json`
  - `powerpacks/schemas/search-strategy-decision.schema.json`
  - `powerpacks/schemas/frontier-assessment.schema.json`
  - `powerpacks/schemas/candidate-review-plan.schema.json`

V1 public verticals:

- `people_by_role`
