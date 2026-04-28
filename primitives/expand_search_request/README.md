# expand_search_request

Normalize a search request from a query, job description, or URL into:

- normalized query text
- one execution vertical
- a schema-valid role-search payload

This primitive should be able to carry:

- role/title intent
- location
- company names
- education
- years of experience
- age
- tenure/date constraints

Its output should validate against `schemas/decomposed-query.schema.json`.
