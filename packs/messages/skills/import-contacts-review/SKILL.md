---
name: import-contacts-review
description: After import-imessage / import-whatsapp, log in to Powerset, sync the operator candidate catalog, run local name matching, and (after explicit user request) LLM-review the unmatched contacts to decide who is worth enriching.
---

# Import Contacts Review

Use this skill **after** `import-imessage` and/or `import-whatsapp` have
produced a message-contacts CSV (e.g. `.powerpacks/messages/contacts.csv`).

The pack is privacy-first:

- never request or store message content
- never send phone numbers, emails, or message bodies to LLM providers
- LLM review only sends `name`, `source`, `message_count`, recency,
  `is_in_group_chats`, and `group_names`
- upload never happens until the final reviewed artifact step, after explicit
  user approval
- OpenRouter LLM review may proceed after an estimate under `$10.00` under the
  initial workflow consent; otherwise LLM review needs explicit cost approval
- Parallel.ai deep research always needs explicit spend approval after showing
  the count and cost estimate

When invoked by the top-level `import-contacts` workflow, the user's initial
workflow consent covers Powerset login, candidate sync, and local matching.
Parallel deep research and upload still require separate cost/action approval.
OpenRouter LLM review can skip a second approval when its estimate is under
`$10.00`; still show the cost first.

## Architecture

Eight small primitive groups:

1. `auth` (in `packs/powerset/primitives/auth/`) — Auth0 PKCE login → JWT cached at
   `~/.powerpacks/credentials.json`
2. `sync_powerset_candidates` — paginated GET `/v2/contacts` using the JWT,
   writes a flat candidate CSV
3. `match_local_candidates` — exact / first-name-prefix / fuzzy last-name
   matcher updates `match_*` columns in the contacts CSV
4. `llm_review_contacts` — OpenRouter batched ENRICH/SKIP review of
   unmatched/suggested rows, updates the `skip` column
5. `review_contacts_web` — local browser yes/no enrichment reviewer with
   `Matched`, `Suggested`, `Unmatched`, `Low signal`, and `Skipped` tabs
6. `prepare_research_queue` — applies the old `network-search-api`
   phone-contact prune rules and writes the Parallel input CSV
7. `sync_messages_research_cache` — download operator-scoped existing research
   profiles from the processing GCS bucket into `.powerpacks/messages/research`
   so paid Parallel research skips prior work
8. `deep_research_contacts` / `build_research_review_csv` /
   `review_research_web` — native Parallel deep research, profile-card review,
   and review CSV assembly
9. `upload_research_review` — upload the reviewed CSV to
   `/v2/messages-research/artifacts` after explicit user approval

## Workflow

### 1. Confirm the contacts CSV exists

Default path: `.powerpacks/messages/contacts.csv`. If it doesn't exist, ask the
user to run `import-imessage` or `import-whatsapp` first.

### 2. Log in to Powerset (if needed)

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/auth/auth.py whoami
```

If `status: anonymous`, ask the user for permission to open a browser, then:

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/auth/auth.py login
```

This opens the user's browser to Auth0, runs a localhost callback on
`127.0.0.1:9876`, exchanges the code for a JWT, and stores credentials at
`~/.powerpacks/credentials.json`.

### 3. Sync the candidate catalog

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/sync_powerset_candidates/sync_powerset_candidates.py sync \
  --output .powerpacks/messages/powerset_contacts.csv
```

If auth/network fails and a previous catalog exists, the manifest will report
`status: cached_after_auth_error` or `cached_after_network_error` and the
matcher can still run.

### 4. Apply local matching

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/match_local_candidates/match_local_candidates.py match \
  --contacts .powerpacks/messages/contacts.csv \
  --candidates .powerpacks/messages/powerset_contacts.csv
```

The contacts CSV is updated in place with `match_status` ∈
`{matched, suggested, unmatched}` and the `matched_*` columns. Surface the
manifest's `stats` dict to the user before continuing.

### 5. Estimate LLM review cost (optional but recommended)

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/llm_review_contacts/llm_review_contacts.py estimate \
  --input .powerpacks/messages/contacts.csv \
  --model anthropic/claude-sonnet-4-6
```

Show the user the candidate count and `estimated_usd` before spending money.

### 6. Run the LLM review (OpenRouter estimates under $10 can proceed)

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/llm_review_contacts/llm_review_contacts.py review \
  --input .powerpacks/messages/contacts.csv \
  --model anthropic/claude-sonnet-4-6
```

If the OpenRouter estimate is `< $10.00`, report only the cost and proceed under
the initial workflow consent. Otherwise, stop for explicit LLM cost approval
first.

`OPENROUTER_API_KEY` must be in the environment, or pass `--api-key`. The
primitive only reviews unmatched/suggested rows by default; pass `--all` to
include matched rows too.

The contacts CSV is updated in place — only the `skip` column changes. A
verdict JSONL artifact is written next to the CSV for auditability.

### 7. Build the deep-research queue

```bash
# Whole ENRICH queue.
uv run --project powerpacks python powerpacks/packs/messages/primitives/prepare_research_queue/prepare_research_queue.py prepare \
  --input .powerpacks/messages/contacts.csv \
  --output .powerpacks/messages/research_queue.csv

# High-signal slice only (cross-channel + active relationships).
uv run --project powerpacks python powerpacks/packs/messages/primitives/prepare_research_queue/prepare_research_queue.py prepare \
  --input .powerpacks/messages/contacts.csv \
  --output .powerpacks/messages/research_queue.p1p2.csv \
  --tiers P1 P2a P2b
```

The manifest reports a per-tier breakdown and a Parallel.ai cost estimate for
the allowed processor tiers: `core`, `core2x`, and `pro`.

Do not treat the raw matcher's `unmatched` count as the paid-research count.
The research queue only includes named, searchable, unresolved contacts and
defaults to the old `looks_like_real_name` plus `message_count >= 3` prune
rules from `../network-search-api/data_pipeline_v2/pipelines/synthetic/prepare_phone_contacts.py`.
It also ports the old `phone_prune_config` last-name token blocklist:
`hinge`, `raya`, `tinder`, and `bumble`.

### 8. Sync prior deep research, then run Parallel.ai with explicit approval

First sync existing operator-scoped research from the processing GCS bucket.
This avoids spending Parallel credits for handles that already have
`01_research_parallel.json` under the same operator:

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/sync_messages_research_cache/sync_messages_research_cache.py status
uv run --project powerpacks python powerpacks/packs/messages/primitives/sync_messages_research_cache/sync_messages_research_cache.py download
```

Then estimate the cost without making Parallel API calls:

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/deep_research_contacts/deep_research_contacts.py estimate \
  --input .powerpacks/messages/research_queue.p1p2.csv \
  --processor core2x \
  --output-dir .powerpacks/messages/research
```

Stop and ask only with the cost: "Estimated Parallel cost: $Y. Approve?" Only
after explicit user approval:

```bash
PARALLEL_API_KEY=... uv run --project powerpacks python powerpacks/packs/messages/primitives/deep_research_contacts/deep_research_contacts.py run \
  --input .powerpacks/messages/research_queue.p1p2.csv \
  --processor core2x \
  --output-dir .powerpacks/messages/research
```

Results land at `.powerpacks/messages/research/<handle>/01_research_parallel.json`
in the same shape `aleph-mvp` produces, so any downstream consumer of that
schema (assemble_profile, network review, etc.) keeps working.

If Parallel is unavailable because `PARALLEL_API_KEY` is not set, and the user
explicitly approves a fallback, split the queue into small shards and spawn
parallel sub-agents to do public-profile review. Each shard result must include
the contact handle, candidate LinkedIn/profile URL, confidence, and reason.
This fallback should be used for validation or tiny batches only; Parallel is
the primitive for production-scale work.

For long batches the `submit` and `poll` subcommands can be split so the
shell isn't tied up:

```bash
python ... deep_research_contacts.py submit --input ... --processor core2x
python ... deep_research_contacts.py status --output-dir .powerpacks/messages/research
python ... deep_research_contacts.py poll --output-dir .powerpacks/messages/research
```

### 9. Review contacts locally

Prefer the local web reviewer for yes/no enrichment decisions:

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/review_contacts_web/review_contacts_web.py serve \
  --contacts .powerpacks/messages/contacts.csv \
  --open
```

Clicking a card toggles `YES` / `NO` and immediately autosaves `skip=false` /
`skip=true` in the contacts CSV. Do not ask the user to edit names, match
fields, or free-text details in this flow.

Default `YES` excludes no-name rows, phone-number names, weak names, and the
ported dating-app name-token blocklist. Already matched rows still default to
`YES` unless the user explicitly skips them.

### 10. Build a research-review CSV for the existing TUI

Fold the per-handle research artifacts into one flat CSV in the shape
`contact-exporter`'s research-review TUI expects:

```bash
# Heuristic bucketing (free):
uv run --project powerpacks python powerpacks/packs/messages/primitives/build_research_review_csv/build_research_review_csv.py build \
  --research-dir .powerpacks/messages/research \
  --queue-csv .powerpacks/messages/research_queue.csv \
  --output-csv .powerpacks/messages/research_review.csv

# Or LLM-scored bucketing (mirrors aleph-mvp's review_phone_research SYSTEM_PROMPT):
OPENROUTER_API_KEY=... python ... build_research_review_csv.py build \
  --bucket-mode llm --model anthropic/claude-sonnet-4-6 \
  --output-csv .powerpacks/messages/research_review.csv
```

Then open the native web reviewer for yes / maybe / no buckets and profile
cards:

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/review_research_web/review_research_web.py serve \
  --csv .powerpacks/messages/research_review.csv \
  --research-dir .powerpacks/messages/research \
  --open
```

This ports the `contact-exporter` research-review TUI behavior:

- bucket tabs: yes / maybe / no
- a card displays phone signal, title/company, education, location, reason,
  identity risk, signals, and profile links
- clicking toggles enrich yes/no
- every click writes the CSV `exclude` column (`exclude=no` include,
  `exclude=yes` exclude), so refresh/quit does not lose progress

And, after the user reviews and explicitly approves upload, upload the artifact
back to Powerset:

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/upload_research_review/upload_research_review.py summarize \
  --csv .powerpacks/messages/research_review.csv

uv run --project powerpacks python powerpacks/packs/messages/primitives/upload_research_review/upload_research_review.py upload \
  --csv .powerpacks/messages/research_review.csv \
  --confirm-upload
```

The uploader uses the cached `$powerset login` credentials and converts the web
reviewer's `exclude` decisions into upload buckets before posting. The server
artifact stores yes/maybe/no splits; the yes split is the include/enrich set.

## What this skill does NOT do

- It does not upload contacts or reviewed research artifacts without explicit
  user approval.
- It does not run Parallel deep research without explicit cost approval.
- It only runs OpenRouter/Sonnet LLM review without a second approval when the
  estimate is under `$1.00`; otherwise LLM review also requires explicit cost approval.
