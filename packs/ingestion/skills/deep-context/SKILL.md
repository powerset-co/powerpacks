---
name: deep-context
description: Build the richest per-person markdown dossier from local message bodies (Gmail + iMessage DMs + WhatsApp DMs) and retrieve it by name/phone/email; surface likely same-person merge candidates; verify each person's attached LinkedIn is really them (self-heal). Use for $deep-context, "build deep context", "context/dossier on a person", "who is <phone/name> in my messages", "find duplicate people to merge", "check the LinkedIn we attached is the right person".
---

<!--
Created: 2026-06-21
Changelog:
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
  It writes a durable override table (network-import/overrides/linkedin-reconcile.csv) that
  the fan-in merge (merge_network_sources) re-applies every run: detach clears the wrong link
  (LinkedIn-only people.csv then drops that person), verify annotates linkedin_verified.
  Survives re-merges + Modal index rebuilds.
- 2026-06-23: Retargeting + approval-aware decisions table. The override gains an `approved`
  column (auto = high-confidence applies; yes/no = user decision, sticky across re-runs) and a
  `retarget` action. Deep research proposes a correct LinkedIn (pending); `apply-retargets`
  enriches it (cache-first RapidAPI) into overrides/retarget-people.csv, which the merge
  auto-ingests so the person re-appears with the correct profile (old wrong link dropped).
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

**FIRST, before running anything: create a literal, visible checklist with all
the steps below and step through it, marking each complete as you go.** Mandatory.
Use your harness's plan/task tool:

- **Claude Code:** `TaskCreate` one task per step (P1.1–P3.7), then `TaskUpdate`
  each to `in_progress` then `completed`.
- **Codex:** `update_plan` with the steps, updating status as you go.
- **Any other harness:** its equivalent todo/plan mechanism.

Seed the checklist with these exact item titles, across three phases:

```
Phase 1 — Build one deep dossier per person
P1.1  Check connections (bin/deep-context check)
P1.2  Ask group opt-in + confirm per-person message cap
P1.3  Collect deep pools per chosen settings
P1.4  Dry-run cost estimate from those settings
P1.5  Confirm estimated cost with the user (explicit go before spend)
P1.6  Synthesize + compose dossiers
P1.7  Validate completeness
Phase 2 — Merge people via the LLM judge
P2.1  Cluster candidates with the LLM judge
P2.2  Build parent dossiers (confirmed core + needs-review)
Phase 3 — Verify each person's attached LinkedIn (self-heal)
P3.1  Dry-run reconcile cost estimate (free)
P3.2  Confirm cost → run the reconcile judge
P3.3  Auto-apply high-confidence verdicts (summary; people.csv backed up)
P3.4  Surface the review queue; get user feedback on low-confidence rows
P3.5  Deep-research wrong_person detaches (auto if ≤ $25, else ask) — proposes retargets
P3.6  Open files to review (applied.csv + review-queue.csv + the decisions table)
P3.7  Apply approved retargets (enrich correct LinkedIn) → retarget-people.csv
```

Do not drop steps; mark inapplicable ones complete as a no-op. **Never run a
paid step (P1.6 / P2.1 / P3.2 onward) before its dry-run + confirm.**

### Phase 1 — Build ONE deep dossier per person

Each person gets a single child dossier from up to `--deep-cap` (1600) messages,
pooled across **Gmail bodies + iMessage DMs + WhatsApp DMs**, plus iMessage
**group-chat names** (metadata) as relationship context.

- **P1.1 Check connections** — `bin/deep-context check`. Per-source readiness +
  `ready`. If iMessage is `unreadable_full_disk_access`, run in a terminal with
  Full Disk Access (not the Claude Code Bash tool).
- **P1.2 Ask the user two things, and get answers before collecting:**
  1. **Group opt-in** — by default we read **DM bodies only** + group *names*.
     Offer `--include-groups` to also read **iMessage group-chat bodies** from
     small shared groups (`--max-group-size`, default 25). Tell them this **costs
     more** (more messages → more synthesis tokens) and pulls in other group
     members' messages.
  2. **Message cap** — we hard-cap at **1600 messages/person** (`--deep-cap`).
     They can raise it for deeper history on heavy relationships, but make sure
     they understand **a higher cap costs more**.
- **P1.3 Collect** (free, local) — `bin/deep-context collect` with the chosen flags
  (e.g. `--include-groups --deep-cap 1600`). `people_capped` flags high-volume contacts.
- **P1.4 Dry-run cost estimate from those settings** — `bin/deep-context dry`
  (re-uses the just-collected pools) → "based on your settings, this is the cost"
  (floor/ceiling + wall). The estimate reflects groups/cap because it reads the
  actual collected bundles.
- **P1.5 Confirm with the user** — present the estimate; get an explicit go. No spend
  before this.
- **P1.6 Synthesize + compose** — incremental confidence-gated synthesis, then
  deterministic dossiers + lookup index.
- **P1.7 Validate completeness** — `bin/deep-context validate` → `validation.md`
  (completeness score + flags). Act on `capped_underconfident` by raising `--deep-cap`.

### Phase 2 — Merge people via the LLM judge

- **P2.1 Cluster (LLM judge)** — `bin/deep-context cluster`. Blocking proposes pairs; a
  high-reasoning judge decides same-person holistically. Writes `merge-candidates.csv`
  + `merge-verdicts.csv` (audit, incl. rejections).
- **P2.2 Parents** — `bin/deep-context parents`. One canonical parent per cluster:
  merges only judge-CONFIRMED children (≥`--confirm-threshold` 0.85), lists borderline
  ones under "Needs review", backrefs each child. Repeatable (parent = f(confirmed children)).

### Phase 3 — Verify each person's attached LinkedIn (self-heal)

Every person in `people.csv` already has a `linkedin_url` stapled on during ingestion —
often resolved on thin same-name evidence. Phase 3 throws a **high-reasoning judge** at
each `(parent dossier ↔ attached LinkedIn)` pair: same human or not? It uses corroboration
(employer / school / location / role / behavior) and especially **contradictions** — never
the name alone (a big-company CEO profile stapled to your plumber of the same name is the
case this catches).

- **P3.1 Dry-run cost estimate (free)** — `bin/deep-context reconcile --dry-run`. Prints
  task count, link conflicts, and a cost floor/ceiling. No spend, no writes.
- **P3.2 Confirm → run the judge** — present the estimate, get an explicit go, then
  `bin/deep-context reconcile`. gpt-5.2, high reasoning, one call per attached profile.
  Writes `reconcile/verdicts.csv` (+ `.jsonl` audit) and injects a `## LinkedIn identity`
  section into each parent (verdict + supporting/contradicting evidence).
- **P3.3 Write the durable override (high-confidence)** — `reconcile` does NOT mutate
  `people.csv`. It writes a **durable override table**,
  `.powerpacks/network-import/overrides/linkedin-reconcile.csv`, that the **fan-in merge
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
- **P3.4 Review queue (low-confidence)** — `reconcile/review-queue.csv` holds everything
  not auto-applied: below threshold + `needs_review` + **ambiguous** link conflicts (e.g.
  two confirmed, or a needs_review in the mix), with a blank `user_decision` column.
  Surface these rows to the user and apply their yes/no calls. Some people legitimately
  have **no LinkedIn** (flagged `linkedin_plausibly_absent`) — never force a match.
- **P3.5 Deep research (default, $25 gate)** — for high-confidence `wrong_person`
  detaches that external research could resolve, find the *correct* identity:
  `bin/deep-context reconcile-deep-research --dry-run` to size it, then estimate the
  Parallel.ai cost (~$0.05/person). **If ≤ $25, run it automatically and just tell the
  user the cost** (`reconcile-deep-research`); **if > $25, stop and ask for approval**
  (`reconcile-deep-research --approve` once they agree). Needs `PARALLEL_API_KEY`. People
  flagged `linkedin_plausibly_absent` are excluded. When research finds a **correct
  LinkedIn**, it adds a `retarget` row (pending) to the decisions table for the user to approve.
- **P3.6 Open files to review** — open the review artifacts for the user:
  `open .powerpacks/deep-context/reconcile/applied.csv .powerpacks/deep-context/reconcile/review-queue.csv .powerpacks/network-import/overrides/linkedin-reconcile.csv`
  (`applied.csv` = what auto-applied; `review-queue.csv` = rows awaiting yes/no; the decisions
  table = the durable override incl. `retarget` proposals to approve). Non-macOS: platform open, or print inline.
- **P3.7 Apply approved retargets** — `bin/deep-context apply-retargets`. For each decisions
  row with `action=retarget` and `approved` ∈ {auto,yes}, it enriches the correct LinkedIn
  (cache-first; RapidAPI only on a miss — auto, effectively free) and writes an enriched
  re-attach row to `.powerpacks/network-import/overrides/retarget-people.csv`, carrying the
  contact's emails/phones/interaction. The fan-in merge **auto-ingests** that file (old wrong
  link detached → dropped; correct enriched row kept). Realize on the next merge + index rebuild.

**ALWAYS end the run with a summary of what changed and why.** After Phase 3, present a short
report to the user, every time (do NOT list the verified/unchanged links — only what changed):
- **Detached:** M wrong links removed — list them with the one-line reason (e.g. "Herman Au:
  LinkedIn is a Canada software lead, but your contact is a Pasadena wedding photographer").
- **Retargeted:** R people re-attached to a correct LinkedIn — for each, the new URL **and the
  reason** (why the new profile is the right person, e.g. "matched the wedding-photography
  business + SoCal location from your messages").
- **Needs your input:** K rows in `review-queue.csv` / pending `retarget` rows to approve.
Pull reasons from `reconcile/applied.csv` + `verdicts.csv` (detaches) and the `reason` column of
the decisions table (retargets). Keep it scannable; the user runs this repeatedly to fix things,
so make "what changed and why" obvious each time.

`bin/deep-context run [--include-groups] [--deep-cap N]` chains Phases 1–2 once P1.5 is
confirmed (Phase 3 is run separately, after parents exist). Full surface:
`check|dry|run|collect|synthesize|compose|cluster|parents|reconcile|
reconcile-deep-research|apply-retargets|validate|lookup|probe|purge-raw`.

### Step 1 — collect (local, free, reads bodies)

```bash
uv run --project . python \
  packs/ingestion/primitives/deep_context/collect_person_context.py
```

Streams each person's Gmail + iMessage-DM + WhatsApp-DM messages into one ephemeral
bundle per person (≥1 message). Pools up to `--deep-cap` (1600) recent messages and
records the TRUE total (`messages_available`) so `people_capped` is honest. Test with
`--limit 5` or `--person <id>`. Idempotent: re-runs skip existing bundles (`--force`).

### Step 1.5 — owner context (optional, free, big relationship win)

Drop a `.powerpacks/deep-context/owner.json` with YOUR bio timeline (name, emails,
education + work with year ranges). Synthesis injects it as a reasoning anchor so the
model infers **shared context** — same school/employer/place/era — from message
content, rendered as a "Shared context with you" dossier section. Example:

```json
{"name": "Jane Doe", "emails": ["jane@x.com"],
 "education": [{"school": "MIT", "start": 2008, "end": 2012}],
 "work": [{"company": "Stripe", "title": "Eng", "start": 2014, "end": 2019}]}
```

Overlaps are only asserted when message content supports them (not date-matching
alone). Run `synthesize --no-owner` to skip it. Get exact LinkedIn dates via
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
    ├── verdicts.csv / .jsonl     same-human verdict per attached profile
    ├── applied.csv               preview of what the override will do (kept/detached) — for review
    ├── review-queue.csv          low-confidence + ambiguous-conflict rows needing your feedback
    └── deep-research/            Parallel.ai re-research of wrong_person detaches

# durable self-heal decisions (fan-in MERGE inputs, re-applied every merge):
.powerpacks/network-import/overrides/linkedin-reconcile.csv   detach|verify|retarget + approved
.powerpacks/network-import/overrides/retarget-people.csv      enriched re-attach rows
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
