# Agentic Candidate Review Harness

Target hosts: Codex and Claude Code.

Powerpacks keeps the portable layer simple:

- `prepare` writes immutable candidate shards from a hydrated run.
- The host runs reviewer agents over shards in parallel.
- `reduce` validates all reviewer outputs and writes one sorted user-facing file.

## Why This Helps

This creates a reproducible answer for ranking questions:

- why a person was reviewed
- which shard reviewed them
- what score they received
- what evidence and concerns were recorded
- why they sorted above or below another person

It also keeps runtime-specific behavior out of core primitives. Codex and Claude
Code can use different parallel execution mechanisms while producing the same
review JSONL contract.

## Host Flow

1. Run `/search-network` through hydration.
2. Run `agentic_candidate_review prepare`; this records shard paths in the
   task JSON under `artifacts.agentic_candidate_review`.
3. Dispatch one reviewer per shard, or a bounded pool of reviewers.
4. Each reviewer writes `review_outputs/<shard_id>.jsonl`.
5. Run `agentic_candidate_review reduce --write-state`; this records final
   ranked artifact paths in the same task JSON.
6. Give the user `ranked_candidates.csv` and `ranked_candidates.jsonl`.

## Reviewer Rule

Reviewers should not append to a shared CSV. They write only their assigned
JSONL shard output. The reducer is the only writer of final sorted CSV/JSONL
artifacts.
