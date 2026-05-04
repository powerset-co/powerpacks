# decide_search_strategy

Choose the next retrieval strategy after query expansion.

Inputs:

- decomposed query
- role-search seed filters
- adjacency plan
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
- adjacency decision
- hard-filter expression decision
- prefilter execution plan
- broadness estimate
- ambiguity flags
- recommended initial limit

Use `ask_for_clarification` when company-domain adjacency could materially
change recall and the user did not request it explicitly.

This primitive should not run retrieval.
