# Search documentation

## Start here

| Need | Document |
| --- | --- |
| Product and system walkthrough | [`$search` architecture](search-architecture.md) |
| Standard-search (`depth: fast`) execution contract | [`$search` skill](../skills/search/SKILL.md) |
| Deep-search operator runbook | [Deep-mode runbook](../skills/search/deep-mode.md) |
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

## Removed legacy material

The V1 slice planner, per-slice harness body, frontier workflow fragments,
README-only design primitives, session-specific parity tracker, and
pre-implementation deep-search plans were removed. They described execution
paths that neither standard nor deep search uses. Two short compatibility
redirects preserve old task-flow and task-harness links; Git history remains the
archive for the retired designs.
