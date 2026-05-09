# build_research_review_csv

Flatten the per-handle deep-research artifacts produced by
`deep_research_contacts` into a single research-review CSV in the **exact
shape contact-exporter's TUI consumes** (`contact-exporter review --file ...`)
and `/v2/messages-research/artifacts` accepts. Stdlib-only.

CSV columns:

```
bucket, handle, full_name, phone_e164, area_code, total_messages,
imessage_message_count, whatsapp_message_count, message_source,
last_message, imessage_last_message, whatsapp_last_message,
group_names, location_city, location_country,
top_titles, top_companies, top_title_company_pairs, schools,
short_reason, identity_risk, signals, retarget_hint, exclude,
enrich_decision
```

Buckets are `confident | medium | review`. `review_research_web` and the
legacy TUI both map those buckets to `yes / maybe / no` tabs.

## Usage

```bash
# 1. LLM network-review bucketing.
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

## Network Review Cache

LLM results are cached at `<research-dir>/<handle>/03_network_review.json`.
Powerpacks treats that as the local source of truth for yes/maybe/no review.
There is no alternate bucket mode and no heuristic fallback; if OpenRouter
cannot return a valid network review, the build fails instead of guessing.

Fresh import runs regenerate the active review CSV from current source data.
When the orchestrator archives an older review CSV, this primitive carries
forward only human state (`exclude`, `enrich_decision`, `retarget_hint`) and
does not reuse stale buckets.

## Pricing reference

| Model | Input $/1M | Output $/1M |
| --- | --- | --- |
| anthropic/claude-sonnet-4-6 | 3.00 | 15.00 |
| anthropic/claude-haiku-4-5 | 0.80 | 4.00 |
| openai/gpt-4.1 | 2.00 | 8.00 |
| openai/gpt-4.1-mini | 0.40 | 1.60 |
| openai/gpt-4.1-nano | 0.10 | 0.40 |

Per-contact tokens are roughly 600 in / 80 out, so 110 contacts on `gpt-4.1`
costs ~$0.20.
