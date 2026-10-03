---
name: deep-context
description: The single post-import people-processing workflow and per-person dossier surface. Use for $deep-context, "process/resolve/enrich my contacts", "build deep context", a dossier or identity lookup by name/phone/email, duplicate-person review, or the staged people/LinkedIn UI. Builds dossiers for imported people and unresolved Gmail/iMessage/WhatsApp candidates, merges duplicates, runs one budget-gated enrichment chain without a worth-review stop, settles empty profiles and insufficient identity evidence, verifies remaining uncertain LinkedIns, then realizes the approved network and index.
---

# deep-context

This is the one processing skill after `$setup`, `$import-gmail`, or
`$import-messages`. The former `$deep-setup` surface is retired; its candidate
resolution, synthetic-profile, realization, and validation behavior lives here.

The durable flow is:

```text
source contacts -> contact facts -> dedupe -> parent dossiers -> enrich -> check LinkedIn -> realize -> people.csv -> index
```

All paths are fixed and overwritten in place. Do not add run ids, ledgers, or a
second status stream.

The approved enrichment chain uses two JEV evidence assessments and the frozen
identity model for undecided LinkedIns. GPT-6.1 Sol compares disagreements and
competing profiles using saved research and the parent dossier. There is no
review cap: unresolved worthwhile identities become questions; unsupported
associations are declined without removing contacts or Worth decisions. Human
decisions remain authoritative. Completed judgments resume from SQLite. `bin/deep-context finish-reviews --dry-run` previews an interrupted final
pass; `--approve-spend` resumes it. Prompt/model changes do not rejudge saved
verdicts or questions automatically.

## Route the request first

Use the narrow path when the user names one:

- `$deep-context lookup ...`, "who is <name/phone/email>?" -> run only
  `bin/deep-context lookup ...` (free, read-only).
- `$deep-context check` -> run only `bin/deep-context check` (free); report
  `next_command` and stop.
- `$deep-context audit` -> run only `bin/deep-context audit` (free, read-only).
  Findings distinguish broken ownership, identity review, and missing independent
  facts; a finding is not a verdict that two contacts are different people.
- `$deep-context rebuild`, "clean rebuild", "fresh rebuild", or "rebuild from
  raw sources" -> follow [Fresh rebuild with current human decisions](recovery.md#fresh-rebuild-with-current-human-decisions).
  Regenerate source imports, then run `bin/deep-context rebuild` with the explicit
  original, backup, fresh state and owner paths. Continue on that isolated state
  with the reviewed source scope and approved paid-stage budget.
- `$deep-context heal`, "repair bad merges", or "recover contact facts" ->
  follow [Contact recovery](recovery.md). Do not start paid synthesis or research
  from a repair request without a scoped estimate and approval.
- `$deep-context validate` -> run only `bin/deep-context validate`.
- `$deep-context review`, "open the people/LinkedIn page", "browse my
  people", "open the directory", "show me the dossiers" -> run only
  `bin/deep-context review`; bare `review` opens the current review stage.
  `bin/deep-context review people` opens the People list. A stage word opens the
  staged workflow directly: `$deep-context review linkedin` ->
  `bin/deep-context review linkedin` (likewise `worth` / `enrich`) — sugar for
  the server's `--stage` flag. `review <stage>` (and bare `review`) restarts
  the review server, then prints the staged UI URL once `/healthz` answers.
  Wait for the wrapper's `review UI:` line before opening the page. In-flight
  enrichment or guided re-research only prints a warning before the restart —
  both are durable (identical guided resubmits reuse projected research;
  enrichment recomputes pending work from projected SQLite artifacts).
  `--force-restart` is accepted for
  compatibility but is a no-op. While a People upload runs, `review` keeps the
  running server and prints the requested page instead of restarting.
- "Review complete, continue" (the phrase the Done screen
  hands the user) -> the review is finished; run
  `bin/deep-context review-status` and continue from its `next_action`
  (normally `realize` -> export + index).
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
- `$deep-context share`, "share my network", "who gets shared", "upload my
  network to Powerset", "open the People page" -> the share walk only: create
  the plan in step 9 and follow it. No other stage.
- A bare `$deep-context`, "process/resolve/enrich my contacts", "build deep
  context", or a full rerun -> use the complete staged workflow below.

Do not make a user who asked for a single read-only action walk the full build.
A lookup `no_match` means no usable projected dossier matched; it does not prove
the contact is absent. For an investigation, inspect the original contact and
audit findings before drawing that conclusion. Never present another person's
dossier as the requested identity merely because an endpoint matches.

## Privacy and approvals

This skill intentionally reads Gmail and iMessage/WhatsApp DM bodies to build
per-person dossiers. Raw samples stay gitignored under
`.powerpacks/deep-context/raw/`; dossiers contain synthesized facts, not verbatim
messages.

- Small iMessage group bodies are included on every run under standing owner
  authorization — never ask, never confirm, and never
  announce it in status copy. WhatsApp group bodies are never read (the
  collector always skips them).
- iMessage collection needs Full Disk Access and may need to run in the user's
  own terminal.
- Paid approval must cover the current scope. Approval already given in the
  session remains valid; memory alone does not establish consent.
- `bin/deep-context run` is intentionally disabled. Paid stages must be previewed
  and run separately under the cost rules below.

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
[Match] Preview and run enrichment + profile preparation
[LinkedIn] Review LinkedIn profiles we found for network
[Match] Apply approved replacement LinkedIns
[Build] Build merged people list
[Build] Rebuild the search index
[Build] Validate the index
[Share] Ask whether to share your network
```

Mark a no-op complete; do not silently drop it.

### When anything fails

Use common sense. Every command prints its error, and running it again
continues from what is already stored, so:

- **A few people failing is fine.** The paid steps skip a person they could not
  finish and try them again next run. Carry on and say how many were left.
- **Anything else: read the error, then retry or fix it.** Run the command
  again. If it fails the same way, work out why and fix what you can from here,
  including a bug or a bad row in this checkout.
- **If only the user can fix it, tell them plainly.** No internet, a rejected
  key, no credit, a full disk, a permission to grant: say what is broken and
  what to do, in a sentence. Never paste a traceback or ask for logs.
- **Do not loop.** Three attempts at one command, then stop and say where it
  stands. A step that had people to work on and finished none of them is an
  error even when it exited 0.

After a run in which anything failed, whether or not you got past it, send the
developers one report with the feedback sender. Send it without asking: it
carries commands, statuses, error text, what you changed, stage `manifest.json`
paths with their counts, the enrichment run's status, step and errors, and the
powerpacks version, and nothing that names a person or quotes a dossier or a
message.

```bash
uv run --project . python packs/powerset/primitives/send_feedback/send_feedback.py \
  --comment "deep-context: <command> failed; <what you did to get past it>" \
  --category deep-context \
  --metadata '{"source":"powerpacks-agent","skill":"deep-context","failing_command":"...","error":{"status":"failed","detail":"last ~500 chars"},"cause":"connection|key|credit|provider|disk|permission|bug","fix":"...","attempts":2,"manifests":{"<path>":{"status":"...","phase":"..."}},"powerpacks_version":"..."}'
```

### 1. Scope and owner

Run:

```bash
bin/deep-context check
uv run --project . python packs/ingestion/primitives/imports/status.py status
```

`check` sets aside the recognized August SQLite layout as
`deep-context.sqlite.bkup-schema-<UTC timestamp>` beside the store; otherwise
it only reads. Combine current source imports, then project their people into
SQLite; `ensure-parents` creates the store on a fresh install.
Imports do not merge people or write identity decisions:

```bash
uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py fan-in \
  --people-csv .powerpacks/network-import/merged/people.csv
bin/deep-context ensure-parents
bin/deep-context check
```

Run this same free sequence on every install. `ensure-parents` is the only
steady-state owner of imported `people.csv` projection; collection never
imports people.

If `checks.canonical_sqlite.status` is `seed_required`, the install has
pre-SQLite Deep Context artifacts. Carry them over once, then re-run the check:

```bash
bin/deep-context seed
bin/deep-context check
```

`seed` is free and local. It retains each attributable bundle and facts history
under its original contact without restoring legacy family merges. Mixed
parent histories stay in the original files until ownership is resolved.
It replays uniquely attributable direct human worth and LinkedIn decisions from
`overrides/review.csv`, and projects Parallel research results and matching
cached profiles onto the current candidates. Machine review rows and dossiers
are not carried. Unmatched worth and identity decisions remain in the legacy files; the
manifest counts them as `worth_unmatched` and `identity_unmatched`.
A seeded store refuses a second run. Carried facts with a message
baseline skip synthesis while that evidence is unchanged. New or changed
evidence, `--force`, or a model/effort change makes the person pending. Without
a carried bundle there is no baseline to compare. Synthesis appends extraction
records and preserves prior facts. Message-content hashes exclude already-used
messages before source caps; capped overflow and late imports stay pending.
The first bounded cold sample may leave older unseen messages for later runs.
Seed uses only its carried raw messages as the known baseline.

Do not run `seed` for a narrow `$deep-context check`; report its
`next_command` and stop.

The store's state picks one of three starts; there is no mode flag:

- **Cold start (new account):** no store yet. `ensure-parents` creates it and
  projects the imported people as the roster.
- **One-time legacy seed:** `check` reports `seed_required` for an install with
  pre-SQLite artifacts. Run `seed` once after `ensure-parents`; a seeded store
  refuses a second run.
- **Incremental refresh:** a store exists. Re-import, fan in, and run
  `ensure-parents`: new contacts and evidence are added, existing contacts and
  every decision are kept, and a contact the refresh omitted is not deleted.
  New unresolved contacts enter the worth and lookup queues like any other.

After `ensure-parents`, stages use SQLite projections; legacy recovery also
reads original facts files. `realize` writes the final people CSV.
Run `bin/deep-context audit` after preparation and again at completion.
Report structural defects, identity review signals and incomplete contact
history separately; follow [Contact recovery](recovery.md) for findings.
An audit finding alone does not authorize paid work or prove a bad identity.

Report Gmail/iMessage/WhatsApp readiness, merged people, and candidates per
source. Stop on unreadable iMessage Full Disk Access.

Inspect `.powerpacks/deep-context/owner.json`. If it exists, run
`bin/deep-context owner` once to project it into SQLite (no flags, no RapidAPI).
If it does not exist, ask for the user's LinkedIn URL and email.
Disclose that a profile-cache miss calls RapidAPI and get approval before:

```bash
bin/deep-context owner --linkedin-url <url> --email <email>
```

Owner context is required: `dry` and `compose` fail without it. `check` routes
to owner only after `ensure-parents` and any required `seed`.

### 2. Message scope

Group bodies are always included — do not ask, and do not mention group
inclusion in user-facing status copy (the authorization is standing; see
Privacy and approvals).

Always use the default depth (`--deep-cap 1600`). Do not ask the user about depth
or surface the message cap; only change it if the user explicitly requests a
shallower or deeper pass.

Collect messages for the source people projected above:

```bash
bin/deep-context collect --deep-cap 1600
```

Collection is local/free. Preserve the exact approved flags through synthesis.
Transient source reads retry three times. An unreadable source fails the stage;
it is not an empty source. On failure, inspect
`.powerpacks/deep-context/raw/manifest.json` and resolve the reported access or
database error before continuing. Existing evidence for the failed read stays.

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
printed by `dry` — do not invent a different scope. Synthesis extracts facts.
JEV then answers the 34 share-label and 7 worth questions together, storing
`network_worth` and `labels` in each contact's `facts/<person_id>.jsonl` and explicitly
projecting that completed payload into SQLite facts. Effective worth reads the
human override first, then the parent machine decision from enrichment, then
the best machine verdict on the parent's facts. Existing facts are reused without
another GPT call; JEV resumes from its request cache; human decisions remain unchanged. Use `--force` explicitly to
rebuild facts.

Synthesis retries transient API failures three times through the SDK. If any
batch remains failed, the person's prior facts stay, the person is left for the
next run, and `.powerpacks/deep-context/facts/manifest.json` lists person,
batch, and error. The run still completes, scores worth for everyone else, and
the flow moves on without those people. It fails only when every person failed.
A later run processes a left-behind person again, including their successful
batches.
Successful extraction records carry model/effort, so a failed model switch
remains pending on an ordinary rerun. `--force` re-extracts the current bounded
bundle and preserves history; retry a failed forced run with `--force` again.
Older unseeded facts without message metadata retain their fingerprint cache;
the first changed bundle establishes coverage while retaining prior facts.

Worth uses message context and contact identifiers only, never the LinkedIn
profile, with one rule decided in code rather than by the model: a person whose
imported LinkedIn headline names a notable role (CEO or any chief officer,
founder, president, chair, partner, managing director) is Yes regardless of
message evidence, with the reason `Notable title: <headline>`. For everyone
else:

- For Gmail or Gmail+phone, bias toward Yes for clearly human, person-directed
  correspondence, including sparse, old, academic, personal, or plausibly
  important professional contacts. Use No only for clear automated/broadcast/
  transactional noise or unengaged cold spam. Maybe should be rare.
- For phone-only dossiers, genuine two-way or repeated conversation is Yes;
  sparse or ambiguous exchanges may be Maybe, and automated noise is No.
- For mixed sources, a real relationship on either channel wins over noise on
  the other. A recognizable name or plausible area code is weak context only
  and must not become an invented identity or fact.

Then run:

```bash
bin/deep-context compose
bin/deep-context validate
```

### 4. Duplicate people

Identity resolves cheapest evidence first. The cluster stage merges an
identical name with a shared source contact phone or email locally. Other
compatible names receive one GPT-6.1-sol high judgment: same person, different
people, or uncertain. Only an affirmative same-person judgment accepts a pair;
a matching name alone is not an accepted merge. Extracted contact details
cannot create a pair or a free merge, and a shared identifier cannot override
incompatible names. Reuse same-person and uncertain decisions only when the
complete request matches. Uncertain pairs remain separate without becoming
different-person constraints. Explicit different-person decisions persist across
evidence changes and block contradictory transitive merges; a clean rebuild
discards these machine decisions too. A shared first name, last name or email
handle alone is not compared.
Preview the complete stage first:

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

`parents` is free and idempotent — run it after clustering so the canonical
layer always matches the accepted merges. Report `pairs_slam_dunk` (identical
name and a shared source identifier), `pairs_reused`,
and `pairs_judged`.

Candidate dossiers participate, so candidate-to-existing-person merges happen
with message context before any paid identity lookup. A candidate merged into an
existing person does not reappear in the People queue or paid lookup; the
merge folds its email/phone/channel metadata onto the kept LinkedIn.

### 5. Preview and run enrichment

After `parents`, the agent runs:

```bash
bin/deep-context enrich --dry-run
```

The read-only plan lists net-new Parallel lookups and cost, profile fetches,
judgment estimates, one `estimated_usd` total, and `estimated_minutes`, a rough
time for the run. Tell the user that time. Run automatically without asking when
`estimated_usd` is at most $100. Ask first only above $100. Then run:

```bash
bin/deep-context enrich
```

The command sets the research budget to the plan's Parallel estimate and runs
research → profiles → identity → relationships → settle → synthetic. Effective-Yes
contact-only parents enter research; existing LinkedIns and completed research,
including no-match, skip new lookups. Mapped real LinkedIns without human or
valid machine decisions enter the identity judge.

Re-running `enrich` after a failure starts the sequence again and continues from
completed work in SQLite and provider artifacts on disk. It is safe to run after
any later import: each step processes only pending work. There are no separate
checkpoints. One enrichment `manifest.json` reports `status`, `phase`, and
errors; it does not select work.

The run also writes where it stands into SQLite (the `enrich_run` row of `meta`):
its status, the step it is on, its errors, and, when it completes, what it could
not finish. One person's lookup, fetch or judgment failing does not stop the run.
A step that crashes is run once more after a few seconds before the run fails.
A completed run moves the flow on to Check LinkedIn with those left over; the
next run tries them again. A run that stopped part-way leaves `running` or
`failed` there, and the next action stays `enrich` until a run completes. The
waiting screen reads the same row.

Then open the review, which lands on Check LinkedIn:

```bash
bin/deep-context review
```

Opening review serves current SQLite choices without resetting human decisions
or calling providers. Worth review remains optional through `review worth`;
Maybe does not stop enrichment. Open the UI once and wait for the wrapper's
`review UI:` line before opening the page.

Use the read-only handoff command while the user reviews:

```bash
bin/deep-context review-status --wait --timeout 900
```

It returns immediately for agent actions `synthesize`, `enrich`, and `realize`.
Run the returned action under its cost rule. For `review_linkedin`, it waits for
SQLite decisions; on timeout, run the wait command again. A bare `review-status`
prints the same contract once. Readiness comes from SQLite, not chat or browser
state. The review API continues to serve worth/identity decisions and enrichment
progress.

### 6. Identity preparation and settlement

The agent's enrichment command uses the same chain as the review server.
Profile preparation is cache-first; saved identity and relationship judgments
are reused. Human worth and identity decisions remain authoritative. Provider
outputs are projected into SQLite before downstream steps read them.

Settlement applies these rules before synthetic assembly:

- A machine-accepted lookup LinkedIn with a missing, errored, or empty profile
  (no experience and no education) is detached with an empty-profile reason.
  Own `linkedin_csv` connections and human link decisions are preserved.
- A LinkedIn the machine is unsure of is detached the same way when its profile
  was fetched and has nothing on it: a person is only asked to check a LinkedIn
  that has something to look at. One whose fetch failed waits for the next run,
  and the LinkedIn a person was imported with is kept.
- Two addresses of one LinkedIn profile (the same member id: the person renamed
  their LinkedIn) are one LinkedIn. An unsure address is detached when the
  person already keeps that profile; among unsure ones, the address LinkedIn
  itself answers to stays and the others are detached with
  `same LinkedIn profile as <kept>`.
- Without a human worth decision, effective Yes/Maybe becomes machine No when
  the parent has no real LinkedIn profile and fewer than `REVIEW_MESSAGE_BAR`
  (25) messages across non-owner imported people. The reason is
  `not enough to know who this is: no LinkedIn profile and N messages`.
  Own connections and LinkedIns a human kept count as real; other real profiles
  must be accepted, present, and have experience or education. At 25 messages
  the parent stays reviewable.
- An own LinkedIn connection with no human worth decision is always worth Yes,
  whatever the worth pass said.

These worth decisions live in `parents.machine_worth` / `machine_worth_reason`,
above facts and below human worth. JEV rewrites facts, so it cannot undo this
rule. Rerunning settlement clears its No when a real profile arrives or messages
reach 25. Worth-No parents leave LinkedIn review and later research, and receive
no synthetic profile.

### 7. LinkedIn decision gate

After enrichment, `bin/deep-context review` opens Check LinkedIn for any
remaining uncertain profiles. The review server stays alive during review.

For a found/existing LinkedIn the question is simply whether it is the right
person. Yes verifies it. No only opens the correction panel and is not a
decision. The correction panel accepts a replacement URL or a terminal Skip;
Skip writes a detach decision, rejects the shown/proposed LinkedIn, and leaves
the person out of the index for now. A description instead of a URL queues a
re-research in the background: if it finds a LinkedIn that clears the judge,
that LinkedIn is applied; otherwise the person's No is saved and they keep no
LinkedIn. The person is not shown again either way, and
`review-status --wait` holds at `review_linkedin` until every re-research has
landed. Synthetic profiles remain local without
requiring LinkedIn review. They are not automatically approved for indexing or
upload. Existing human decisions, including synthetic profiles retargeted to a
real LinkedIn, remain authoritative.

Continue through the wait loop. Continue to realization only when
`bin/deep-context review-status --wait` returns `next_action == "realize"`.

### 8. Apply and realize

Stop the review UI first so realization is not competing with an in-process
enrichment job. SQLite transactions serialize the writes without auxiliary
runtime state.

Realizing reads only the SQLite store and needs no provider approval:

```bash
bin/deep-context stop
bin/deep-context realize
```

`realize` applies every verified or retargeted LinkedIn (including a pasted
LinkedIn on a synthetic card) and every detach to the SQLite roster, then
exports one row per existing parent to
`.powerpacks/network-import/merged/people.csv`. SQLite keeps the individual
source-contact rows; an exported parent is never re-imported as source ownership.
Each accepted LinkedIn fills its work history, education and headline from the
profile already projected into SQLite; realize never calls a provider. Report
`rows`, `profiles_filled` and `profiles_missing` in one line. When
`profiles_missing` is not 0, run `bin/deep-context profile-prefetch` (free
preview), get approval for the RapidAPI calls it lists, run it with `--fetch`,
then realize again.

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

Pass only on `status: ok`. The validator also fails when someone with work
history in `merged/people.csv` has no positions in the index
(`people_missing_positions`); rebuild the index before sharing.

### 9. Share: who leaves the laptop

After the index validates, ask once: **"Share your network with the team?"**
No -> go to the completion report. Yes (or `$deep-context share` on its own)
-> create a visible plan with these exact phases:

```text
[Share] Build the share list
[Share] Open the People page
```

Who leaves the laptop is a per-person decision — see
`packs/ingestion/docs/share-and-upload.md`. Synthesize (step 3) already asked
JEV the label questions and saved the answers with each person's facts; `share`
is free and local, and safe to rerun: it rebuilds `person_labels` and `share`
and never touches the user's tags, which win over worth.

```bash
bin/deep-context share   # person_labels + share tables, every merged person
```

Report its `share_yes`, `share_no` and `confirm` counts in one line, then open
the page (run it in the background; it serves in the foreground):

Share follows worth: the `share` table says yes to the worth-yes people, no to
the owner, to a human `private`, and to everyone worth said no or maybe to. The JEV
labels decide nothing — they raise at most one flag (family, partner, minor,
sensitive context, clinician/lawyer/banker, automated sender, stranger) on a
worth-yes person, which makes that row `confirm`. Confirm rows upload nothing
until the user answers. The answer is a tag, recorded on the People page:

```bash
bin/deep-context review people   # prints review UI: http://127.0.0.1:8765/people
```

The page has three tabs — Needs confirmation, Sharing, Not sharing — one row
per parent (the emails and phone numbers merged under one person share the row,
its counts and its decision) with the reason, sources, relationship, worth,
warmth, last contact and interaction count; the
left rail filters by those cells and by the JEV labels, the quick filters open
the usual slices (family, sensitive context, service providers, recruiters,
strangers, dormant), and the bulk bar tags a whole selection **Share** or
**Keep private** (`Use worth` clears the tag). Each write re-decides those
people's `share` rows in the same transaction, so the table stays current; `z`
undoes the last write. Tell the user the URL and the three counts; do not read
the list aloud. The upload is the page's **Share network** button (top right):
the user clicks it, reads the check's counts, and presses **Confirm sharing**
themselves. Do not run the upload for them. It upserts `persons`, reconciles
this operator's `operator_person_sources` rows, writes or patches the five v3
TurboPuffer namespaces, and mirrors the user's own `private` into
`contact_tags`. People without a LinkedIn never reach the cloud. The
standalone CLI (`upload_powerset.py`, plan-only without `--apply`) is for
debugging; `.powerpacks/upload-powerset/manifest.json` and `errors.log` hold
the last run.

Opening the page ends the skill: give the URL, the three counts, and one line
— answer Needs confirmation, then Share network → Confirm sharing.

## Completion report

Report terse counts: people/candidates dossiered, duplicate merges, explicit
worth Yes/No, lookup results, LinkedIns verified/detached/retargeted, synthetic
profiles accepted, final merged people count, and index validation. Mention any
still-unresolved Yes people explicitly.

## Durable artifacts

```text
.powerpacks/deep-context/raw/                    ephemeral sampled bodies + manifest
.powerpacks/deep-context/facts/                  extracted facts + manifest
.powerpacks/deep-context/facts/parents/          derived parent facts; original contact files remain
.powerpacks/deep-context/dossiers/               dossiers + index
.powerpacks/deep-context/parents/                canonical people + manifest
.powerpacks/deep-context/reconcile/deep-research/<handle>/00_parallel_result.json
.powerpacks/deep-context/reconcile/deep-research/manifest.json  display-only stage receipt
.powerpacks/deep-context/deep-context.sqlite      canonical runtime state
.powerpacks/deep-context/review/avatars/          locally cached live profile images
.powerpacks/network-import/merged/people.csv
```

The product/algorithm detail remains in
`packs/ingestion/docs/deep-context-pipeline.md`; read it only when diagnosing a
failed primitive or changing implementation behavior.
