# Add Sales Nav

Install and maintain Sales Navigator or adjacent sales-intel connector flows.

## Intent

- keep auth and connector behavior isolated from generic search
- expose bounded search/join primitives
- fail safely when credentials or rate limits are missing

## Expected Primitives

- `query_sales_nav`
- `join_sales_nav_results`
- `classify_connector_failure`
