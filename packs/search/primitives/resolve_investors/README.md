# resolve_investors

Resolve `investor_names` into URNs usable by the company namespace
`investor_urns` filter.

Inputs:

- task state with `expand_search_request.output.role_search_filters`
- optional `investors` containing already-resolved URNs
- optional `investor_names` containing person or firm names

Outputs:

- `investor_urns`
- `unresolved_names`
- `sample_investors`

Use this before `resolve_companies` for queries like `founders backed by
Sequoia`, `founders backed by Naval Ravikant`, or `companies backed by Amplify`.
