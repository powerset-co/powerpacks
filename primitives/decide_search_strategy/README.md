# decide_search_strategy

Choose the next retrieval strategy after query expansion.

Inputs:

- decomposed query
- role-search seed filters
- company names or resolved company IDs
- planning notes

Allowed strategies:

- `direct_execute`
- `count_then_execute`
- `generate_slices`
- `ask_for_clarification`

Expected output:

- strategy
- reason
- broadness estimate
- ambiguity flags
- recommended initial limit

This primitive should not run retrieval.
