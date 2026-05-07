# llm_review_contacts

LLM ENRICH/SKIP review of message contacts via OpenRouter. Stdlib-only.

By default only unmatched or suggested named contacts are reviewed. The
verdict is used to update the `skip` column in the contacts CSV in place.

## Privacy contract

The primitive sends only:

- `name`
- `source` (`imessage` / `whatsapp`)
- `message_count`
- recency (`today`, `12 days ago`, …) — derived from `last_message`
- `is_in_group_chats`
- `group_names`

It does **not** send phone numbers, message text, or any other identifier.

The only field updated in the CSV is `skip`. A JSONL of per-contact verdicts
is written next to the CSV for auditability.

## Usage

```bash
# 1. Estimate cost without calling the API.
python packs/messages/primitives/llm_review_contacts/llm_review_contacts.py estimate \
  --input .powerpacks/messages/contacts.csv

# 2. Run the review. Auto-loads OPENROUTER_API_KEY from the repo .env.
python packs/messages/primitives/llm_review_contacts/llm_review_contacts.py review \
  --input .powerpacks/messages/contacts.csv \
  --model anthropic/claude-sonnet-4-6

# Use a cheaper model.
python ... llm_review_contacts.py review \
  --input .powerpacks/messages/contacts.csv \
  --model openai/gpt-4.1-mini

# Review every named contact (including already-matched).
python ... llm_review_contacts.py review --input ... --all
```

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
