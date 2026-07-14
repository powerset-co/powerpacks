# agentic_candidate_review

Prepare and reduce sharded candidate reviews for Codex, Claude Code, or another
host harness.

This is an optional standalone/manual review utility, not the automatic judge
stage in current standard or deep `$search`.

This primitive does not call an LLM. It creates immutable review shards from a
hydrated search run, then merges completed shard outputs into one final sorted
review file for the user.

## Prepare Shards

```bash
uv run --project . python \
  packs/search/primitives/agentic_candidate_review/agentic_candidate_review.py prepare \
  --state .powerpacks/runs/search-network-<id>.json \
  --shard-size 25
```

Outputs:

- `review_manifest.json`
- `reviewer_instructions.md`
- `review_shards/0000.json`, `review_shards/0001.json`, ...
- empty `review_outputs/` directory for host reviewer outputs

By default, `prepare` also records `agentic_candidate_review_prepare` in the
task state and writes all review paths under
`artifacts.agentic_candidate_review`. Use `--no-write-state` only for dry
artifact experiments.

Each reviewer writes one JSONL file named by the shard manifest's
`output_jsonl`.

## Review Output Contract

Each output row must include:

```json
{
  "person_id": "person-id",
  "score": 0.82,
  "decision": "yes",
  "evidence": "Currently builds production backend systems in SF.",
  "concerns": "No explicit infra depth."
}
```

Valid `decision` values are `strong_yes`, `yes`, `maybe`, and `no`.

## Reduce To One Sorted Review File

```bash
uv run --project . python \
  packs/search/primitives/agentic_candidate_review/agentic_candidate_review.py reduce \
  --manifest .powerpacks/runs/.../agentic_candidate_review/review_manifest.json \
  --write-state
```

Outputs:

- `ranked_candidates.jsonl`
- `ranked_candidates.csv`
- `review_summary.json`

The final JSONL and CSV are sorted by descending `score`; ties preserve original
frontier order. These final files are the user-facing review artifacts. Shard
files are implementation details for parallel runtime only. With `--write-state`,
the reducer records `agentic_candidate_review_reduce` and updates
`artifacts.agentic_candidate_review` with `ranked_candidates.jsonl`,
`ranked_candidates.csv`, and `review_summary.json`.
