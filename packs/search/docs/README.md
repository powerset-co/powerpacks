# Search documentation

## Start here

| Need | Document |
| --- | --- |
| Product and system walkthrough | [`$search` architecture](search-architecture.md) |
| Lookup dispatch and live legacy ordinary-search execution | [`$search` skill](../skills/search/SKILL.md) |
| Canonical legacy recruiting runbook | [Deep-mode runbook](../skills/search/deep-mode.md) |
| Additive typed validation candidate | [Architecture boundary](search-architecture.md) |
| How the local DuckDB is built | [LinkedIn and Modal indexing](../../indexing/docs/linkedin-modal-pipeline.md) |

The architecture page is the canonical prose description. `SKILL.md` files and
primitive CLIs are the executable contracts when implementation details matter.

## Data contracts

- [Postgres hydration contract](postgres-contract.md)
- [TurboPuffer query contract](turbopuffer-contract.md)
- [TurboPuffer physical schema](turbopuffer-schema.md)
- [Semantic query examples](semantic-query-examples.md)
- [Checked-in backend contracts](../contracts/README.md)
- [JSON schemas](../schemas/)

The TurboPuffer query contract explains allowed public filters and operators;
the physical-schema page names the indexed namespaces and attributes. They are
separate because a stored attribute is not automatically a supported public
filter.

## Method and evidence

- [Agentic search method](agentic-search.md) explains the recall-first sourcing
  and evidence-first judging model in implementation-neutral terms.
- [Deep-search benchmark findings](deep-search-ground-truth-status.md) preserves
  the dated measurements that motivated the method, with explicit limitations.

## Pre-cutover legacy ownership

Bare-person lookup is the narrow typed execution exception. The legacy
standard-search and deep-recruiting orchestrators, task state,
ledgers, compatibility artifacts, `$search-company`, `$search-network`, and
NanoClaw `/search-network` remain live until their consumers and quality gates
are migrated atomically. Do not infer retirement from the presence of typed
candidate modules.
