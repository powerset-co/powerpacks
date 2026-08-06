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
- `$deep-context review`, "open the people/LinkedIn page", "browse my
  people", "open the directory", "show me the dossiers" -> run only
  `bin/deep-context review`; bare `review` opens the read-only A-Z directory
  (Yes/No tabs, search, full dossier + LinkedIn pane). A stage word opens the
  staged workflow there directly: `$deep-context review linkedin` ->
  `bin/deep-context review linkedin` (likewise `worth` / `enrich`) — sugar for
  the server's `--stage` flag. `review <stage>` (and bare `review`) always
  runs one fixed order: (1) SELF-HEAL first, before touching the server, with
  its progress visible (fresh-fetch re-judge of judge-skipped
  LinkedIn cards + free dead-link termination; a RapidAPI fetch per healed
  candidate plus ~cents of OpenAI judging, no approval stop — invoking review
  is the consent); (2) RESTART the review server — stop any running one
  (review state is in SQLite; nothing is lost), then serve without
  auto-opening a browser; (3) OPEN the staged UI
  as an explicit final step — the wrapper polls the fresh server's /healthz,
  and prints the URL — the wrapper never launches a browser; surface the
  printed URL to the user (open it only if they ask). Before running `review <stage>`, create a
  task list in your harness's todo/task tool with the flow's definitive steps
  — (1) Self-heal, (2) restart server, (3) open the
  staged UI, plus any follow-ups the heal surfaces (e.g. a recovery batch
  offer) — and check each off as the wrapper's output confirms it, STRICTLY IN
  ORDER — the follow-ups item resolves only after the UI is open, even
  when the heal was a no-op — so the user always sees where the flow is
  and nothing is silently skipped.
  NEVER open, navigate to, or surface the review URL before the wrapper
  prints its `review UI:` line — the wrapper owns the browser; the harness
  only mirrors checklist state from wrapper output (the heal step completes
  only when the heal summary JSON line is seen, the open step only when
  `review UI:` appears). Nothing is deferred: in-flight
  enrichment or guided re-research only prints a warning before the restart —
  both are durable (identical guided resubmits reuse projected research;
  enrichment resumes from projected artifacts and its SQLite job receipt).
  `--force-restart` is accepted for
  compatibility but is a no-op — restart is always unconditional.
- `$deep-context heal` -> run only `bin/deep-context heal`: the same
  self-heal pass on its own, idempotent (`--cap N` runaway backstop only).
- `$deep-context refresh`, "resynthesize and show me the directory" -> run only
  `bin/deep-context refresh`; it re-synthesizes stale dossiers (free when facts
  are on the current synthesis contract; a contract bump re-runs everyone and
  the dry estimate prints first — invoking refresh is the approval), rebuilds
  parents, and opens the directory.
- `$deep-context rejudge` -> preview with `bin/deep-context rejudge --dry-run`,
  show the OpenAI estimate, get fresh approval, then run the exact paid command.
  This re-runs synthesis for every Gmail/iMessage/WhatsApp message-backed
  dossier, including mixed-source people and people with an attached LinkedIn.
  It ignores cached machine and human worth for selection, never uses LinkedIn
  as evidence, and never overwrites the human-owned `network_worth` column.
  Both commands first rebuild every raw bundle from the current message stores
  (free, local, no LLM), so a deeper message sync is picked up automatically —
  expect a higher estimate than the original run when history got deeper.
- "Review complete proceed with enrichment" (the phrase the Done screen
  hands the user) -> the review is finished; run
  `bin/deep-context review-status` and continue from its `next_action`
  (normally `realize` -> merge + index).
- `$deep-context restart`, "restart the review", "clear my review decisions",
  "take the staged review again" -> the SMALL reset: clear HUMAN decisions
  only, keep all derived state, review re-takeable immediately (no re-walk).
  Run `bin/deep-context restart` (dry run), show what would clear (worth
  marks, Check-LinkedIn clicks incl. pasted URLs, synthetic approvals),
  confirm, then `bin/deep-context restart --apply` (one SQLite transaction).
  Every machine verdict, facts file, deep-research artifact
  and profile cache survives. Then STOP — no review launch, no workflow plan.
  End by telling the user: run `$deep-context` whenever you're ready.
- `$deep-context clean`, "clean slate", "pipeclean", "start over from
  scratch" -> the BIG reset (full derived-state scrub + reimport walk): load
  and follow `packs/ingestion/skills/clean-slate/SKILL.md` — do not improvise
  the steps here.
- A bare `$deep-context`, "process/resolve/enrich my contacts", "build deep
  context", or a full rerun -> use the complete staged workflow below.

Do not make a user who asked for a single read-only action walk the full build.

## Privacy and approvals

This skill intentionally reads Gmail and iMessage/WhatsApp DM bodies to build
per-person dossiers. Raw samples stay gitignored under
`.powerpacks/deep-context/raw/`; dossiers contain synthesized facts, not verbatim
messages.

- Small iMessage group bodies are included on every run (`--include-groups`)
  under standing owner authorization — never ask, never confirm, and never
  announce it in status copy. WhatsApp group bodies are never read (the
  collector always skips them).
- iMessage collection needs Full Disk Access and may need to run in the user's
  own terminal.
- Never treat memory, an earlier transcript, or an earlier approval as consent
  for OpenAI, Parallel, RapidAPI cache misses, or Modal upload.
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
[Learn] Collect messages and emails for people
[Learn] Approve deep context synthesis cost
[Learn] Build and validate deep context results
[Combine] Resolve people with multiple emails and/or phone numbers
[Combine] Build one record per person
[Heal] Self-heal (runs inside review)
[People] Wait for review to complete
[People] Review people worth adding to network
[Match] Confirm imported LinkedIn matches the person
[Match] App runs enrichment + profile prep after in-UI approval
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
uv run --project . python packs/ingestion/primitives/imports/status.py status
```

`check` is read-only. If `checks.canonical_sqlite.status` is
`migration_required`, run the one explicit compatibility import, then re-run
the check before continuing:

```bash
bin/deep-context migrate-sqlite
bin/deep-context check
```

Do not run migration for a narrow `$deep-context check`; report its
`next_command` and stop. A populated canonical database never imports legacy
artifacts again.

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

Always pass `--include-groups` on every run — do not ask, and do not mention
group inclusion in user-facing status copy (the authorization is standing; see
Privacy and approvals).

Always use the default depth (`--deep-cap 1600`). Do not ask the user about depth
or surface the message cap; only change it if the user explicitly requests a
shallower or deeper pass.

For full processing, candidates are always included:

```bash
bin/deep-context collect --deep-cap 1600 --include-groups [--force]
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
printed by `dry` — do not invent a different scope. Synthesis also produces an
initial `network_worth` recommendation and reason in each
`facts/<parent_id>.jsonl`, then explicitly projects each completed facts payload
into SQLite. The one parent-owned machine worth value and optional human
override are read and written through the same SQLite row.
Normal repeated synthesis rejudges only
missing/Maybe machine verdicts; machine Yes/No and human Yes/No are stable.

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

Identity resolves in tiers, cheapest first, so one human is one record = one
review = one dossier as early and as cheaply as possible.

**Tier 0 — free, deterministic, run it unconditionally.** Identical name plus a
shared phone/email is identity equality; it is settled in code, needs no
approval, and calls no provider:

```bash
bin/deep-context dedupe
```

Report `pairs_deterministic` (merged for free) and `pairs_unsettled` (what only
the judge can decide). It never guesses — a pair it cannot settle is left
unjudged — and it carries forward every merge a paid run already established.

**Tier 1 — the paid LLM judge, for exactly what tier 0 could not settle.**
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

`parents` is free and idempotent — run it after whichever tier you reached, so
the canonical layer always matches the merges that exist. `cluster` always
judges with the LLM (the offline stub is a constructor-only testing seam, no
longer a CLI flag); `dedupe` is the free path.

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
detach, or request human review. One SQL queue admits effective Yes and Maybe
parents but excludes effective No before any hydration or judge call. Reconcile
never judges, refreshes, or writes worth, and
it never sends a person with no attached LinkedIn to the judge (there is nothing
to reconcile) — but those people are still recorded, so a contact-only person
(email/phone only, no LinkedIn) shows up in the review and can be kept or
rejected. They are never queued for paid research; only the worth-gated candidate
path spends on a lookup.

Launch the local UI once in a background terminal:

```bash
bin/deep-context review --stage worth --fresh
```

Every `review <stage>` boot runs the SELF-HEAL pass (`bin/deep-context heal`)
FIRST — before touching the server, with its output streaming, so boot never
looks hung and stale cards fix themselves. It then RESTARTS the review server
(stops any running one, then serves) so the UI
always serves the current code (state is in SQLite; nothing is lost), and
finally OPENS the staged UI once the fresh server answers /healthz. Never skip
the launch because "a server is already up" — a leftover server keeps serving
the stale Python it loaded at startup.

The self-heal pass: (1) a FRESH profile fetch plus re-judge for every
worth-eligible undecided LinkedIn card the judge previously skipped as "no
usable profile" (the same SQL worth gate, normal judge, and write path, so
confirm/detach bars auto-apply), and (2) free termination of confirmed-dead
links — detach plus a free identity stand from an existing synthetic row or
research output, else the person stays a pending re-research card. This spends
real money without pausing: a fresh RapidAPI call per healed candidate plus
OpenAI judge calls (~cents for tens of people). Invoking `review`/`heal` IS the
consent — there is no approval stop; the pre-run count lines are information,
and `--cap` (default 200) is only a runaway backstop. Typical sessions heal a
handful of new cards (the first run after this ships is the big one); a clean
store prints one `[heal] ... (nothing to do)` line and spends nothing. The
summary is written to the stage manifest as display-only metadata. It does not
control `review-status`, whose next action comes only from SQLite queue queries.

Then watch for your turn with the ONE agent-handoff mechanism — a blocking
read of canonical SQLite (no daemons, no sockets, no thread ids; it always
works in any harness):

```bash
bin/deep-context review-status --wait --timeout 900
```

It queries canonical SQLite once a second and returns the current queue-derived
`next_action`: pending worth parents -> `review_people`; uncovered effective-Yes
parents -> `enrich`; pending LinkedIn candidates -> `review_linkedin`; otherwise
`realize`. The app itself runs everything in between:
preview, approved enrichment, from-cache continuation, synthetic assembly,
and profile prefetch. On timeout the wait returns `status: waiting` with the
current human-wait action — just run it again. Mark
`[People] Wait for review to complete` complete once the
first wait is running.

The UI is the user's control surface for review and approval. It records choices
in canonical SQLite. Enrichment writes its fixed artifacts, projects their full
payloads into SQLite, and writes a display-only manifest receipt. The agent owns workflow control:
run the wait command, then run only the exact `next_action` it returns, then
wait again. Never infer readiness from chat text or browser state. Direct
progress-step navigation is preview only; it does not itself advance provider
work. A clicked preview stage stays visible and keeps refreshing from database
changes instead of being forced back to the actual workflow stage.
The browser observes SQLite through the existing HTTP API and automatically
refreshes or moves to the current stage. People and LinkedIn decisions commit
directly to SQLite, and each save returns the new state token. No status poll is
part of a decision click.
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
When the final Maybe is answered, the server writes People completion
automatically. The completion endpoint does not reject unresolved Maybes, but
the UI adds no separate skip control. The browser then opens Enrich Contacts,
where an indeterminate "Preparing enrichment" bar remains visible until the
next projected SQLite state arrives.

The wait command is the read-only deterministic primitive — it queries SQLite
and emits one `next_action`; it does not mutate files, open a
browser, shell out, or call a network. Follow only that exact action. A bare
`bin/deep-context review-status` (no `--wait`) prints the same contract once
for a quick look.

The fixed runtime record and display receipt are:

```text
.powerpacks/deep-context/deep-context.sqlite
.powerpacks/deep-context/reconcile/deep-research/manifest.json
```

Selection and reuse come from the current SQLite worth/candidate rows plus
projected artifact fingerprints. Nothing reads the manifest to determine what
is pending, current, or allowed to run.

### 6. Identity preparation and one lookup — THE APP RUNS THIS

The review app runs the whole mid-flow itself, in-process, when the user acts:

- **People review completes** → the app builds the free preview
  (`reconcile-deep-research --dry-run`) and the Enrich Contacts page renders
  the exact `Approve $X.XX` estimate (gross eligible, completed-result reuse,
  net-new submissions, budget). When net-new is zero it continues from cache
  immediately — no approval exists for zero dollars.
- **The user clicks Approve $X.XX** → that click IS the spend approval: the
  app runs the approved Parallel pass with exactly that budget cap.
- **Research completes** → the app chains the free follow-ups automatically:
  `assemble-synthetic` (no-LinkedIn cards) and `profile-prefetch --fetch`
  (cached profiles + nano summaries; pennies).

The agent runs NONE of these steps while the app owns them. Files remain the
durable provider outputs, but the writer projects every downstream payload into
SQLite before success. The `jobs` table is the sole async progress/error receipt
and paid-run double-submit guard. The manifest is write-only display metadata.
The manual commands remain available for headless/broken-UI recovery only.

The lookup wrapper and its provider child update one SQLite job receipt and
overwrite the fixed enrichment manifest with counts/timing/error metadata.
Nothing reads that manifest. The current queue CSV is a write-only export;
selection and synthetic assembly query SQLite, so stale rows cannot reappear.

When you report lookup progress to the user, phrase it as "Parallel tasked with
N net-new lookups" and use the SQLite job receipt's running/completed counts. Do not call
the approved budget a "cap" or restate the dollar amount in status updates — the
approval already happened, so the number is noise.

### 7. LinkedIn decision gate

When enrichment is complete, Enrich Contacts shows a checkmark and Continue.
That compatibility click opens Check LinkedIn; it does not create stage state
or start work. The first review server stays alive.

For a found/existing LinkedIn the question is simply whether it is the right
person. Yes verifies it. No only opens the correction panel and is not a
decision. The correction panel accepts a replacement URL or a terminal Skip;
Skip writes a detach decision, rejects the shown/proposed LinkedIn, and leaves
the person out of the index for now. A synthetic result has the same two
outcomes: paste the LinkedIn URL to create an approved retarget, or Skip it.
Synthetic rows are never directly approved for indexing.

Continue through the wait loop. Continue to realization only when
`bin/deep-context review-status --wait` returns `next_action == "realize"`.
A LinkedIn page opened directly before current enrichment completes remains a
read-only waiting view.

### 8. Apply and realize

Stop the review UI first so realization is not competing with an in-process
enrichment job. SQLite transactions serialize the writes without auxiliary
runtime state.

Machine-cleared retargets attempt hydration when the judge records them. A
human-pasted or human-fixed retarget may have no cached profile and projects
from its SQLite carry instead. Applying and realizing are still local,
paid-free projections and need no provider approval:

```bash
bin/deep-context stop
bin/deep-context apply-retargets
bin/deep-context realize
```

`apply-retargets` and `realize` make no network calls. `realize` rebuilds
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
.powerpacks/deep-context/reconcile/deep-research/manifest.json  display-only stage receipt
.powerpacks/deep-context/deep-context.sqlite      canonical runtime state
.powerpacks/deep-context/review/avatars/          locally cached live profile images
.powerpacks/network-import/overrides/review.csv   explicit compatibility export baton
.powerpacks/network-import/overrides/retarget-people.csv
.powerpacks/network-import/overrides/synthetic-people.csv  explicit compatibility export baton
.powerpacks/network-import/merged/people.csv
```

The product/algorithm detail remains in
`packs/ingestion/docs/deep-context-pipeline.md`; read it only when diagnosing a
failed primitive or changing implementation behavior.
