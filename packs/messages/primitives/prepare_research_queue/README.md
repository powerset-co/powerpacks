# prepare_research_queue

Filter + reshape the unified `contacts.csv` into the input CSV that
`aleph-mvp/data_pipeline_v2/pipelines/synthetic/research_parallel.py`
consumes. Stdlib-only.

This primitive does **not** call the deep-research pipeline. It only
produces the CSV that pipeline reads.

## Default filter

A row makes it into the research queue when:

- `name` is non-empty
- `skip != "yes"` (LLM review didn't ENRICH-deny it)
- `matched_person_id` is empty (no existing Powerset linkage)

## Priority tiers

Each row gets a `priority_reason` from this ladder:

| Tier | Definition |
| --- | --- |
| **P1** | cross-channel (`imessage,whatsapp`) AND (`message_count >= 100` OR `last_message <= 365 days`) |
| **P2a** | cross-channel, any volume/recency |
| **P2b** | single channel, lifetime `message_count >= 100` |
| **P3** | single channel, recent (`last_message <= 365d`) and `message_count >= 10` |
| **P4** | everything else |

Rows are sorted by `(priority_tier, -message_count)` so `--limit N` always
picks the highest-signal N first.

## Usage

```bash
# Whole queue:
python packs/messages/primitives/prepare_research_queue/prepare_research_queue.py prepare \
  --input .powerpacks/messages/contacts.csv \
  --output .powerpacks/messages/research_queue.csv

# P1+P2 only, top 50:
python packs/messages/primitives/prepare_research_queue/prepare_research_queue.py prepare \
  --input .powerpacks/messages/contacts.csv \
  --output .powerpacks/messages/research_queue.p1p2.csv \
  --tiers P1 P2a P2b \
  --limit 50

# Then hand off to aleph-mvp:
cd ../aleph-mvp
uv run python -m data_pipeline_v2.pipelines.synthetic.research_parallel \
  --input ../powerpacks/.powerpacks/messages/research_queue.p1p2.csv \
  --processor core2x
```

## Output

A 41-column CSV in the exact column order `research_parallel.py` expects.
Most columns are blank for phone-source contacts (`primary_email`, `domain`,
`bio`, `follower_count`, etc.); only the phone/messaging fields are populated:

```
handle, display_name, first_name, last_name,
total_messages, source_channel="phone", message_source,
phone_e164, phone_last4, area_code,
last_message, is_in_group_chats, group_names,
match_status, match_confidence, match_method, match_reason,
priority_reason
```

The manifest JSON next to the CSV reports `by_tier_total`, eligible/filtered
counts, and a Parallel.ai cost estimate at each processor tier.
