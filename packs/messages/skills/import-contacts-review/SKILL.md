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
- every step that touches the network or an LLM is gated on explicit user
  approval

## Architecture

Four small primitives:

1. `auth` (in `packs/powerset/primitives/auth/`) — Auth0 PKCE login → JWT cached at
   `~/.powerpacks/credentials.json`
2. `sync_powerset_candidates` — paginated GET `/v2/contacts` using the JWT,
   writes a flat candidate CSV
3. `match_local_candidates` — exact / first-name-prefix / fuzzy last-name
   matcher updates `match_*` columns in the contacts CSV
4. `llm_review_contacts` — OpenRouter batched ENRICH/SKIP review of
   unmatched/suggested rows, updates the `skip` column

## Workflow

### 1. Confirm the contacts CSV exists

Default path: `.powerpacks/messages/contacts.csv`. If it doesn't exist, ask the
user to run `import-imessage` or `import-whatsapp` first.

### 2. Log in to Powerset (if needed)

```bash
python packs/powerset/primitives/auth/auth.py whoami
```

If `status: anonymous`, ask the user for permission to open a browser, then:

```bash
python packs/powerset/primitives/auth/auth.py login
```

This opens the user's browser to Auth0, runs a localhost callback on
`127.0.0.1:9876`, exchanges the code for a JWT, and stores credentials at
`~/.powerpacks/credentials.json`.

### 3. Sync the candidate catalog

```bash
python packs/messages/primitives/sync_powerset_candidates/sync_powerset_candidates.py sync \
  --output .powerpacks/messages/powerset_contacts.csv
```

If auth/network fails and a previous catalog exists, the manifest will report
`status: cached_after_auth_error` or `cached_after_network_error` and the
matcher can still run.

### 4. Apply local matching

```bash
python packs/messages/primitives/match_local_candidates/match_local_candidates.py match \
  --contacts .powerpacks/messages/contacts.csv \
  --candidates .powerpacks/messages/powerset_contacts.csv
```

The contacts CSV is updated in place with `match_status` ∈
`{matched, suggested, unmatched}` and the `matched_*` columns. Surface the
manifest's `stats` dict to the user before continuing.

### 5. Estimate LLM review cost (optional but recommended)

```bash
python packs/messages/primitives/llm_review_contacts/llm_review_contacts.py estimate \
  --input .powerpacks/messages/contacts.csv \
  --model anthropic/claude-sonnet-4-6
```

Show the user the candidate count and `estimated_usd` before spending money.

### 6. Run the LLM review (after explicit user request)

```bash
python packs/messages/primitives/llm_review_contacts/llm_review_contacts.py review \
  --input .powerpacks/messages/contacts.csv \
  --model anthropic/claude-sonnet-4-6
```

`OPENROUTER_API_KEY` must be in the environment, or pass `--api-key`. The
primitive only reviews unmatched/suggested rows by default; pass `--all` to
include matched rows too.

The contacts CSV is updated in place — only the `skip` column changes. A
verdict JSONL artifact is written next to the CSV for auditability.

### 7. Build the deep-research queue

```bash
# Whole ENRICH queue.
python packs/messages/primitives/prepare_research_queue/prepare_research_queue.py prepare \
  --input .powerpacks/messages/contacts.csv \
  --output .powerpacks/messages/research_queue.csv

# High-signal slice only (cross-channel + active relationships).
python packs/messages/primitives/prepare_research_queue/prepare_research_queue.py prepare \
  --input .powerpacks/messages/contacts.csv \
  --output .powerpacks/messages/research_queue.p1p2.csv \
  --tiers P1 P2a P2b
```

The manifest reports a per-tier breakdown and a Parallel.ai cost estimate at
every processor tier (`core2x` / `pro` / `ultra8x`).

### 8. Run deep research (Parallel.ai)

After explicit user approval and budget confirmation:

```bash
# Estimate the cost without making API calls first.
python packs/messages/primitives/deep_research_contacts/deep_research_contacts.py estimate \
  --input .powerpacks/messages/research_queue.p1p2.csv \
  --processor core2x

# Submit + poll + write per-handle JSON artifacts.
PARALLEL_API_KEY=... python packs/messages/primitives/deep_research_contacts/deep_research_contacts.py run \
  --input .powerpacks/messages/research_queue.p1p2.csv \
  --processor core2x \
  --output-dir .powerpacks/messages/research
```

Results land at `.powerpacks/messages/research/<handle>/01_research_parallel.json`
in the same shape `aleph-mvp` produces, so any downstream consumer of that
schema (assemble_profile, network review, etc.) keeps working.

For long batches the `submit` and `poll` subcommands can be split so the
shell isn't tied up:

```bash
python ... deep_research_contacts.py submit --input ... --processor core2x
python ... deep_research_contacts.py status --output-dir .powerpacks/messages/research
python ... deep_research_contacts.py poll --output-dir .powerpacks/messages/research
```

### 9. Build a research-review CSV for the existing TUI

Fold the per-handle research artifacts into one flat CSV in the shape
`contact-exporter`'s research-review TUI expects:

```bash
# Heuristic bucketing (free):
python packs/messages/primitives/build_research_review_csv/build_research_review_csv.py build \
  --research-dir .powerpacks/messages/research \
  --queue-csv .powerpacks/messages/research_queue.csv \
  --output-csv .powerpacks/messages/research_review.csv

# Or LLM-scored bucketing (mirrors aleph-mvp's review_phone_research SYSTEM_PROMPT):
OPENROUTER_API_KEY=... python ... build_research_review_csv.py build \
  --bucket-mode llm --model anthropic/claude-sonnet-4-6 \
  --output-csv .powerpacks/messages/research_review.csv
```

Then open the existing TUI for yes / maybe / no review:

```bash
cd ../powerset-contacts
uv run contact-exporter review --file ../powerpacks/.powerpacks/messages/research_review.csv
```

And, after the user reviews and is ready, upload the artifact back to
Powerset:

```bash
uv run contact-exporter research-review --upload \
  ../powerpacks/.powerpacks/messages/research_review.csv
```

## What this skill does NOT do

- It does not upload contacts to Powerset. Upload remains in
  `powerset_contacts_harness` (contact-exporter compatibility) until a native
  upload primitive is added.
- It does not run deep research. That is a separate, heavier pipeline.
