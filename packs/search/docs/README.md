# Search documentation

## Start here

| Need | Document |
| --- | --- |
| Product and system walkthrough | [Layered `$search` architecture](search-architecture.md) |
| Public routing and execution contract | [`$search` skill](../skills/search/SKILL.md) |
| Recruiting Review and resume | [Typed recruiting runbook](../skills/search/deep-mode.md) |
| Current public routes | [Search surface](search-surface.md) |
| How the local DuckDB is built | [LinkedIn and Modal indexing](../../indexing/docs/linkedin-modal-pipeline.md) |

The architecture page is the canonical prose description. `SKILL.md` files and
the typed composition-root CLI are executable contracts when implementation
details matter.

## Data contracts

- [Postgres hydration contract](postgres-contract.md)
- [TurboPuffer query contract](turbopuffer-contract.md)
- [TurboPuffer physical schema](turbopuffer-schema.md)
- [Semantic query examples](semantic-query-examples.md)
- [Checked-in backend contracts](../contracts/README.md)
- [JSON schemas](../schemas/)

The TurboPuffer query contract explains supported filters and operators; the
physical-schema page names indexed namespaces and attributes. A stored attribute
is not automatically a supported public filter.

## Method and evidence

- [Agentic search method](agentic-search.md) explains the recall-first sourcing
  and evidence-first judging model in implementation-neutral terms.
- [Deep-search benchmark findings](deep-search-ground-truth-status.md) preserves
  dated historical measurements, with explicit limitations.

## Current ownership

One `$search` router owns lookup, GTM, and recruiting. Engine requests use the
canonical typed `SearchSpec` → `CandidateFrontier` → `StageResult` path.
Company-only local relational questions use `$search-sql`; people-at-company
questions use GTM with company constraints. Company resolution remains an
internal runner capability rather than a public skill.
