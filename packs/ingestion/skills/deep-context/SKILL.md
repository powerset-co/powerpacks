---
name: deep-context
description: Build the richest per-person markdown dossier from local message bodies (Gmail + iMessage DMs + WhatsApp DMs) and retrieve it by name/phone/email; surface likely same-person merge candidates; verify each person's attached LinkedIn is really them (self-heal). Use for $deep-context, "build deep context", "context/dossier on a person", "who is <phone/name> in my messages", "find duplicate people to merge", "check the LinkedIn we attached is the right person".
---

<!--
Created: 2026-06-21
Changelog:
- 2026-07-09b: Synthetic review in the UI + research opt-in. reconcile-deep-research gained
  --include-plausibly-absent (researches the judge's "plausibly has no LinkedIn" people — the
  primary synthetic candidates; still excluded from retarget research by default). The review UI
  now surfaces synthetic-people.csv rows: pending -> Needs review with a "🧬 synthetic — no
  LinkedIn" badge (researched profile shown in place of a LinkedIn link), Keep -> approved=yes
  (merges), Detach -> approved=no, ↺ -> pending. Decisions write only the synthetic CSV's
  approved column.
- 2026-07-09: Synthetic profiles (packs/ingestion/docs/synthetic-profiles-plan.md, built).
  `bin/deep-context assemble-synthetic` turns existing deep-research artifacts into
  people-schema rows for people with NO real LinkedIn (synth-… identifiers,
  enrichment_provider=synthetic, research metadata attached). Gated behind `approved`:
  completeness >= 0.6 auto-approves, the rest wait in overrides/synthetic-people.csv.
  The fan-in merge auto-ingests approved rows (keep-filter admits synthetic rows only
  with approved auto/yes; real rows still require LinkedIn + rapidapi). Research runs
  on Parallel.ai via the existing reconcile-deep-research flow — one research pass per
  person, branching retarget (LinkedIn found) vs synthetic (none found).
- 2026-07-06: Spam screen + LLM re-review. reconcile now also judges whether a contact is
  spammy cold outreach the user never engaged with (spam_contact/spam_confidence/spam_reason in
  the verdict schema) and writes machine-owned llm_reject/llm_reject_confidence/llm_reject_reason
  columns into overrides/review.csv (backwards compatible; ALWAYS refreshed, even on user-decided
  rows — action/approved stay user-owned). The fan-in merge drops spam-flagged people (conf >=
  0.85) UNLESS the user made a keep-ish decision (approved=yes, action != detach). The review UI
  gained a "Rejected" tab showing flagged people (spam-flagged leave the Needs-review pile; a Keep
  there protects them). `$deep-context re-review` / "re-review my contacts" / "refresh the LLM
  decisions" runs `bin/deep-context re-review` — a fresh LLM re-judge refreshing all NON-user
  decisions + the spam screen; user yes/no rows keep their action. SPEND-GATED like reconcile:
  always run `re-review --dry-run` first and confirm the estimate with the user. Also: merged
  (multi-LinkedIn) people now auto-resolve in the review UI (parent auto-verifies when the judge
  is confident, the rest auto-detach as approved=auto — one overturn click instead of N confirms),
  and a retarget ("fix") now reads as "verified" in the UI.
- 2026-06-29: Added a review-only fast path. `$deep-context review` (or "open/show the review
  UI", "let me click through") now just runs `bin/deep-context review` — read-only over the
  existing reconcile artifacts, opens the browser, no checklist, no spend — instead of walking
  the full pipeline checklist. Same for other single read-only subcommands the user names
  (lookup/check/validate). The mandatory full checklist stays for an actual build/run/--force.
- 2026-06-26: Never rely on memory for ANY approval — always ask/confirm with the user, every
  run. Added a top-level gate rule: a harness memory/auto-recall layer (Codex ~/.codex/memories,
  Claude Code session memory, prior transcripts, a cached "previously accepted" answer) may
  suggest a default but NEVER substitutes for the user's confirmation this run; do not mark a
  gate "satisfied by a previous answer." Non-negotiable for --include-groups (reads others'
  group bodies) and any paid step. Fixes a Codex run that skipped the group/cap ask by treating
  a remembered "include groups, cap 1600" as pre-approved.
- 2026-06-25: A `--force` rerun keeps the FULL checklist — pass `--force` to the two incremental
  steps and finish every step. "How to run" now says: still create + walk the entire checklist;
  add `--force` only to `collect` and `synthesize`; run to completion, pausing ONLY at the gate
  items (owner-LinkedIn answer [skip the ask if owner.json exists], group/cap, the dry-run cost
  OKs, review-wait). Names the failure mode: running one step (e.g. `collect --force`) and
  halting — collect is step ~4 of ~20, mark it done and continue. Fixes Jake's rerun where the
  agent ran a lone `collect --force` and dropped the step-1 LinkedIn ask.
- 2026-06-24: Owner-alias EXCLUSION (the downstream half of is_owner detection). build_parents now
  skips any person flagged is_owner (you on another email — e.g. arthur.chen@spot2.mx) so you stop
  appearing as your own contact, and folds those alias emails into owner.json (so your addresses
  aggregate + future runs know them directly). `reconcile --reapply` drops verdicts for parents that
  no longer exist, so the excluded owner falls out of the review table/UI for FREE (no re-judge).
- 2026-06-24: Deep-research recovery now also honors USER detaches. reconcile-deep-research's
  eligible set was model-only (high-confidence wrong_person verdicts); it now ALSO includes links
  the user marked detach in review.csv (action=detach, approved=yes), so running it AFTER review
  recovers what you detached. Skips links that already have a retarget; idempotent across re-runs.
- 2026-06-24: Pass thread-level participants + detect owner aliases. collect now captures each
  email thread's full from/to/cc roster (Name <email>) from msgvault (was dropped — we only kept
  subject/body/direction); synthesis gets it plus an owner-identity declaration + heuristic and a
  new is_owner output. So a 'contact' that is really YOU on another address (e.g. arthur.chen@spot2.mx,
  with your gmail CC'd and the same name) is flagged is_owner=true. Validated: 3 real aliases → true,
  a genuine namesake (Arthur Lam, whose threads also include your gmail) → false. Richer co-participant
  context also improves every dossier. (Downstream: exclude is_owner from parents/network + fold the
  alias into owner.json — follow-up.)
- 2026-06-24: Free recovery from a self-reported LinkedIn. When a contact SHARED their own
  LinkedIn in their messages (synthesis captures it in facts `identifiers`), reconcile now feeds
  it to the judge AND auto-proposes a retarget to it when the attached link differs — no Parallel
  deep-research needed (e.g. ankita-goyal recovered from a wrong namesake). Name-compatibility
  guard: auto only when the URL's slug matches the contact's name, else pending (a shared URL can
  be a third party they mentioned). Runs free on `reconcile --reapply` too.
- 2026-06-24: Don't truncate LLM output. Raised synthesis max_output_tokens 4000->8000 (a ceiling,
  billed only if used) and warn on a truncated (incomplete) completion. The dossier one-line
  Summary now trims at a word boundary with an ellipsis (full text still in Relationship & cadence).
- 2026-06-24: Review UI dossier shows the rich CHILD dossier(s), not the thin parent stub.
- 2026-06-24: No needs_review limbo in parent clustering. build_parents now folds EVERY
  clustered member into the parent as a child (defaulted in, carrying its merge confidence)
  instead of splitting low-confidence members into a needs_review bucket that nothing surfaced
  — those members appeared in no parent's children, so reconcile never judged them and they
  vanished (e.g. a 3rd 'Chrissy Hu' that turned out to be a different person). A human rejects
  the rare wrong one in the review UI.
- 2026-06-24: Feed the judge the FULL LinkedIn work history (was truncated to 6). A PAST
  employer is often the identity anchor — e.g. Clara Ma's profile leads with founder roles but
  lists 'Venture Hacker @ AngelList' at position 9, matching her help@alist.co contact; the cap
  hid it and manufactured a false miss. No cap now, in the judge prompt and the review UI.
- 2026-06-24: LinkedIn Connections are GROUND TRUTH. A contact imported from your LinkedIn
  Connections (source_channels contains linkedin_csv) is auto-confirmed at 1.0 WITHOUT the LLM
  — you're connected, so it's them. Skips ~26% of judge calls and fixes the few connections the
  judge hesitated on. `reconcile --reapply` overlays it for free.
- 2026-06-24: Keep-biased self-heal. The judge now DEFAULTS TO CONFIRMING — name + any one
  corroborating signal (employer/school/location/era/social context) and no hard contradiction
  → confirmed; absence of work talk for personal contacts no longer deflates confidence (was
  manufacturing false negatives). Asymmetric thresholds: confirmed auto-VERIFIES at 0.70 (the
  user fixes the rare mismatch), wrong_person auto-DETACHES only at 0.85 (dropping a real person
  is costly). `--detach-threshold` added; `reconcile --reapply` re-decides for free. Review UI
  shows direction-aware judgments (a high-% wrong_person reads as a strong MISMATCH, not a strong
  match) and groups multi-LinkedIn ("conflict") people as labelled Option 1/2 under one banner.
- 2026-06-24: Review UI (`bin/deep-context review` → reconcile_review_web, local web, free).
  review.csv is keyed only on the candidate LinkedIn, so it's un-reviewable alone; the UI JOINS
  it with reconcile/verdicts.jsonl (parent, profile, judge reasoning) + parents/*.md to show one
  expandable row per PERSON — the LinkedIn(s) matched, picked link, verified/detached/pending
  state, supporting/contradicting evidence. Keep / Detach / Fix-LinkedIn autosave into review.csv
  (the merge's durable table); Fix writes a retarget row applied by apply-retargets. Filters
  (needs-review/verified/detached/conflicts/fixed/my-decisions), search, riskiest-first sort.
  This is now the review surface (replaces "open summary.md"; summary.md kept as a text fallback).
- 2026-06-21: Initial skill — two-phase flow (deep per-person dossiers → LLM-judge
  merge into parents). Incremental confidence-gated synthesis, deep 1600-msg pools,
  owner.json shared-context inference, high-reasoning holistic merge judge,
  parent/child layer (confirmed core + needs-review), validate_dossiers, group NAMES
  as context, opt-in --include-groups for group bodies, gated check→ask→collect→
  dry-run→confirm→run task flow (mirrors $setup).
- 2026-06-22: Phase 3 — reconcile each parent against its attached LinkedIn profile
  (self-heal). High-reasoning judge (confirmed / wrong_person / needs_review) over the
  message-derived dossier vs the LinkedIn lookup; high-confidence verdicts auto-apply to
  people.csv (confirmed→verified, wrong_person→detach with backup), low-confidence →
  review queue; never forces a LinkedIn (linkedin_plausibly_absent). Deep-research
  escalation (Parallel.ai) on wrong_person detaches, $25 auto-approve cost gate.
- 2026-06-22: Conflict auto-resolution — a parent with multiple attached links where
  exactly one is high-confidence confirmed and the rest high-confidence wrong_person is
  resolved automatically (keep the confirmed, detach the wrong); ambiguous conflicts still
  defer to review. All auto-actions logged to reconcile/applied.csv; `reconcile --reapply`
  re-decides/applies from existing verdicts with no OpenAI spend.
- 2026-06-23: Inverted the self-heal to be durable — reconcile no longer mutates people.csv.
  It writes a durable override table (network-import/overrides/review.csv) that
  the fan-in merge (merge_network_sources) re-applies every run: detach clears the wrong link
  (LinkedIn-only people.csv then drops that person), verify annotates linkedin_verified.
  Survives re-merges + Modal index rebuilds.
- 2026-06-23: Retargeting + approval-aware decisions table. The override gains an `approved`
  column (auto = high-confidence applies; yes/no = user decision, sticky across re-runs) and a
  `retarget` action. Deep research proposes a correct LinkedIn (pending); `apply-retargets`
  enriches it (cache-first RapidAPI) into overrides/retarget-people.csv, which the merge
  auto-ingests so the person re-appears with the correct profile (old wrong link dropped).
- 2026-06-23: Contact consolidation — when a parent keeps one link and detaches siblings,
  reconcile folds every child's emails/phones/per-channel interaction_counts onto the kept
  LinkedIn via overrides/consolidate-people.csv (contact-only, auto-ingested + unioned by the
  merge). The surviving person keeps the correct profile AND all sibling contacts; per-channel
  counts stay per-channel. Trusts Phase 2's grouping (fix over-merges upstream).
- 2026-06-23: One summary (reconcile/summary.md) instead of three CSVs; hard-stop "WAIT for the
  user to finish reviewing" step before apply-retargets. Merge self-heal inputs resolve relative
  to the output-dir's overrides/ sibling (cwd-independent).
- 2026-06-23: One editable file. Every judged row (incl. low-confidence/needs_review/ambiguous)
  now lands in overrides/review.csv — high-confidence as approved=auto, the rest as
  pending with a suggested action. Retired the separate review-queue.csv; the user reads
  summary.md and edits the single decisions table (approved column, sticky).
- 2026-06-23: Owner profile is now the FIRST step — `bin/deep-context owner --linkedin-url <you>`
  builds owner.json from the RapidAPI cache (never WebFetch). Added Phase 4 `[Realize]`
  (`bin/deep-context realize` = fan-in merge applying review.csv/consolidate/retarget, then the
  $setup Modal index-people rebuild). Scrubbed real names from test fixtures/skill examples.
-->

# deep-context

Use this for `$deep-context`, "build a dossier on the people I message", "what
context do we have on <name/phone>", or "find people I should merge".

It builds one **markdown dossier per person** from the actual bodies of your
Gmail threads and iMessage/WhatsApp **DMs**, then lets you **look a person up by
name and/or phone (or email)** and flags **likely same-person merge candidates**.

Heavy reasoning runs on OpenAI (parallel, medium reasoning); the local box only
streams SQLite row-by-row and writes files, so it stays well under 1 GB RAM on a
weak CPU.

## Reads message bodies (by design)

Deep inspection of message bodies is the whole point of this skill — it reads
Gmail threads and iMessage/WhatsApp DM bodies to build the dossier. This is a
scoped exception to the repo's otherwise metadata-only contract; it applies ONLY
to `$deep-context`.

- iMessage / WhatsApp: **DM bodies only**. Group-chat **bodies are never read** —
  only group **names** (metadata) are collected, as a relationship signal.
- Raw sampled message text lands in `.powerpacks/deep-context/raw/` — **ephemeral,
  gitignored**. Purge it after dossiers are built (see Step 5).
- Dossiers store **synthesized facts, not verbatim message bodies**.
- Synthesis (Step 2) sends the sampled text to **OpenAI** for fact extraction —
  the same trust boundary as `$enrich-email-markers`.
- iMessage needs macOS **Full Disk Access** for the process that runs it. The
  Claude Code Bash tool runs under a helper that does NOT inherit your terminal's
  FDA, so run the pipeline in your own terminal (e.g. `bin/deep-context run`).

## Prerequisites

- A merged network: `.powerpacks/network-import/merged/people.csv` (run `$setup`
  / `$discover-contacts` first).
- msgvault synced (`$import-email`); macOS **Full Disk Access** for iMessage
  (`chat.db`); WhatsApp via `$import-whatsapp` if you want that channel.
- `OPENAI_API_KEY` in `.env`; `.venv/` ready (`bin/setup-python`).

## How to run this skill

**Review-only fast path (no checklist, no spend).** If the user only wants to
**open / look at the review UI** — `$deep-context review`, "open the review
page", "show me the review UI", "pop the UI back up", "let me click through" —
do NOT create the full checklist and do NOT run any pipeline or paid step. Just
run `bin/deep-context review` (it serves the existing `reconcile/verdicts.jsonl`
⨝ `overrides/review.csv` ⨝ `parents/*.md`, opens the browser, and is read-only +
free), tell the user it's open and that their keep/detach/fix/exclude clicks
autosave to `overrides/review.csv`, and stop there. The review UI does NOT
require re-running anything — it shows whatever the last run produced. Only if
the reconcile artifacts don't exist yet (Phase 3 never ran) should you say so
and offer the full run below. Same goes for any other single read-only
subcommand the user names explicitly (`lookup`, `check`, `validate`): run just
that one, skip the checklist. The full checklist below is for an actual
build/`run`/`--force` of the pipeline.

**FIRST, before running anything: create a literal, visible checklist with all
the steps below and step through it, marking each complete as you go.** Mandatory.
Use your harness's plan/task tool:

- **Claude Code:** `TaskCreate` one task per item below, then `TaskUpdate`
  each to `in_progress` then `completed`.
- **Codex:** `update_plan` with the steps, updating status as you go.
- **Any other harness:** its equivalent todo/plan mechanism.

**Never rely on memory for ANY approval — always ask and confirm with the user, every run.**
This skill's gates are interactive: the owner-LinkedIn ask, the group-chat / message-cap
choices, and every cost confirmation (synthesis, cluster, reconcile, deep-research). A harness
memory / auto-recall layer (Codex `~/.codex/memories`, Claude Code session memory, prior
transcripts, a cached "previously accepted" answer) MAY suggest a default — but it is **never**
a substitute for the user's confirmation *this* run. Surface the suggested default and wait for
an explicit OK; do not mark a gate "satisfied by a previous answer." This is non-negotiable for
`--include-groups` (it reads other people's group-chat bodies) and for any paid step — never
silently apply a remembered "yes." `--force` does not change this: it re-processes everyone, it
does not pre-approve anything.

**A `--force` rerun keeps the FULL checklist — you just pass `--force` to the two
incremental steps and run every step to completion.** When the user says "rerun",
"run again", or "`$deep-context --force`":

- **Still create and walk the ENTIRE checklist below, in order.** `--force` does NOT
  change the steps or let you skip any — it changes only *incrementality*
  (re-process EVERYONE instead of skipping people who already have a bundle/facts).
- **Add `--force` to exactly the two incremental steps:**
  - `[Context] Gather each person's messages` → `bin/deep-context collect --force`
  - `[Context] Build a profile for each person` → `bin/deep-context synthesize --force`

  Every other step is run exactly as written (compose, cluster, parents, validate,
  reconcile, review, realize). Equivalently, `bin/deep-context run --force` chains
  all of them and propagates `--force` to both — but you still track each as its own
  task so the user sees progress.
- **Run the whole checklist to COMPLETION. The only places you stop for the user are
  the explicit gate items:** the owner-LinkedIn answer (skip the *ask* if `owner.json`
  already exists — just confirm its values), the group-chat / cap answers, each cost
  confirmation (`dry` → OK before synthesize; `cluster --dry-run` → OK; `reconcile
  --dry-run` → OK), and the review-wait. Do not pause anywhere else.

**The one failure mode to avoid:** running a single step (e.g. `collect --force`) and
halting. Collect is step ~4 of ~20 — finishing it is not finishing the skill. Mark it
done and continue straight to the next task.

Seed the checklist with these exact item titles. Each is tagged by phase —
`[Context]` builds a profile of each person from your messages, `[Merge]` combines
duplicates of the same person, `[Self-heal]` checks & fixes each person's LinkedIn,
`[Realize]` applies it all and rebuilds the search index:

```
[Context]   Ask for YOUR LinkedIn and build your own profile (so we can spot what you share with each person)
[Context]   See which message sources are connected (Gmail, iMessage, WhatsApp)
[Context]   Ask whether to include group chats, and how far back to read per person
[Context]   Gather each person's messages
[Context]   Estimate the cost before anything is spent
[Context]   Confirm the cost with you before spending
[Context]   Build a profile for each person from their messages
[Context]   Double-check the profiles came out complete
[Merge]     Find people who look like duplicates of each other
[Merge]     Combine each set of duplicates into one person
[Self-heal] Preview the LinkedIn check (free, nothing spent yet)
[Self-heal] Confirm cost, then check each person's attached LinkedIn is really them
[Self-heal] Record the fixes (remove wrong LinkedIns, keep the right ones)
[Self-heal] Add the unsure ones to your review list
[Self-heal] Look up the correct person for any wrong LinkedIn we removed
[Self-heal] Open the review page to see each person, the LinkedIn we picked, and why
[Self-heal] Wait for you to finish reviewing (keep / detach / fix each link) before continuing
[Self-heal] Re-attach the correct LinkedIns you approved
[Realize]   Apply your decisions to the network (fan-in merge — uses your review.csv)
[Realize]   Rebuild the search index on Modal so the fixes show up in search (~5–30 min)
```

Do not drop steps; mark inapplicable ones complete as a no-op. **Never spend money before
showing the estimate and getting an explicit OK** — the paid steps are building the profiles
(`[Context]`), finding duplicates (`[Merge]`), and the LinkedIn check (`[Self-heal]`).

### Phase 1 — Build ONE deep dossier per person

Each person gets a single child dossier from up to `--deep-cap` (1600) messages,
pooled across **Gmail bodies + iMessage DMs + WhatsApp DMs**, plus iMessage
**group-chat names** (metadata) as relationship context.

- **[Context] Ask for YOUR LinkedIn and build your own profile** — FIRST, ask the user for their
  own LinkedIn URL, then `bin/deep-context owner --linkedin-url <their-url> --email <their-email>`.
  This writes `.powerpacks/deep-context/owner.json` (their schools/jobs/locations with year ranges)
  from the **RapidAPI cache** (a cache hit is free; NEVER WebFetch linkedin.com — it hallucinates).
  Confirm the schools/employers it found look right. This owner profile is injected into synthesis
  and the LinkedIn judge so they infer **shared context** (same school/employer/era) with each
  contact — skipping it loses that whole signal. If `owner.json` already exists, it reports the
  current values; `--force` to rebuild. (owner.json is gitignored — it's local only, never committed.)
- **[Context] See which message sources are connected** — `bin/deep-context check`. Per-source readiness +
  `ready`. If iMessage is `unreadable_full_disk_access`, run in a terminal with
  Full Disk Access (not the Claude Code Bash tool).
- **[Context] Ask whether to include group chats, and how far back to read — get answers before collecting:**
  1. **Group opt-in** — by default we read **DM bodies only** + group *names*.
     Offer `--include-groups` to also read **iMessage group-chat bodies** from
     small shared groups (`--max-group-size`, default 25). Tell them this **costs
     more** (more messages → more synthesis tokens) and pulls in other group
     members' messages.
  2. **Message cap** — we hard-cap at **1600 messages/person** (`--deep-cap`).
     They can raise it for deeper history on heavy relationships, but make sure
     they understand **a higher cap costs more**.
- **[Context] Gather each person's messages** (free, local) — `bin/deep-context collect` with the chosen flags
  (e.g. `--include-groups --deep-cap 1600`). `people_capped` flags high-volume contacts.
- **[Context] Estimate the cost before anything is spent** — `bin/deep-context dry`
  (re-uses the just-collected pools) → "based on your settings, this is the cost"
  (floor/ceiling + wall). The estimate reflects groups/cap because it reads the
  actual collected bundles.
- **[Context] Confirm the cost with you before spending** — present the estimate; get an explicit go. No spend
  before this.
- **[Context] Build a profile for each person from their messages** — incremental confidence-gated synthesis, then
  deterministic dossiers + lookup index.
- **[Context] Double-check the profiles came out complete** — `bin/deep-context validate` → `validation.md`
  (completeness score + flags). Act on `capped_underconfident` by raising `--deep-cap`.

### Phase 2 — Merge people via the LLM judge

- **[Merge] Find people who look like duplicates** — first `bin/deep-context cluster --dry-run`
  for the count + cost (free): only genuinely ambiguous same/similar-name pairs are judged, so
  this is a **small, bounded spend** (typically tens of pairs, well under $1) — don't improvise
  an estimate or run an offline pass. Then `bin/deep-context cluster`: a high-reasoning judge
  decides same-person holistically. Writes `merge-candidates.csv` + `merge-verdicts.csv` (audit).
- **[Merge] Combine each set of duplicates into one person** — `bin/deep-context parents`. One canonical parent per cluster:
  merges only judge-CONFIRMED children (≥`--confirm-threshold` 0.85), lists borderline
  ones under "Needs review", backrefs each child. Repeatable (parent = f(confirmed children)).

### Phase 3 — Verify each person's attached LinkedIn (self-heal)

Every person in `people.csv` already has a `linkedin_url` stapled on during ingestion —
often resolved on thin same-name evidence. Phase 3 throws a **high-reasoning judge** at
each `(parent dossier ↔ attached LinkedIn)` pair: same human or not? It uses corroboration
(employer / school / location / role / behavior) and especially **contradictions** — never
the name alone (a big-company CEO profile stapled to your plumber of the same name is the
case this catches).

- **[Self-heal] Preview the LinkedIn check (free, nothing spent yet)** — `bin/deep-context reconcile --dry-run`. Prints
  task count, link conflicts, and a cost floor/ceiling. No spend, no writes.
- **[Self-heal] Confirm cost, then check each person's LinkedIn is really them** — present the estimate, get an explicit go, then
  `bin/deep-context reconcile`. gpt-5.2, high reasoning, one call per attached profile.
  Writes `reconcile/verdicts.csv` (+ `.jsonl` audit) and injects a `## LinkedIn identity`
  section into each parent (verdict + supporting/contradicting evidence).
- **[Self-heal] Record the fixes (remove wrong LinkedIns, keep the right ones)** — `reconcile` does NOT mutate
  `people.csv`. It writes a **durable override table**,
  `.powerpacks/network-import/overrides/review.csv`, that the **fan-in merge
  re-applies every run** (so the heal survives re-merges). High-confidence verdicts become
  entries: `confirmed ≥ threshold` → `action=verify` (merge annotates `linkedin_verified`
  on the kept row); `wrong_person ≥ threshold` → `action=detach` (merge clears the wrong
  link → since `people.csv` is LinkedIn-only, that person drops out). **Conflict
  auto-resolution:** when one parent has several attached links and exactly one is
  high-confidence `confirmed` while the rest are high-confidence `wrong_person`, the
  confirmed becomes `verify` and the rest `detach`. Entries are keyed by `public_identifier`
  (idempotent upsert) and carry an **`approved` column**: high-confidence rows are written
  `approved=auto` (applied at merge); a user may set `yes`/`no` on any row. **A user-touched
  row (`approved` ∈ {yes,no}) is sticky** — re-runs never overwrite it, so the table is the
  durable, incrementally-curated record of decisions; the merge applies only `approved` ∈
  {auto,yes}. Everything decided is previewed in **`reconcile/applied.csv`**
  (parent, person, kept/detached, via=normal|conflict_resolved, confidence, reason) for
  review. Report: "✅ N verify, 🔧 M detach (incl. R conflict-resolved), ❓ K need feedback".
  `--confirm-threshold` defaults to 0.85. `reconcile --reapply` regenerates the override
  from existing verdicts with no OpenAI spend. **Realize the heal** by re-running the
  fan-in merge + index rebuild (Modal rebuilds the whole index) — the merge auto-reads the
  override file.
  **Contact consolidation:** when a parent has a kept link + detached siblings, reconcile also
  writes `overrides/consolidate-people.csv` — contact-only rows that fold EVERY child's
  emails/phones/per-channel `interaction_counts` onto the kept LinkedIn (trusting Phase 2's
  grouping). The merge auto-ingests it and unions onto the surviving row (the real row supplies
  the profile), so the kept person keeps the correct profile AND all the siblings' contacts,
  while the wrong-link rows drop. Per-channel counts stay per-channel (never summed).
- **[Self-heal] Add the unsure ones to your review list** — every judged row, including the
  low-confidence / `needs_review` / ambiguous-conflict ones, lives in the SAME decisions table
  `overrides/review.csv` as `approved=` **pending** (with a suggested `action`). The
  user acts by setting `approved=yes`/`no` there (sticky) — there is no separate review-queue
  file. Some people legitimately have **no LinkedIn** (flagged `linkedin_plausibly_absent`) —
  they get no row and are left as-is; never force a match.
- **[Self-heal] Look up the correct person for any wrong LinkedIn we removed** — find the *correct*
  identity for detaches via `bin/deep-context reconcile-deep-research`. Eligible = high-confidence
  `wrong_person` detaches the judge flagged **PLUS any link the USER marked detach in
  `review.csv`** (`action=detach, approved=yes`) — so this step **also runs AFTER the user reviews**
  to recover what they detached. `--dry-run` to size it, then the Parallel.ai cost (~$0.05/person):
  **if ≤ $25 run it automatically and tell the user the cost**; **if > $25 stop and ask** (`--approve`).
  Needs `PARALLEL_API_KEY`. Skipped: links flagged `linkedin_plausibly_absent`, or that already have a
  `retarget`. When research finds a **correct LinkedIn**, it adds a `retarget` row (pending) for the
  user to approve. (Idempotent — safe to re-run after review; it only researches what's still unresolved.)
- **[Self-heal] Open the review page to see each person, the LinkedIn we picked, and why** —
  `bin/deep-context review` (opens a local web UI in the browser; free). This is the review
  surface — it JOINS `reconcile/verdicts.jsonl` (parent, profile, the judge's reasoning) with
  `overrides/review.csv` (decisions), so the user sees **one row per person**, expandable to the
  LinkedIn(s) we matched, the verified/detached/pending state, the supporting/contradicting
  evidence, and the message dossier. Quick filters (needs-review / verified / detached /
  conflicts / fixed / my decisions), search, and a riskiest-first sort surface the low-confidence
  ones. Every **Keep / Detach / Fix LinkedIn** click autosaves straight into `overrides/review.csv`
  (the same durable table the merge re-applies) — no CSV editing by hand. (`reconcile/summary.md`
  + `applied.csv` still exist as a text fallback, but the UI is the review surface now.)
- **[Self-heal] Wait for you to finish reviewing before continuing** — this is a **hard stop**. Tell the
  user the review page is open and say *"keep / detach / fix the links you want, then let me know
  when you're done and I'll continue."* Do **not** proceed to apply-retargets until they reply.
  Decisions are saved as they click (sticky `approved` rows in `overrides/review.csv`). Opening
  the page is not the same as the user having reviewed it — never auto-advance past this step.
- **[Self-heal] Re-attach the correct LinkedIns you approved** — `bin/deep-context apply-retargets`. For each decisions
  row with `action=retarget` and `approved` ∈ {auto,yes}, it enriches the correct LinkedIn
  (cache-first; RapidAPI only on a miss — auto, effectively free) and writes an enriched
  re-attach row to `.powerpacks/network-import/overrides/retarget-people.csv`, carrying the
  contact's emails/phones/interaction. The fan-in merge **auto-ingests** that file (old wrong
  link detached → dropped; correct enriched row kept). Realize on the next merge + index rebuild.

**ALWAYS end the run with a summary of what changed and why.** After Phase 3, present a short
report to the user, every time (do NOT list the verified/unchanged links — only what changed):
- **Detached:** M wrong links removed — list them with the one-line reason (e.g. "<name>:
  the attached LinkedIn is a big-company exec, but your contact is a local tradesperson of the
  same name").
- **Retargeted:** R people re-attached to a correct LinkedIn — for each, the new URL **and the
  reason** (why the new profile is the right person, e.g. "matched the wedding-photography
  business + SoCal location from your messages").
- **Needs your input:** K pending rows in the decisions table (`overrides/review.csv`) — low-confidence verdicts + `retarget` proposals to approve.
The interactive version of this is **`bin/deep-context review`** (the UI — present that to the
user). `reconcile/summary.md` carries the same content as text if you need to quote it inline.
Keep it scannable; the user runs this repeatedly to fix things, so make "what changed and why"
obvious each time.

### Phase 4 — Realize the fixes (rebuild people.csv + the Modal search index)

The self-heal writes decisions to `overrides/review.csv` (+ consolidate/retarget files) but does
NOT change `people.csv` directly — the fixes land only when you re-run the **fan-in merge** (which
auto-applies those files) and **rebuild the index**. This reuses `$setup`'s exact steps. Run it
AFTER the user is done reviewing/approving.

- **[Realize] Apply your decisions (fan-in merge)** — `bin/deep-context realize` (or directly:
  `uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py fan-in --people-csv .powerpacks/network-import/merged/people.csv`).
  This goes through `merge_network_sources`, which auto-reads `overrides/review.csv` (applies the
  `auto`/`yes` detach/verify rows), `consolidate-people.csv`, and `retarget-people.csv` — so wrong
  links drop, contacts consolidate, and approved retargets re-attach, all in the rebuilt
  `merged/people.csv`. Free + local.
- **[Realize] Rebuild the search index on Modal** — `uv run --project . python
  packs/indexing/modal/linkedin_modal_pipeline.py index-people --people-csv .powerpacks/network-import/merged/people.csv`.
  Server-side on Modal (needs Powerset runtime keys; spend-bearing). **Expect 5–30+ min, mostly
  quiet** — run it in the background, keep the step `in_progress` until it exits 0, and reassure
  the user while it runs (don't panic at the silence). This is the same indexer `$setup` uses.

`bin/deep-context run [--include-groups] [--deep-cap N]` chains Phases 1–2 once the cost is
confirmed (Phases 3–4 are run separately, after parents exist). Full surface:
`owner|check|dry|run|collect|synthesize|compose|cluster|parents|reconcile|
reconcile-deep-research|apply-retargets|realize|validate|lookup|probe|purge-raw`.

### Step 1 — collect (local, free, reads bodies)

```bash
uv run --project . python \
  packs/ingestion/primitives/deep_context/collect_person_context.py
```

Streams each person's Gmail + iMessage-DM + WhatsApp-DM messages into one ephemeral
bundle per person (≥1 message). Pools up to `--deep-cap` (1600) recent messages and
records the TRUE total (`messages_available`) so `people_capped` is honest. Test with
`--limit 5` or `--person <id>`. Idempotent: re-runs skip existing bundles (`--force`).

### Step 0 — owner context (now the FIRST step, free on a cache hit)

`bin/deep-context owner --linkedin-url <you> --email <you>` builds
`.powerpacks/deep-context/owner.json` (YOUR bio timeline) from the RapidAPI cache — name,
emails, education + work with year ranges. Synthesis injects it as a reasoning anchor so the
model infers **shared context** — same school/employer/place/era — from message content,
rendered as a "Shared context with you" dossier section. It's `{name, emails, education:
[{school,start,end}], work:[{company,title,start,end}], locations, notes}`.

Overlaps are only asserted when message content supports them (not date-matching
alone). Run `synthesize --no-owner` to skip it. (owner.json is gitignored — local only.)
You can also get exact LinkedIn dates via
`packs/search/primitives/fetch_person_profile/fetch_person_profile.py --linkedin-url <you>`
(checks local cache first; RapidAPI only on a cache miss).

### Step 2 — synthesize (paid OpenAI; confirm cost)

```bash
# dry-run shows estimated_cost_usd, then drop --dry-run to spend
uv run --project . python \
  packs/ingestion/primitives/deep_context/synthesize_person_context.py --dry-run
```

Fans out parallel Responses calls (gpt-5.2, medium reasoning). Per person it
**groks incrementally** — refining one running profile batch-by-batch (newest
first) and stopping at `--target-confidence` (0.85), `--saturation-rounds` (2)
stale batches, exhaustion, or `--max-batches` (20). Checkpointed per person
(`facts/<id>.jsonl`) — resumes after interruption. `--dry-run` prints the cost
floor/ceiling. Tune with `--reasoning-effort high`, `--deep-cap` (collection),
`--concurrency`, `--no-owner`.

### Step 3 — compose (local, free)

```bash
uv run --project . python \
  packs/ingestion/primitives/deep_context/compose_dossier.py
```

Deterministically merges each person's facts into `dossiers/<slug>.md` and writes
the lookup `index.json` + `index.md`. To enrich the `## Summary` prose, optionally
spawn a Claude **sub-agent** per dossier afterward (keeps compose itself free/fast).

### Step 4 — merge candidates (LLM judge; small spend)

```bash
uv run --project . python \
  packs/ingestion/primitives/deep_context/cluster_merge_candidates.py
```

Blocking (shared phone/email/email-local-part/name) + a name-similarity gate only
pick which pairs are worth judging — the DECISION is always a **high-reasoning LLM
judge** that weighs ALL evidence holistically (identity, role in your life,
content/behavior, and tone where available). Writes `merge-candidates.csv` (+ a
full `merge-verdicts.csv` audit log incl. rejections) and injects a "Possible same
person" section with the judge's reason. ~$0.004/pair (only ambiguous pairs are
judged). `--no-llm` is an offline/test fallback only. **Suggestions only — never
auto-merges.**

### Step 5 — purge raw bodies (recommended)

```bash
rm -rf .powerpacks/deep-context/raw
```

### Lookup (the user-facing query)

```bash
uv run --project . python packs/ingestion/primitives/deep_context/lookup_person.py \
  --name "Jane Doe"
uv run --project . python packs/ingestion/primitives/deep_context/lookup_person.py \
  --phone "+1 415 555 1234"
```

Pure local index read (no DB, no network). `--email` and `--json` also supported;
name falls back to an all-tokens fuzzy match.

## Outputs

```
.powerpacks/deep-context/
├── raw/<person_id>.json        ephemeral sampled bodies (gitignored; purge)
├── facts/<person_id>.jsonl     structured facts per chunk (checkpoint)
├── dossiers/<slug>.md          one dossier per person
├── index.json / index.md       name/phone/email -> slug lookup + catalog
├── merge-candidates.csv / .md   likely same-person clusters
├── parents/<slug>.md            canonical person (one per real person)
└── reconcile/                   Phase 3 LinkedIn self-heal
    ├── verdicts.jsonl / .csv     ⭐ the review UI's display source (parent + profile + reasoning)
    ├── summary.md                text fallback of what changed + what needs review
    ├── applied.csv               preview of what the override will do (kept/detached) — drill-down
    └── deep-research/            Parallel.ai re-research of wrong_person detaches
#   review it interactively:  bin/deep-context review  (joins verdicts.jsonl ⨝ review.csv)

# durable self-heal decisions (fan-in MERGE inputs, re-applied every merge):
.powerpacks/network-import/overrides/review.csv   detach|verify|retarget + approved
.powerpacks/network-import/overrides/retarget-people.csv      enriched re-attach rows
.powerpacks/network-import/overrides/consolidate-people.csv   children's contacts folded onto kept link
```

## Performance & scale (measured)

Benchmarked on ~300 contacts (4.6 GB msgvault + 165k-message chat.db), Apple silicon:

| Stage | Speed | Peak RAM | Cost |
|---|---|---|---|
| collect | ~250 contacts/s (~1,800 msgs/s) | **74 MB** | free |
| synthesize | ~10 chunks/s (high concurrency) | **187 MB** | ~$0.005/contact |
| compose + cluster | <1 s total | <100 MB | free |

**Cold start (~300 contacts): ≈30 s, ≈$1.40.** Scales ~linearly:
~1k → ≈$5 / ~2 min · ~3k → ≈$14 / ~4 min · ~10k → ≈$48 / ~14 min. Memory stays
**flat (~200 MB)** regardless of contact count — it's bounded by `--concurrency`,
not corpus size (every source query is per-person `LIMIT`-bounded; one person's
window in memory at a time). `synthesize --dry-run` prints `estimated_cost_usd`
and `estimated_wall_seconds` before any spend.

**Incremental deepening:** collection pools up to `--deep-cap` (default 1600)
recent messages per person and reports the TRUE total (`messages_available`), so
`people_capped` honestly flags high-volume contacts (a spouse can have 100k+
DMs). Synthesis then **groks incrementally**: it refines ONE running profile
batch-by-batch (newest first) and stops when the profile reaches
`--target-confidence` (0.85), OR `--saturation-rounds` (2) batches add nothing
new, OR it runs out of pooled messages, OR it hits `--max-batches` (20). Each
dossier reports what happened: _"grokked 1600 of 101558 messages over 4 batches
(stopped: exhausted)."_ Most contacts finish in one batch; only deep
relationships spend more.

**Richness (message-derived only):** relationship 99%, employer ~73%, title ~29%,
location ~28%, school ~5%, ~4 topics, ~2.5 timeline events (iMessage people
average ~4.4 events). Career fields (title/school) are low because message bodies
rarely state them — they live in `people.csv` (LinkedIn) and are intentionally
NOT fused here, so the dossier reflects pure message inspection.

## Notes

- Single fixed output dir, overwrite-in-place; manifest + outputs only (no ledgers,
  no run ids).
- People with **0 messages produce no dossier** — cost scales with real interaction.
- **Incremental re-runs:** collect skips people whose bundle exists; synthesize skips
  people whose facts exist. Only *new* people are processed. To pull *new messages*
  for an existing person, re-run with `--force`.
