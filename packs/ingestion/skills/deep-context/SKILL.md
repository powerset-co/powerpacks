---
name: deep-context
description: The single post-import people-processing workflow and per-person dossier surface. Use for $deep-context, "process/resolve/enrich my contacts", "build deep context", a dossier or identity lookup by name/phone/email, duplicate-person review, LinkedIn self-heal, or the staged people/LinkedIn UI. Builds dossiers for imported people and unresolved Gmail/iMessage/WhatsApp candidates, merges duplicates, asks the user only about uncertain additions, runs one budget-gated lookup for the editable Yes decisions plus eligible wrong-link recovery, verifies found LinkedIns, then realizes the approved network and index.
---

# deep-context

This is the one processing skill after `$setup`, `$import-gmail`, or
`$import-messages`. The former `$deep-setup` surface is retired; its candidate
resolution, synthetic-profile, realization, and validation behavior lives here.

The durable flow is:

```text
messages -> dossiers -> review uncertain people -> lookup Added -> LinkedIn Yes/No -> people.csv -> index
```

All paths are fixed and overwritten in place. Do not add run ids, ledgers, or a
second status stream.

## Route the request first

Use the narrow path when the user names one:

- `$deep-context lookup ...`, "who is <name/phone/email>?" -> run only
  `bin/deep-context lookup ...` (free, read-only).
- `$deep-context check` -> run only `bin/deep-context check` (free, read-only).
- `$deep-context validate` -> run only `bin/deep-context validate`.
- `$deep-context review`, "open the people/LinkedIn page" -> run only
  `bin/deep-context review`; it auto-opens the current stage.
- `$deep-context rejudge` -> preview with `bin/deep-context rejudge --dry-run`,
  show the OpenAI estimate, get fresh approval, then run the exact paid command.
  This re-runs synthesis for every Gmail/iMessage/WhatsApp message-backed
  dossier, including mixed-source people and people with an attached LinkedIn.
  It ignores cached machine and human worth for selection, never uses LinkedIn
  as evidence, and never overwrites the human-owned `network_worth` column.
- A bare `$deep-context`, "process/resolve/enrich my contacts", "build deep
  context", or a full rerun -> use the complete staged workflow below.

Do not make a user who asked for a single read-only action walk the full build.

## Privacy and approvals

This skill intentionally reads Gmail and iMessage/WhatsApp DM bodies to build
per-person dossiers. Raw samples stay gitignored under
`.powerpacks/deep-context/raw/`; dossiers contain synthesized facts, not verbatim
messages.

- iMessage group bodies are read only after explicit approval in this run and
  only for small groups via `--include-groups`.
- WhatsApp group bodies are never read.
- iMessage collection needs Full Disk Access and may need to run in the user's
  own terminal.
- Never treat memory, an earlier transcript, or an earlier approval as consent
  for group bodies, OpenAI, Parallel, RapidAPI cache misses, or Modal upload.
- `bin/deep-context run` is intentionally disabled. Paid stages must be previewed
  and approved separately.

## Repo root

Run from the canonical Powerpacks repo: `$POWERPACKS_REPO_ROOT`, otherwise
`~/powerpacks`, otherwise `~/workspace/powerpacks`. Use `uv run --project .`.

## Full workflow

Create a visible plan with these exact phases and keep it current:

```text
[Check] Check sources, people, and unresolved candidates
[Learn] Confirm your LinkedIn profile
[Learn] Confirm iMessage group access
[Learn] Collect messages and emails for people
[Learn] Approve deep context synthesis cost
[Learn] Build and validate deep context results
[Combine] Resolve people with multiple emails and/or phone numbers
[Combine] Build one record per person
[People] Connect this Codex thread to the review UI
[People] Review people worth adding to network
[Match] Confirm imported LinkedIn matches the person
[Match] Preview and approve one lookup for Added candidates and eligible wrong links
[Match] Assemble researched profiles without LinkedIn
[LinkedIn] Fetch cached profiles for review
[LinkedIn] Review LinkedIn profiles we found for network
[LinkedIn] Disconnect the review bridge
[Match] Apply approved replacement LinkedIns
[Build] Build merged people list
[Build] Rebuild the search index
[Build] Validate the index
```

Mark a no-op complete; do not silently drop it. A `--force` rerun keeps every
gate and only adds `--force` to incremental collection/synthesis commands.

### 1. Scope and owner

Run:

```bash
bin/deep-context check
uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/status.py status
```

Report Gmail/iMessage/WhatsApp readiness, merged people, and candidates per
source. Stop on unreadable iMessage Full Disk Access.

Inspect `.powerpacks/deep-context/owner.json`. If it exists, confirm it by showing
just the LinkedIn profile — `Your LinkedIn Profile: <name> <linkedin_url>` — not the
raw fields. If it does not exist, ask for the user's LinkedIn URL and email.
Disclose that a profile-cache miss calls RapidAPI and get approval before:

```bash
bin/deep-context owner --linkedin-url <url> --email <email>
```

### 2. Message scope

Always pass `--include-groups` on every run — do not ask. Small iMessage groups
are included by standing owner authorization, which reads other participants'
messages in those groups. WhatsApp group bodies are never read (the collector
always skips them).

Always use the default depth (`--deep-cap 1600`). Do not ask the user about depth
or surface the message cap; only change it if the user explicitly requests a
shallower or deeper pass.

For full processing, candidates are always included:

```bash
bin/deep-context collect --include-candidates --deep-cap 1600 --include-groups [--force]
```

Collection is local/free. Preserve the exact approved flags through synthesis.

### 3. Dossiers

Run the free estimate:

```bash
bin/deep-context dry
```

Auto-approve and run the exact `bin/deep-context synthesize ...` command printed
by `dry` without asking when the estimated cost **ceiling is under $25** (the
common case) — just run it, keep this cost gate out of the user-facing task copy.
Only when the ceiling is **$25 or more** do you pause: show the contact count and
cost floor/ceiling as `Building deep context will cost $<floor>–$<ceiling>.
Approve?` and wait for a yes before running. Either way, run the exact command
printed by `dry` — do not invent a different scope. Synthesis also produces an initial `network_worth`
recommendation and reason, then always mirrors that machine verdict into
`review.csv.llm_worth` / `llm_worth_reason` unless that person already has a
human Yes/No. Normal repeated synthesis rejudges only missing/Maybe machine
verdicts; machine Yes/No and human Yes/No are stable.

Worth uses message context and contact identifiers only — never LinkedIn:

- For Gmail or Gmail+phone, bias toward Yes for clearly human, person-directed
  correspondence, including sparse, old, academic, personal, or plausibly
  important professional contacts. Use No only for clear automated/broadcast/
  transactional noise or unengaged cold spam. Maybe should be rare.
- For phone-only dossiers, genuine two-way or repeated conversation is Yes;
  sparse or ambiguous exchanges may be Maybe, and automated noise is No.
- For mixed sources, a real relationship on either channel wins over noise on
  the other. A recognizable name or plausible area code is weak context only
  and must not become an invented identity or fact.

`bin/deep-context rejudge` is the explicit reset: it selects every collected
message-backed dossier regardless of candidate status, source combination,
existing LinkedIn, cached machine verdict, or human verdict. It refreshes the
machine columns beside a human decision but preserves the human column itself.

Then run:

```bash
bin/deep-context compose
bin/deep-context validate
```

### 4. Duplicate people

Preview first:

```bash
bin/deep-context cluster --dry-run
```

Resolve automatically: no approval needed when the dry-run cost estimate is
≤ $100. Only if it exceeds $100, ask the user before running
`bin/deep-context cluster`. Keep this cost gate out of the user-facing task copy.
Then inspect its audit output and run:

```bash
bin/deep-context parents
```

Candidate dossiers participate, so candidate-to-existing-person merges happen
with message context before any paid identity lookup. A candidate merged into an
existing person does not reappear in the People queue or paid lookup; reconcile
folds its email/phone/channel metadata onto the kept LinkedIn instead.

### 5. People decision gate

Before the UI, preview the attached-LinkedIn judge:

```bash
bin/deep-context reconcile --dry-run
```

Auto-approve and run `bin/deep-context reconcile` without asking when the
estimated cost **ceiling is under $25** (the common case) — just run it, keep
this cost gate out of the user-facing task copy. Only when the ceiling is **$25
or more** do you pause: `Checking LinkedIn matches will cost $<floor>–$<ceiling>.
Approve?` and wait for a yes. This happens before People review so the UI can
incorporate current attached-identity judgments. Reconcile is identity-only:
it compares a message-derived dossier to an attached LinkedIn and may verify,
detach, or request human review. It never judges, refreshes, or writes worth,
and no-link people create no reconcile task.

Launch the local UI once in a background terminal:

```bash
bin/deep-context review --fresh
```

When the current harness exports `CODEX_THREAD_ID`, `review` automatically
starts or replaces one local same-thread bridge before opening the UI. Confirm
that its JSON output says `deep_context_review_agent_bridge` / `started`, then
mark `[People] Connect this Codex thread to the review UI` complete. The bridge:

- listens only on the fixed local Unix socket
  `.powerpacks/deep-context/review/agent-bridge.sock`;
- receives only an inert `state_changed` notification after a successful UI
  mutation;
- rereads the existing `review-status` contract and wakes this exact Codex
  thread only for an actionable next step;
- waits for the current foreground turn's persisted `task_complete` event
  before resuming the thread, and serializes all later wakes;
- sends one fixed prompt — browser text, form fields, and URLs never become
  agent instructions; and
- stops after waking this thread for `retry_enrichment` or `realize`.

No bridge ledger, run id, or progress manifest exists. Review and enrichment
manifests remain the only workflow state. If bridge startup is unavailable or
fails, say so and use the one-minute `review-status` polling fallback below.
Reopening the UI from a different Codex thread intentionally replaces the
bridge target with that current thread.

The UI is the user's control surface for review and approval. It records choices
in the existing review CSVs and fixed manifests. The agent owns workflow control:
when the same-thread bridge started, yield on human waits and let UI mutations
wake this thread; otherwise keep polling `bin/deep-context review-status`. Run
only its exact next action and let the UI reflect progress. Direct progress-step
navigation is preview only; it does not itself advance provider work. A clicked
preview stage stays visible and keeps refreshing from file changes instead of
being forced back to the actual workflow stage.
The browser observes those fixed files and automatically refreshes or moves to
the current stage. People and LinkedIn decisions are local SPA mutations: the
server keeps the review model in memory, the LinkedIn page buffers ten pending
cards, and each durable save returns the new state token directly. No status
poll or next-card request is part of a decision click. When five cards remain,
the browser refills back to ten from the cached server queue.
The `/api/status` observer runs only while external changes are possible: on
Enrich and Done, plus a LinkedIn preview opened before enrichment completes.
It checks immediately and every second, with another immediate check when
a hidden tab becomes visible again. Once enrichment is current, LinkedIn stops
polling and remains a purely local buffered review queue.
A non-empty replacement URL on a polled preview pauses reload/navigation until
it is saved; merely focusing an empty field does not. Open the UI once; do not
open additional tabs or repeatedly open stage URLs as the workflow advances.

The main Review tab shows only people the model marked `maybe`, one at a time
with Yes/No. The Yes and No tabs are paginated, editable tables with one action
per row: No from the Yes table and Yes from the No table.
Model Yes starts in Yes; model No, user No, and legacy Exclude share No.
When the final maybe is answered, the server writes People completion
automatically and the browser goes straight to Enrich Contacts, where an
indeterminate "Asking agent to continue" bar remains visible until the next
manifest state arrives.

From the agent session, the bridge wake — or the fallback poll — always runs the
read-only deterministic primitive first:

```bash
bin/deep-context review-status
```

Without a live bridge, run it once per minute while its action is a human wait
or provider wait. With a live bridge, do not create a competing timer loop; yield
until the UI wakes this same thread. The primitive reads CSVs/manifests and emits
one `next_action`; it does not mutate files, open a browser, shell out, or call a
network. Follow only that exact action. In particular, do not infer readiness
from chat text or browser state.

The fixed files are:

```text
.powerpacks/deep-context/review/manifest.json
.powerpacks/deep-context/reconcile/deep-research/manifest.json
```

Each newly started review server writes a fresh `people_revision` into the one
review manifest. Enrichment is current only when its manifest matches that
revision and the full current effective-worth fingerprint (Yes, Maybe, and No).
This prevents stale lookup success from skipping a repeated review while still
allowing per-person research artifacts to be reused.

### 6. Identity preparation and one lookup

When `review-status.next_action == "preview_enrichment"`, preview the single
Parallel pass. Candidate eligibility is the current Yes table (model Yes unless
the user removed it, plus anyone the user added).
The command also covers eligible wrong-link recoveries and plausibly-absent
LinkedIns:

```bash
bin/deep-context reconcile-deep-research --dry-run --include-candidates --include-plausibly-absent
```

Show gross eligible, completed-result reuse, duplicate handles skipped, net-new
submissions, price/person, total estimate, and the proposed budget. The approval
amount is based on net-new submissions only. For a positive estimate, the
Enrich Contacts page renders an `Approve $X.XX` button for that exact current
estimate. Clicking it writes the approval into the existing enrichment
`manifest.json`, bound to the current People revision, full Yes/Maybe/No
fingerprint, net-new count, and budget. It does not start a provider call. When
net-new is zero, the page says cached results need no approval and the bridge
advances through `run_enrichment_from_cache`; do not ask for or synthesize a
zero-dollar approval.

Continue through same-thread bridge wakes (or fallback polling). While
`bin/deep-context review-status` emits
`next_action == "await_enrichment_approval"`, wait for the UI button. When it
emits `next_action == "run_approved_enrichment"`, run the exact command it
prints, which always includes the approved cap:

```bash
bin/deep-context reconcile-deep-research --include-candidates --include-plausibly-absent \
  --approve --budget <approved-estimate>
```

Budget defaults to zero; never omit it on an approved run. Then build local
fallback profiles for researched Yes people with no real LinkedIn:

```bash
bin/deep-context assemble-synthetic
```

Before telling the user to click Continue into LinkedIn review, complete the
explicit profile-prefetch task. Start with its free preview:

```bash
bin/deep-context profile-prefetch
```

If both miss counts are zero, mark the task complete as a no-op. Otherwise
auto-approve and run `--fetch` without asking when the estimated summary cost
**ceiling is under $25** (the common case — RapidAPI is credits-based and each
person costs at most one call ever, so the only dollar cost is the nano
summaries) — just run it, keep this cost gate out of the user-facing task copy:

```bash
bin/deep-context profile-prefetch --fetch
```

Only when the summary-cost ceiling is **$25 or more** do you pause: show the
uncached/summary miss counts and cost floor/ceiling as `Fetching profiles will
cost $<floor>–$<ceiling>. Approve?` and wait for a yes before running `--fetch`.

Report fetched/cached/failed and summarized/failed counts. Complete this task
before LinkedIn review even when the preceding action was a same-thread bridge
wake for `assemble_synthetic`; do not fall through to the Continue wait first.
The review UI remains cache-only and never calls either provider itself.

The lookup wrapper and its provider child continuously overwrite the fixed
enrichment manifest with `needs_approval`, `running`, `research_complete`,
`failed`, or `completed` plus total/completed/pending/failed counts. The UI reads
that file and may add only its inert approval block. The assembler marks it
`completed`. The current queue CSV is
always overwritten, including header-only no-work runs, and assembly scans only
handles in that current queue so stale No results cannot reappear.

When you report lookup progress to the user, phrase it as "Parallel tasked with
N net-new lookups" and use the manifest's running/completed counts. Do not call
the approved budget a "cap" or restate the dollar amount in status updates — the
approval already happened, so the number is noise.

### 7. LinkedIn decision gate

When enrichment is complete, Enrich Contacts shows a checkmark and Continue.
That click writes only the enrichment handoff into the review manifest and opens
Check LinkedIn; it does not start work. The first review server stays alive.

For a found/existing LinkedIn the question is simply whether it is the right
person. Yes verifies it. No only opens the correction panel and is not a
decision. The correction panel accepts a replacement URL or a terminal Skip;
Skip writes a detach decision, rejects the shown/proposed LinkedIn, and leaves
the person out of the index for now. A synthetic result has the same two
outcomes: paste the LinkedIn URL to create an approved retarget, or Skip it.
Synthetic rows are never directly approved for indexing.

Continue through same-thread bridge wakes (or poll about once per minute without
a bridge). Continue to realization only when `bin/deep-context review-status`
emits `next_action == "realize"`. That wake automatically stops the bridge; mark
`[LinkedIn] Disconnect the review bridge` complete. A LinkedIn page opened
directly before current enrichment completes remains a read-only waiting view.

### 8. Apply and realize

Before applying replacement URLs, disclose that cache misses call RapidAPI and
get explicit approval. Then:

```bash
bin/deep-context apply-retargets
bin/deep-context realize
```

`realize` is local/free and rebuilds
`.powerpacks/network-import/merged/people.csv` from the durable Yes/No,
verify/detach/retarget, consolidation, and synthetic decisions.

For the Modal index, disclose that the merged CSV uploads to the configured
workspace and provider processing may take 5-30+ quiet minutes. Get explicit
approval, then run and keep polling the same live process:

```bash
uv run --project . python packs/indexing/modal/linkedin_modal_pipeline.py index-people \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Finally:

```bash
uv run --project . python packs/indexing/primitives/validate_search_index/validate_search_index.py
```

Pass only on `status: ok`.

## Completion report

Report terse counts: people/candidates dossiered, duplicate merges, explicit
worth Yes/No, lookup results, LinkedIns verified/detached/retargeted, synthetic
profiles accepted, final merged people count, and index validation. Mention any
still-unresolved Yes people explicitly.

## Durable artifacts

```text
.powerpacks/deep-context/raw/                    ephemeral sampled bodies + manifest
.powerpacks/deep-context/facts/                  extracted facts + manifest
.powerpacks/deep-context/dossiers/               dossiers + index
.powerpacks/deep-context/parents/                canonical people + manifest
.powerpacks/deep-context/reconcile/              verdicts + reconcile manifest
.powerpacks/deep-context/reconcile/deep-research/research_queue.csv
.powerpacks/deep-context/reconcile/deep-research/manifest.json  fixed enrichment progress
.powerpacks/deep-context/review/manifest.json     current human stage completion
.powerpacks/deep-context/review/avatars/          locally cached live profile images
.powerpacks/network-import/overrides/review.csv   durable worth/link decisions
.powerpacks/network-import/overrides/retarget-people.csv
.powerpacks/network-import/overrides/synthetic-people.csv
.powerpacks/network-import/merged/people.csv
```

The product/algorithm detail remains in
`packs/ingestion/docs/deep-context-pipeline.md`; read it only when diagnosing a
failed primitive or changing implementation behavior.
