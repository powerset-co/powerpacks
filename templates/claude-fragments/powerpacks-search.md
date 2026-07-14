## Powerpacks Search Rules

Use `$search` as the single people-search door. Before dispatch, record the
requested result surface, candidate backend, and search depth in `decision.json`.

- Route company output to `$search-company`, relational or aggregate questions
  to `$search-sql`, and known-contact queries to `$search-contacts`.
- Treat explicit Powerset, set, team, or shared-network wording as binding to
  the Powerset backend. Treat explicit local, offline, or imported-network
  wording as binding to the local DuckDB backend. Never change retrieval
  surfaces silently as a fallback.
- Standard search (`depth: fast`) is the original one-pass `$search-network`
  path: run `search_network_pipeline.py prepare`, show its exact preview once,
  then run the emitted command with `--execute-approved` after confirmation.
- Deep search is for JDs, role briefs, shortlists, and strongest-candidate
  requests with a stated role or domain. Follow `deep-mode.md`: build the
  recruiter contract, run the automated critic, stop once for Review, then
  source diverse probes, judge evidence, apply deterministic gates, expand from
  strong anchors, and converge without another human gate.
- Deep probes preserve the approved backend, location, recruiter contract, and
  plan binding. They are candidate-archetype hypotheses, not retrieval slices.
- Powerset search uses TurboPuffer for retrieval and Postgres for set scope and
  profile hydration. Local search uses the downloaded DuckDB index.
- Do not invent fields, filter operators, enum values, or backend capabilities.
  A stored backend attribute is not automatically a supported public filter.

Current references:

- `packs/search/skills/search/SKILL.md`
- `packs/search/skills/search/deep-mode.md`
- `packs/search/docs/search-architecture.md`
- `packs/search/docs/turbopuffer-contract.md`
- `packs/search/contracts/README.md`
