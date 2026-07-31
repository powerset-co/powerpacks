## Powerpacks Search Rules

Use `$search` as the live router. For a sufficiently specific people request,
record `SearchRoute(target, profile, backend, reason)`. Ambiguous intent returns
`needs_input`; ask once and perform no retrieval or guessed routing.

- Route ordinary people searches through the live legacy
  `search_network_pipeline.py` prepare/Review/run flow.
- Route bare-person lookup through `packs.search.pipeline.search`. Local fields
  are capability-derived; Powerset supports set-scoped `person_id`, `name`,
  `handle`, and `profile_url`, not email or phone.
- Route people-at-company asks to GTM with company constraints.
- Route company-only lookup/resolution to the live `$search-company` surface;
  route relational local output to `$search-sql`.
- Route contact-field/set-contact questions to `$search-contacts`.
- Recruiting JDs and role briefs use canonical legacy `deep-mode.md` and
  `deep_search_loop.py` until atomic cutover. Preserve task state and ledgers.
- Except for deterministic bare-person lookup, `packs.search.pipeline.search`
  is additive opt-in validation code only:
  deterministic tests or approved read-only real-environment comparison, never
  an implied production cutover or paid-run authorization.
- Keep `$search-network` as a recognized alias and NanoClaw `/search-network` as
  live while its task/result UI remains.
- Powerset uses TurboPuffer for scoped retrieval and Postgres for hydration.
  Local search uses the selected DuckDB. Never invent fields or capabilities.
