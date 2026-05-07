# prepare_research_queue

Filter + reshape the unified `contacts.csv` into the input CSV that
Powerpacks `deep_research_contacts` and the legacy
`aleph-mvp/data_pipeline_v2/pipelines/synthetic/research_parallel.py`
consume. Stdlib-only.

This primitive does **not** call the deep-research pipeline. It only
produces the CSV that pipeline reads.

## Default filter

A row makes it into the research queue when:

- `name` is non-empty
- the normalized name passes the old `looks_like_real_name` rule:
  at least two name tokens, tokens of at least two characters, and at least
  five alpha characters total
- the last-name tokens do not contain the old `phone_prune_config` dating-app
  labels: `hinge`, `raya`, `tinder`, or `bumble`
- the name is not just the phone number
- `message_count >= 3` unless `--min-message-count` overrides it
- `skip != "yes"` (LLM/manual review did not reject it)
- no existing Powerset linkage: no `matched_person_id`, no
  `matched_linkedin_url`, and `match_status != matched`
- `match_status` is blank, `unmatched`, or `suggested`

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

# Then run the native Parallel primitive:
python packs/messages/primitives/deep_research_contacts/deep_research_contacts.py estimate \
  --input .powerpacks/messages/research_queue.p1p2.csv \
  --processor core2x

PARALLEL_API_KEY=... python packs/messages/primitives/deep_research_contacts/deep_research_contacts.py run \
  --input .powerpacks/messages/research_queue.p1p2.csv \
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
counts, and Parallel.ai cost estimates for the allowed processor tiers: `core`,
`core2x`, and `pro`.
