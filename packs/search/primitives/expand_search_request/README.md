# expand_search_request

Normalize a search request from a query, job description, or URL into:

- normalized query text
- one execution vertical
- a schema-valid role-search seed payload
- optional planning notes

`role_search_filters.semantic_query` must be dense semantic retrieval prose:
2-3 sentences describing the target person's work, responsibilities, and
experience profile. Do not use a bare title phrase such as "software engineer";
put title aliases in `bm25_queries`.

This primitive should be able to carry:

- role/title intent
- location
- company names
- seniority
- education
- years of experience
- age
- tenure/date constraints

Its output should validate against `schemas/decomposed-query.schema.json`.
