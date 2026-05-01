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

Buckets are `confident | medium | review`. The TUI auto-routes when it sees
the column set above and maps the buckets to its `yes / maybe / no` tabs.

## Usage

```bash
# 1. Heuristic bucketing (default, free, stdlib-only).
python packs/messages/primitives/build_research_review_csv/build_research_review_csv.py build \
  --research-dir .powerpacks/messages/research \
  --queue-csv .powerpacks/messages/research_queue.csv \
  --output-csv .powerpacks/messages/research_review.csv

# 2. LLM-scored bucketing (uses the same SYSTEM_PROMPT aleph-mvp
#    review_phone_research.py uses, via OpenRouter).
python ... build_research_review_csv.py build \
  --bucket-mode llm \
  --model anthropic/claude-sonnet-4-6

# 3. Hand off to the existing TUI:
cd ../powerset-contacts
uv run contact-exporter review --file ../powerpacks/.powerpacks/messages/research_review.csv

# 4. Upload back to Powerset after review:
uv run contact-exporter research-review --upload ../powerpacks/.powerpacks/messages/research_review.csv
```

## Heuristic bucket rules

| Outcome | Condition |
| --- | --- |
| `review` | no real_name surfaced, OR returned name shares zero tokens with input name (likely wrong person) |
| `confident` | linkedin_url AND name_confidence ≥ 0.85 AND ≥1 work position |
| `medium` | linkedin_url + positions but lower name confidence, OR real_name + positions/location without linkedin |
| `review` | otherwise (real name only, no career evidence) |

The shared-token check on input vs returned name catches the failure mode we
saw in our smoke test: `input=L*** S***` came back as `W** S***` (different
first name, confidence 0.90). Heuristic bucketer routes those to `review`
with `identity_risk: wrong_person`.

## LLM bucket cache

In `--bucket-mode llm` mode, results are cached at
`<research-dir>/<handle>/02_review_cache.json` so re-runs skip already-scored
handles. Use `--refresh-cache` to invalidate.

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
