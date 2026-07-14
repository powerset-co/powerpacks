# merge_candidate_frontier

Merge and deduplicate candidate files from several explicit search probes.

The output preserves per-probe provenance, overlap statistics, and probe yield
while deduplicating by person ID and LinkedIn URL. This is a standalone bridge
for benchmark/manual probe collections. The shipped deep loop owns its own
epoch union and does not require slice planning.

```bash
uv run --project . python \
  packs/search/primitives/merge_candidate_frontier/merge_candidate_frontier.py \
  --run-dir .powerpacks/search-network-jd/<slug>/ \
  --plan-json .powerpacks/search-network-jd/<slug>/plan.json
```

Use `collect-probes` when pipeline task-state files must first be converted into
the canonical `probe_summaries.json` input.
