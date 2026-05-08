# build_research_review_csv

Flatten the per-handle deep-research artifacts produced by
`deep_research_contacts` into a single research-review CSV in the **exact
shape contact-exporter's TUI consumes** (`contact-exporter review --file ...`)
and `/v2/messages-research/artifacts` accepts. Stdlib-only.

CSV columns:

```
bucket, handle, full_name, phone_e164, area_code, total_messages,
message_source, group_names, location_city, location_country,
top_titles, top_companies, top_title_company_pairs, schools,
short_reason, identity_risk, signals
```

Buckets are `confident | medium | review`. `review_research_web` and the
legacy TUI both map those buckets to `yes / maybe / no` tabs.

## Usage

```bash
# 1. LLM network-review bucketing (default).
python packs/messages/primitives/build_research_review_csv/build_research_review_csv.py build \
  --research-dir .powerpacks/messages/research \
  --queue-csv .powerpacks/messages/research_queue.csv \
  --output-csv .powerpacks/messages/research_review.csv

# 2. Review in the native web UI:
python packs/messages/primitives/review_research_web/review_research_web.py serve \
  --csv .powerpacks/messages/research_review.csv \
  --research-dir .powerpacks/messages/research \
  --open

# 3. Upload back to Powerset after review:
python packs/messages/primitives/upload_research_review/upload_research_review.py summarize \
  --csv .powerpacks/messages/research_review.csv

python packs/messages/primitives/upload_research_review/upload_research_review.py upload \
  --csv .powerpacks/messages/research_review.csv \
  --confirm-upload
```

## Heuristic Fallback

If OpenRouter is unavailable while scoring an individual row, the primitive
falls back to deterministic identity-safety heuristics rather than failing the
whole CSV build. The normal path is the LLM network-review scorer.

## Network Review Cache

LLM results are cached at `<research-dir>/<handle>/03_network_review.json`.
Powerpacks treats that as the local source of truth for yes/maybe/no review.
Legacy `02_review_cache.json` can still be read and upgraded to `03`; legacy
synced `06_network_review.json` is kept only as fallback data for non-LLM/debug
paths.

## Pricing reference (LLM mode)

| Model | Input $/1M | Output $/1M |
| --- | --- | --- |
| anthropic/claude-sonnet-4-6 | 3.00 | 15.00 |
| anthropic/claude-haiku-4-5 | 0.80 | 4.00 |
| openai/gpt-4.1 | 2.00 | 8.00 |
| openai/gpt-4.1-mini | 0.40 | 1.60 |
| openai/gpt-4.1-nano | 0.10 | 0.40 |

Per-contact tokens are roughly 600 in / 80 out, so 110 contacts on `gpt-4.1`
costs ~$0.20.
