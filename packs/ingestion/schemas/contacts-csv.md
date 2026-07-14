# Powerpacks messages contacts CSV schema

Canonical file: `.powerpacks/messages/contacts.csv`

This is the input schema expected by the message-contact matching, LLM review,
and research-queue primitives. If an agent receives a legacy CSV with different
headers, convert it to this schema before rerunning the import pipeline.

## Required input columns

A minimally convertible contacts CSV must include:

```text
phone,name
```

`phone` should be phone-ish and will be canonicalized to E.164 when possible.
`name` should be the contact display/full name.

## Canonical header order

```text
phone,name,source,is_in_group_chats,group_names,message_count,imessage_message_count,whatsapp_message_count,last_message,imessage_last_message,whatsapp_last_message,skip,match_status,matched_person_id,matched_name,matched_linkedin_url,match_confidence,match_method,match_reason
```

## Field notes

| Column | Meaning |
| --- | --- |
| `phone` | Phone number, preferably E.164 (`+14155550101`) |
| `name` | Contact full/display name |
| `source` | `imessage`, `whatsapp`, or comma-separated sources |
| `is_in_group_chats` | `true`/`false` |
| `group_names` | Group names joined with ` \| ` |
| `message_count` | Total non-negative message count across known message sources |
| `imessage_message_count` | iMessage/SMS message count if known |
| `whatsapp_message_count` | WhatsApp direct-chat message count if known |
| `last_message` | Latest ISO-8601 timestamp across known message sources |
| `imessage_last_message` | Latest iMessage/SMS timestamp if known |
| `whatsapp_last_message` | Latest WhatsApp timestamp if known |
| `skip` | `yes`/`true` to exclude from research |
| `match_status` | blank, `unmatched`, `suggested`, or `matched` |
| `matched_person_id` | Person ID for a confirmed match in the imported local network |
| `matched_name` | Name on the matched local-network person |
| `matched_linkedin_url` | Matched LinkedIn/profile URL |
| `match_confidence` | Numeric confidence |
| `match_method` | Matcher method label |
| `match_reason` | Short reason |

## Common legacy mappings

If the file looks like an old research queue or review CSV, use these mappings:

| Legacy/source column | Canonical column |
| --- | --- |
| `phone_e164`, `phone_number`, `primary_phone` | `phone` |
| `display_name`, `full_name`, `real_name` | `name` |
| `total_messages` | `message_count` |
| `imessage_messages`, `imessage_count`, `imessage_message_count` | `imessage_message_count` |
| `whatsapp_messages`, `whatsapp_count`, `whatsapp_message_count` | `whatsapp_message_count` |
| `message_source`, `source_channel` | `source` |
| `last_message` | `last_message` |
| `imessage_last_message` | `imessage_last_message` |
| `whatsapp_last_message` | `whatsapp_last_message` |
| `is_in_group_chats` | `is_in_group_chats` |
| `group_names` | `group_names` |
| `match_status` | `match_status` |
| `match_confidence` | `match_confidence` |
| `match_method` | `match_method` |
| `match_reason` | `match_reason` |

Fill missing optional canonical columns with blanks, except `source` can default
to `phone` or the known channel if no better source is available. For new
exports, agents should preserve per-source counts and set `message_count` to the
total across known message sources.
