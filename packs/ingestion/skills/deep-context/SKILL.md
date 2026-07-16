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
- `$deep-context re-review` -> preview with `bin/deep-context re-review --dry-run`,
  show the OpenAI estimate, get fresh approval, then run the exact paid command.
  This refreshes only dossier-backed machine Maybe/unjudged import candidates.
  Machine Yes/No is reused, and every human Yes/No remains authoritative.
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
[People] Review people worth adding to network
[Match] Confirm imported LinkedIn matches the person
[Match] Preview and approve one lookup for Added candidates and eligible wrong links
[Match] Assemble researched profiles without LinkedIn
[LinkedIn] Review LinkedIn profiles we found for network
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

Ask one question, phrased exactly: "Include iMessage groups? WhatsApp groups are
always skipped." Default is DM-only; `--include-groups` also reads other
participants' messages in small iMessage groups. WhatsApp group bodies are never
read.

Always use the default depth (`--deep-cap 1600`). Do not ask the user about depth
or surface the message cap; only change it if the user explicitly requests a
shallower or deeper pass.

For full processing, candidates are always included:

```bash
bin/deep-context collect --include-candidates --deep-cap 1600 [--include-groups] [--force]
```

Collection is local/free. Preserve the exact approved flags through synthesis.

### 3. Dossiers

Run the free estimate:

```bash
bin/deep-context dry
```

Show its contact count and cost floor/ceiling. Get explicit approval, then run
the exact `bin/deep-context synthesize ...` command printed by `dry`. Do not
invent a different scope. Synthesis also produces an initial `network_worth`
recommendation and reason. Reconcile refreshes only machine Maybe/unjudged
recommendations before People review; machine Yes/No is stable.

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

Show the OpenAI estimate, get fresh approval, then run
`bin/deep-context reconcile`. This happens before People review so the UI can
incorporate the current spam and attached-identity judgments without another
hidden stage later. The dry run reports attached-profile identity tasks and
worth-only no-LinkedIn import-candidate tasks separately. Reconcile judges both
in the same approved pass.

Worth is deliberately decisive:

- Yes for a genuine human relationship, including family/relatives, friends, classmates,
  professors/teachers/mentors, alumni/school contacts, colleagues, and other
  real personal or professional correspondence.
- No for automated/broadcast mail, marketing, cold sales/recruiting/agency
  outreach without meaningful engagement, spam, and purely transactional
  service/vendor exchanges.
- Maybe only when the evidence is genuinely balanced about whether a real
  relationship exists. Missing professional prestige, job details, or seniority
  is not a reason for Maybe.

On every repeated full `$deep-context`, this reconcile pass refreshes only the
machine-owned Maybe/unjudged tail even when existing dossier facts are reused.
Machine Yes/No is not re-judged. A human-owned `network_worth` Yes/No in
`review.csv` always wins and is never overwritten.
The narrower `bin/deep-context re-review` command applies the same rule and
automatically rebuilds the free parent layer first when a newer `compose` pass
has replaced the shared lookup index.

Launch the local UI once in a background terminal:

```bash
bin/deep-context review --fresh
```

The UI is the user's control surface for review and approval. It records choices
in the existing review CSVs and fixed manifests. The agent owns workflow control:
keep polling `bin/deep-context review-status`, run only its exact next action, and
let the UI reflect progress. Direct progress-step navigation is preview only; it
does not itself advance provider work.
The browser observes those fixed files and automatically refreshes or moves to
the current stage. Open it once; do not open additional tabs or repeatedly open
stage URLs as the workflow advances.

The main Review tab shows only people the model marked `maybe`, one at a time
with Yes/No. The Yes and No tabs are paginated, editable tables with one action
per row: No from the Yes table and Yes from the No table.
Model Yes starts in Yes; model No/spam, user No, and legacy Exclude share No.
When the final maybe is answered, the server writes People completion
automatically. Continue only changes the visible page to Enrich Contacts.

From the agent session, poll the read-only deterministic primitive:

```bash
bin/deep-context review-status
```

Run it once per minute while its action is a human wait or provider wait. It
reads CSVs/manifests and emits one `next_action`; it does not mutate files, open
a browser, shell out, or call a network. Follow only that exact action. In
particular, do not infer readiness from chat text or browser state.

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
amount is based on net-new submissions only. The Enrich Contacts page renders an
`Approve $X.XX` button for that exact current estimate. Clicking it writes the
approval into the existing enrichment `manifest.json`, bound to the current
People revision, full Yes/Maybe/No fingerprint, net-new count, and budget. It
does not start a provider call.

Keep polling `bin/deep-context review-status`. While it emits
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

Keep polling `bin/deep-context review-status` about once per minute. Continue to
realization only when it emits `next_action == "realize"`. A LinkedIn page opened
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
