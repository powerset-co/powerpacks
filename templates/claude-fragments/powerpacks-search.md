## Powerpacks Search Rules

Use `$search` as the public router. For a sufficiently specific request, record
`SearchRoute(target, profile, backend, reason)`. Ambiguous intent returns
`needs_input`; ask once and perform no retrieval or guessed routing.

- Route person lookup, GTM, and recruiting to `target=engine`, persist one
  schema-valid `SearchSpec`, and execute `packs.search.pipeline.search`.
- Route people-at-company asks to GTM with company constraints.
- Route company-only local relational or directory questions to `$search-sql`.
  There is no public company-search command.
- Route other relational/aggregate local questions to `$search-sql` and
  contact-field/set-contact questions to `$search-contacts`.
- Use `lookup`, `gtm`, or `recruiting` profiles, not fast/deep pipeline modes.
  Recruiting follows the typed Review/resume flow in `deep-mode.md`.
- Preserve the canonical person-grain `CandidateFrontier` and report each
  typed `StageResult`, including status, counts, provenance, warnings, errors,
  and artifact paths.
- Powerset uses its selected TurboPuffer/Postgres runner; local search uses the
  selected DuckDB runner. Never invent fields, capabilities, or fallback to the
  other backend.
