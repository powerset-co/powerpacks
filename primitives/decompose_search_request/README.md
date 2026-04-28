# decompose_search_request

Normalize a search request from a query, job description, or URL into:

- normalized query text
- vertical selection
- schema-valid role/company filters

This primitive should never guess unknown fields. Its output should validate
against `schemas/decomposed-query.schema.json`.
