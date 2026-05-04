# llm_filter_candidates

Filter hydrated search candidates with the same fast pre-screening prompt used
by the Aleph search pipeline.

This is not expensive scoring or reranking. It is a conservative "kick out
clearly bad candidates" pass:

- run only after `hydrate_people`
- hydrate the full frontier first, not just the visible shortlist
- score candidates from 0.0 to 1.0
- keep candidates with score >= 0.3 by default
- when uncertain, include the candidate
- record filtered people with reason and score

Usage:

```bash
python powerpacks/primitives/llm_filter_candidates/llm_filter_candidates.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --write-state
```

Dry-run without calling OpenAI:

```bash
python powerpacks/primitives/llm_filter_candidates/llm_filter_candidates.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --dry-run
```

Inputs:

- task state with `merge_candidate_frontier` or `direct_execute`
- `hydrate_people.output.profiles` covering the candidate frontier
- `OPENAI_API_KEY`

Outputs:

- `llm_filter_candidates` step in task state
- JSONL artifact with all scores
- JSONL artifact with filtered-out candidates
