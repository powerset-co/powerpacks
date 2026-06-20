---
name: enrich-email-markers
description: Mine local Gmail/msgvault emails for LinkedIn-resolution markers via an LLM, and produce a reviewable CSV of the context + markers we would send onward. Use for $enrich-email-markers, gmail LLM enrichment, "test the markers we'd send", or previewing per-contact identity signals before LinkedIn resolution.
---

# enrich-email-markers

Use this for `$enrich-email-markers`, "gmail enrichment via LLM", or "show me the
markers/context we'd send to an LLM for a contact".

Three steps, run in order:

1. **`build_email_context`** — local only, no spend. Reads your msgvault, and for
   each contact (the same set we'd send to LinkedIn resolution) pulls their recent
   emails and writes a per-person context file.
2. **`infer_linkedin_markers`** — LLM step (OpenAI). Classifies that context into
   LinkedIn-resolution markers (employers, title, school, location, phone/handles)
   + a `linkedin_query`, with evidence + confidence per marker.
3. **A/B resolution** — proves the markers help. Resolves the same contacts on
   LinkedIn **twice** (without the markers as context, then with) and diffs the two
   runs, so the lift is attributable to the markers. This is the **paid Parallel
   step** — it runs as the next step automatically, but you MUST confirm the
   spend estimate first (see Step 3).

Outputs to review: **`markers.csv`** (markers), then **`ab_comparison.csv`** /
**`ab_summary.json`** (the resolution lift from attaching those markers).

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
  (The owner's **identity** — name + all synced email addresses — is auto-derived
  from msgvault `sources`/`participants` and always passed to the LLM so it knows
  who "me" is and never mints a marker from the owner's own identity. No flag.)
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
marker category (`employers`, `job_title`, `school`, `location`,
`professional_affiliation`, `online_identifier`, …), each `value (confidence)`.
The `employers` column lists every company the contact works/worked at, each
tagged `(current)`/`(past)`. `overall_confidence` ranks how resolvable they are.

Passing `--open` (above) pops it automatically on macOS. To open it manually:

```bash
open .powerpacks/network-import/discover/email-context/markers/markers.csv
```

## Reading results — use the schema, do not invent

When you report what was found, read it **only** from the generated artifacts.
Never summarize markers, employers, schools, names, or counts from memory or from
the email context — every claim must come from a row that exists in the output.

- **Report findings from `markers.csv`** — it is the flat, schema'd artifact: one
  row per contact; columns are exactly `email, full_name, type, company_guess,
  overall_confidence, canonical_name, linkedin_query, <one column per marker
  category>, error`. A blank category cell means no marker of that kind — not
  "unknown", do not fill it in.
- **Report counts/cost from `manifest.json`** — `people_total`, `new_this_run`,
  `resumed_skipped`, `errors`, `estimated_cost_usd`. If `errors > 0`, say so
  explicitly (rate-limit failures leave error rows; the run is partial).
- **If you read `markers.jsonl`, mind the nesting.** Each line is
  `{email, full_name, …, usage, markers: {canonical_name, markers: [ {category,
  value, evidence, confidence}, … ], linkedin_query, overall_confidence}}`. The
  per-contact schema object is under the `markers` key,
  and the marker **array** is the inner `markers.markers`. Every marker carries the
  `evidence` phrase it came from — quote that, don't paraphrase a value into a fact.
- **Trust the phase gate.** Step 2 refuses to run if step 1's `email_context` is
  missing, empty, or its manifest status is not `completed` / has 0 contacts. So if
  step 2 produced a `markers.csv`, step 1 succeeded. Do not report markers when a
  step errored out — re-run the failed step instead.

## Cost reference

- Step 1: $0 (local).
- Step 2: ~$0.003/contact (gpt-5.2, flex). e.g. ~$1.50 for ~500 contacts. The
  manifest reports exact prompt/completion tokens and estimated USD.
- Step 3: `contacts_queued × 2` Parallel lookups (control + treatment arm) at the
  processor price (core2x = $0.05/lookup default). e.g. ~$41 for 414 contacts.
  Confirm this estimate with the user before running — it is the only paid step
  beyond the LLM in Step 2.

## Step 3 — A/B resolution (runs as the next step; paid)

After markers are written, continue automatically into the A/B resolution. It
isolates one variable — whether the mined markers are attached as `context` — so
any change in resolution is attributable to the markers.

**3a. Build the two queues (local, free).** One row per contact with usable
markers; control gets a blank `context`, treatment gets the markers.

```bash
uv run --project . python packs/ingestion/primitives/build_resolution_queue/build_resolution_queue.py
```

Writes `…/email-context/ab/queue_control.csv` and `queue_context.csv` (+ manifest
with `contacts_queued`).

**3b. Confirm the spend, then resolve both arms.** This is the only paid part.
Estimate first and confirm with the user before running — `contacts_queued × 2`
Parallel lookups at the processor price (core $0.025, core2x $0.05, pro $0.10 per
lookup). E.g. 414 contacts × 2 = 828 lookups ≈ $41 on core2x. Do **not** pass
`--approve-spend` until the user has approved the estimate.

```bash
# control arm (no markers)
uv run --project . python packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py run \
  --input  .powerpacks/network-import/discover/email-context/ab/queue_control.csv \
  --output-dir .powerpacks/network-import/discover/email-context/ab/control
# treatment arm (with markers as context)
uv run --project . python packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py run \
  --input  .powerpacks/network-import/discover/email-context/ab/queue_context.csv \
  --output-dir .powerpacks/network-import/discover/email-context/ab/treatment
```

**3c. Diff the arms (local, free).**

```bash
uv run --project . python packs/ingestion/primitives/compare_resolution_ab/compare_resolution_ab.py \
  --baseline .powerpacks/network-import/discover/email-context/ab/control/linkedin_resolutions.csv \
  --context  .powerpacks/network-import/discover/email-context/ab/treatment/linkedin_resolutions.csv --open
```

Report the lift from `ab_summary.json` (newly_found / lost / url_changed /
confidence deltas) — the proof that the markers improved resolution.

---
_Created 2026-06-16. Changelog: 2026-06-16 initial version (body-mode default, role-mailbox filter, owner-context prior); 2026-06-17 add `--open` to auto-open markers.csv on macOS; 2026-06-19 hardcode concurrency default to 12 and stop reading `POWERPACKS_OPENAI_CONCURRENCY` (pass `--concurrency` explicitly to raise); 2026-06-19 auto-derive mailbox-owner identity (name + addresses) from msgvault and pass it to the LLM so owner facts are never attributed to a contact (no flag); 2026-06-19 add a phase-1 gate (step 2 aborts on missing/empty/incomplete step-1 context) and a "Reading results — use the schema" guardrail so results are reported from `markers.csv`/`manifest.json`, never fabricated; 2026-06-19 simplify marker schema — drop `is_person`/`relationship`, merge `current_employer`+`past_employer` into one `employers` list column (tagged current/past), and blank `company_guess` for personal/free-provider domains; 2026-06-19 drop the duplicate `canonical_name` marker category (it stays a single top-level column); 2026-06-20 add Step 3 — the A/B resolution now runs automatically as the next step (build_resolution_queue builds control+treatment queues, resolve_linkedin_queue runs both arms, compare_resolution_ab reports the lift), gated by a one-time Parallel spend confirmation._
