---
name: enrich-email-markers
description: Mine local Gmail/msgvault emails for LinkedIn-resolution markers via an LLM, and produce a reviewable CSV of the context + markers we would send onward. Use for $enrich-email-markers, gmail LLM enrichment, "test the markers we'd send", or previewing per-contact identity signals before LinkedIn resolution.
---

# enrich-email-markers

Use this for `$enrich-email-markers`, "gmail enrichment via LLM", or "show me the
markers/context we'd send to an LLM for a contact".

Two local-first primitives, run in order:

1. **`build_email_context`** — local only, no spend. Reads your msgvault, and for
   each contact (the same set we'd send to LinkedIn resolution) pulls their recent
   emails and writes a per-person context file.
2. **`infer_linkedin_markers`** — LLM step (OpenAI). Classifies that context into
   LinkedIn-resolution markers (current/past employer, title, school, location,
   phone/handles) + a `linkedin_query`, with evidence + confidence per marker.

Output to review: **`markers.csv`** (one row per contact) and `email_context.csv`.

## Contract / privacy

- Step 1 reads email **bodies locally** (`message_bodies`, head + tail per email) to
  capture signatures/footers. This is local-only — nothing leaves the machine.
- Step 2 sends that context text to **OpenAI** (cloud) for marker extraction — the
  same as any LLM enrichment. Bodies therefore reach OpenAI in step 2; that is the
  intended behavior for this skill. If bodies must never leave the box, run step 1
  with `--source snippet` (Gmail's ~200-char preview) instead of full bodies.
- Third-party-sent emails and role mailboxes (`support@`, `info@`, `careers@`, …)
  are filtered out, so markers describe the contact, not a thread or a role inbox.

## Prerequisites

- msgvault synced for the account (see `$import-email` / `$msgvault`).
- `OPENAI_API_KEY` in `.env` (repo root).
- Python env ready: `bin/setup-python` if `.venv/` is missing.

## Step 1 — build context (local, free)

```bash
uv run --project . python packs/ingestion/primitives/build_email_context/build_email_context.py
```

Defaults: `--source body`, **20 emails/contact**, head/tail 300/300, role mailboxes dropped.

Tune how many emails are mined per contact with `--per-person` (more = richer
identity signal, ~linear cost; the LLM step is ~$0.0003 extra per +1 email per
contact). E.g. a deeper 50-email pass:

```bash
uv run --project . python packs/ingestion/primitives/build_email_context/build_email_context.py \
  --per-person 50
```

Override the DB or account if needed:

```bash
uv run --project . python packs/ingestion/primitives/build_email_context/build_email_context.py \
  --msgvault-db ~/.msgvault/msgvault.db --account-email <email>
```

Writes `.powerpacks/network-import/discover/email-context/email_context.{jsonl,csv}`.

## Step 2 — extract markers (LLM, ~$0.003/contact)

By default this marks up the **top 500 contacts by message volume** (deterministic —
same contacts every run, no randomness), opens `markers.csv`, and is idempotent
(resumes / skips contacts already done; `--force` to redo).

```bash
# default: top 500 by volume, auto-opens markers.csv (~$1.50 for ~500 contacts)
uv run --project . python packs/ingestion/primitives/infer_linkedin_markers/infer_linkedin_markers.py --open
```

Useful flags:
- `--limit 50` — smaller deterministic top-N (e.g. a cheap ~$0.15 preview first).
- `--all` — every contact in the context (overrides `--limit`).
- `--sample-work N --sample-personal M` — eval mode: top-N per type (for A/B work).
- `--owner-context "Went to UCLA; from Palo Alto, CA"` — a prior about the mailbox
  owner; used (gated, low-confidence) to disambiguate friends/classmates.
- `--concurrency N` (**hardcoded default 12** — safe for tier-1 OpenAI projects
  ~60 RPM). This primitive intentionally ignores the shared
  `POWERPACKS_OPENAI_CONCURRENCY` env var, so to go faster you must pass
  `--concurrency` explicitly (e.g. `--concurrency 64` on a higher-tier account).
  `--model gpt-5.2`. If you see OpenAI 429 rate-limit errors, lower
  `--concurrency` and/or raise `--max-retries` (default 8).

Writes `.powerpacks/network-import/discover/email-context/markers/markers.{jsonl,csv}`
plus a `manifest.json` with token + cost totals.

## Review

`markers.csv` has one row per contact, with `linkedin_query` and one column per
marker category (`current_employer`, `job_title`, `school`, `location`,
`professional_affiliation`, `online_identifier`, …), each `value (confidence)`.
`overall_confidence` ranks how resolvable the contact is.

Passing `--open` (above) pops it automatically on macOS. To open it manually:

```bash
open .powerpacks/network-import/discover/email-context/markers/markers.csv
```

## Cost reference

- Step 1: $0 (local).
- Step 2: ~$0.003/contact (gpt-5.2, flex). e.g. ~$1.50 for ~500 contacts. The
  manifest reports exact prompt/completion tokens and estimated USD.

## Optional next step — LinkedIn resolution

`resolve_linkedin_queue` accepts a `context` column in its queue CSV and now returns
a `candidates` shortlist. To feed these markers into it, build a queue CSV with a
`context` column (the `linkedin_query` + key markers) — paid Parallel.ai step, ask
before running.

---
_Created 2026-06-16. Changelog: 2026-06-16 initial version (body-mode default, role-mailbox filter, owner-context prior); 2026-06-17 add `--open` to auto-open markers.csv on macOS; 2026-06-19 hardcode concurrency default to 12 and stop reading `POWERPACKS_OPENAI_CONCURRENCY` (pass `--concurrency` explicitly to raise)._
