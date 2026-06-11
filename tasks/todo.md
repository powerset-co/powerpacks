# Powerpacks TODOs 📋

> Created: 2026-06-11
>
> Change log:
> - 2026-06-11: Initial file; added relationship-strength search feature TODO.

## Relationship strength as a first-class search signal 🤝

Goal: let search filter/sort/rerank by how warm a contact actually is
("senior infra engineers I've actually talked to in the last year").

- [ ] **Hydration**: join `local_person_source_summary` during candidate
      hydration so each result carries `message_count`, `last_interaction`
      (most recent interaction date across channels), and `source_channels`.
- [ ] **Pipeline capture**: verify the ingestion pipeline actually captures
      last-interaction timestamps and message counts for every source
      (iMessage, WhatsApp, Gmail/msgvault, Twitter). If coverage is partial,
      either denormalize the fields onto the main people tables at index
      build time, or build a small `local_interactions` /
      `local_person_source_summary`-style aggregate table that all sources
      write into. (Per repo rules: no ledgers, no run ids — just another
      records JSONL + table in the existing index build.)
- [ ] **Filter DSL**: expose the new fields (`message_count`,
      `last_interaction_epoch`, `source_channels`) as filterable columns in
      the local filter DSL (`Gte` on recency, `Gt` on counts,
      `ContainsAny` on channels).
- [ ] **Rerankers**: update LLM filter/rerank prompts so they understand the
      relationship-strength fields and can use them when the query implies
      warmth ("people I know", "warm intro to ...").
- [ ] **Extraction**: teach query extraction to emit relationship-strength
      traits (e.g. "people I've messaged recently" → recency filter) so the
      signal is reachable from natural language, not just manual filters.

Context: today `local_person_source_summary` exists in the local DuckDB but
no retrieval stage, hydration step, or reranker reads it. Related new work:
the agentic SQL vertical (`search-sql` skill) can join it manually in the
meantime.
