# expand_search_request

Normalize a search request from a query, job description, or URL into:

- normalized query text
- one execution vertical
- a schema-valid role-search seed payload
- optional planning notes

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
