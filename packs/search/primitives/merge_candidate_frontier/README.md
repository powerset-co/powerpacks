# merge_candidate_frontier

Merge and dedupe candidates across completed search slices.

The merged frontier should preserve:

- slice provenance per candidate
- overlap statistics
- per-slice yield

Use this primitive to decide whether the retrieval plan is already good enough
or needs another pass.
