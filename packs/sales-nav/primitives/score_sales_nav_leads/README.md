# score_sales_nav_leads

Fan-out LLM scoring over a local Sales Nav run.

Reads `sales_nav_artifacts` state, loads `leads.jsonl` plus `mutuals.jsonl`,
evaluates each lead against a criteria string, and writes only matching leads
(default score >= 0.7) to a task-specific output folder.

```bash
python packs/sales-nav/primitives/score_sales_nav_leads/score_sales_nav_leads.py \
  --state .powerpacks/sales-nav/runs/<run>/state.json \
  --criteria "real estate exposure" \
  --threshold 0.7
```

Outputs:

- `scores/<criteria>/matches.jsonl` — matching scored lead records with profile + mutual context
- `scores/<criteria>/matches.csv` — user-facing ranked CSV
- `scores/<criteria>/manifest.json` — counts and paths

Non-matches are not written by default. Pass `--dump-debug` to write
`raw_scores.jsonl` with all scored leads.
