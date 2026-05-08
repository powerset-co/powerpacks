# llm_review_contacts

LLM ENRICH/SKIP review of message contacts via OpenRouter. Stdlib-only.

By default only unmatched or suggested named contacts are reviewed. The
verdict is used to update the `skip` column in the contacts CSV in place.
The model call is deterministic (`temperature: 0`) and the prompt asks it to
skip personal-note/context labels such as dating-app/source notes, location
shorthand, event tags, and relationship labels when those notes are what make
the contact identifiable.

## Privacy contract

The primitive sends only:

- `name`
- `source` (`imessage` / `whatsapp`)
- `message_count`
- recency (`today`, `12 days ago`, …) — derived from `last_message`
- `is_in_group_chats`
- `group_names`

It does **not** send phone numbers, message text, or any other identifier.

The only field updated in the CSV is `skip`. `SKIP` writes `skip=yes`; `ENRICH`
clears stale skip values. A JSONL of per-contact verdicts is written next to
the CSV for auditability.

## Usage

```bash
# 1. Estimate cost without calling the API.
uv run --project . python packs/messages/primitives/llm_review_contacts/llm_review_contacts.py estimate \
  --input .powerpacks/messages/contacts.csv

# 2. Run the review. Auto-loads OPENROUTER_API_KEY from the repo .env.
uv run --project . python packs/messages/primitives/llm_review_contacts/llm_review_contacts.py review \
  --input .powerpacks/messages/contacts.csv \
  --model anthropic/claude-sonnet-4-6

# Tune request shape/concurrency. Defaults are 20 contacts/request, 4 workers.
uv run --project . python packs/messages/primitives/llm_review_contacts/llm_review_contacts.py review \
  --input .powerpacks/messages/contacts.csv \
  --batch-size 20 \
  --max-workers 4

# Use a cheaper model.
uv run --project . python ... llm_review_contacts.py review \
  --input .powerpacks/messages/contacts.csv \
  --model openai/gpt-4.1-mini

# Review every named contact (including already-matched).
uv run --project . python ... llm_review_contacts.py review --input ... --all
```

Rate-limit behavior: each batch retries transient/rate-limit errors with
`retry-after` when OpenRouter/provider responses include it. Reduce
`--max-workers` if the provider starts returning repeated 429s.

## Default match-status filter

Rows are reviewed when `match_status ∈ {unmatched, suggested, ""}` and
`matched_person_id` is empty. Use `--all` to also review matched rows.

## Artifacts

- `<input>.llm_review.jsonl` — one JSON record per contact verdict
  `{phone, name, verdict, reason, match_status}`
- `<input>.llm_review.jsonl.manifest.json` — counts, tokens, cost, errors

## Models

Pricing table built in for cost estimation:

| Model | Input $/1M | Output $/1M |
| --- | --- | --- |
| anthropic/claude-sonnet-4-6 | 3.00 | 15.00 |
| anthropic/claude-haiku-4-5 | 0.80 | 4.00 |
| openai/gpt-4.1 | 2.00 | 8.00 |
| openai/gpt-4.1-mini | 0.40 | 1.60 |
| openai/gpt-4.1-nano | 0.10 | 0.40 |
