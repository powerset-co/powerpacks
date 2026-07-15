---
name: deep-setup
description: The centralized post-import processing layer. Use for $deep-setup, "process my contacts", "resolve my imported contacts", or after any import skill offers it. Builds per-person context across Gmail + iMessage/WhatsApp (people AND the imports' research candidates), merges duplicates, verifies attached LinkedIns, runs ONE dossier-informed reverse lookup for unidentified contacts (Parallel.ai, budget-gated), assembles synthetic profiles for the rest, then realizes everything: fan-in merge + Modal index rebuild + validation. Spend-gated at every paid stage with a mandatory human review stop.
---

<!--
Created: 2026-07-16
Changelog:
- 2026-07-16: Initial skill — the processing half of the import refocus. The
  import skills ($setup / $import-gmail / $import-messages) now only sync
  contacts down (people.csv + candidates.csv); this skill owns everything that
  thinks or spends: dossiers, duplicate merge, LinkedIn self-heal, the single
  reverse lookup over candidates + detaches, synthetic profiles, review, and
  the fan-in + Modal index + validate finale. A skill-level fork of
  $deep-context sharing the same primitives (deep_context package) — that
  skill remains the ad-hoc dossier/lookup/self-heal surface.
-->

# deep-setup

Use this for `$deep-setup`, "process my contacts", "resolve my imported
contacts", or when an import skill's final step asked *"process your contacts
now?"* and the user said yes.

The import skills are contact-sync only. This skill is where identity happens:
it builds one **markdown dossier per contact** from actual message bodies —
including the **research candidates** the imports staged
(`import/gmail/candidates.csv`, `import/messages/candidates.csv`) — merges
duplicates, verifies every attached LinkedIn, runs **one reverse lookup per
unidentified contact with their dossier as context** (that context is exactly
why this beats resolving at import time), mints synthetic profiles for people
who plausibly have no LinkedIn, and finally **realizes** it all: fan-in merge →
Modal index rebuild → validation.

It shares its primitives (and gates) with `$deep-context` — that skill remains
the surface for ad-hoc dossier builds, person lookups, and re-reviews.

## Reads message bodies (by design)

Same scoped exception as `$deep-context`: Gmail threads and iMessage/WhatsApp
**DM** bodies are read to build dossiers. Group **names** are metadata; iMessage
group bodies only after an explicit `--include-groups` approval this run;
WhatsApp group bodies never. Raw samples live in `.powerpacks/deep-context/raw/`
(ephemeral, gitignored); dossiers store synthesized facts, not verbatim text.
iMessage needs macOS **Full Disk Access** — run collect in your own terminal
(e.g. `bin/deep-context collect`), not the Claude Code Bash helper.

## Prerequisites

- A merged network: `.powerpacks/network-import/merged/people.csv` (any import
  skill's fan-in produces it).
- Candidates from the imports (either file may be absent):
  `.powerpacks/network-import/import/{gmail,messages}/candidates.csv`.
- msgvault synced for Gmail; Full Disk Access for iMessage; wacli for WhatsApp.
- `OPENAI_API_KEY` in `.env`; `PARALLEL_API_KEY` for the reverse lookup;
  Powerset runtime keys (Modal) for the index rebuild.

## How to run this skill

**FIRST, create a literal, visible checklist with all the steps below and step
through it, marking each complete as you go.** Mandatory (TaskCreate /
update_plan / your harness's todo tool).

**Never rely on memory for ANY approval — always ask and confirm with the user,
every run.** Every gate here is interactive: the owner-LinkedIn ask, the
group-chat/cap choices, every measured cost confirmation (synthesis, cluster,
reconcile, reverse lookup), the review wait, and the Modal upload. A remembered
"yes" from a previous run or a harness memory layer never satisfies a gate.

**Run the whole checklist to COMPLETION**, pausing ONLY at the explicit gate
items. Do not run `bin/deep-context run` (intentionally disabled — one chained
command cannot pause for the required approvals). A `--force` rerun keeps the
full checklist and only changes incrementality on `collect`/`dry`/`synthesize`.

Seed the checklist with these exact item titles:

```
[Scope]     Check sources, people, and waiting candidates (free)
[Context]   Ask for YOUR LinkedIn and build your own profile
[Context]   Ask whether to include group chats, and how far back to read per person
[Context]   Gather each person's and candidate's messages
[Context]   Estimate the cost before anything is spent
[Context]   Confirm the cost with you before spending
[Context]   Build a profile for each person from their messages
[Context]   Compose dossiers and the lookup index locally
[Merge]     Find duplicates (including candidates who match people you already have)
[Merge]     Combine each set of duplicates into one person
[Identify]  Preview the LinkedIn check (free, nothing spent yet)
[Identify]  Confirm cost, then check each person's attached LinkedIn is really them
[Identify]  Reverse-lookup identities once (candidates + wrong-link detaches, Parallel)
[Identify]  Assemble synthetic profiles for people with no LinkedIn
[Identify]  Open the review page
[Identify]  Wait for you to finish reviewing (keep / detach / fix) before continuing
[Identify]  Re-attach the correct LinkedIns you approved
[Realize]   Apply your decisions to the network (fan-in merge)
[Realize]   Rebuild the search index on Modal (~5-30 min)
[Realize]   Validate the search index
```

Do not drop steps; mark inapplicable ones complete as a no-op (e.g. no
candidates staged → the reverse-lookup step may cover only detaches).

### Repo root

Run from the canonical repo root (same resolution as every ingestion skill):
`$POWERPACKS_REPO_ROOT`, else `~/powerpacks`, else `~/workspace/powerpacks`.

---

## The checklist

### [Scope] Check sources, people, and waiting candidates (free)

```bash
bin/deep-context check
uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/status.py status
```

Report per-source readiness (Gmail / iMessage / WhatsApp), the merged people
count, and the **candidates waiting per source**. If a message store the user
imported is unreadable (`unreadable_full_disk_access`), stop and fix access
first. If no candidates exist anywhere and every attached LinkedIn is already
verified, say so — the run may still be useful for dossiers, but set
expectations.

### [Context] — dossiers for people AND candidates

Identical to `$deep-context` Phase 1, with one addition: **collect takes
`--include-candidates`** so the imports' research candidates get dossiers too
(they're exactly the people the reverse lookup needs context for).

1. **Owner profile** — ask for the user's own LinkedIn URL; disclose that a
   cache miss calls RapidAPI and get approval; then
   `bin/deep-context owner --linkedin-url <their-url> --email <their-email>`.
   Skip the ask if `owner.json` exists — confirm its values instead.
2. **Group / cap ask** — DM bodies only by default; offer `--include-groups`
   (iMessage small groups, costs more, reads other members' messages) and
   `--deep-cap` (default 1600 per source pool). Explicit answers this run.
3. **Collect** (free, local, FDA terminal):
   `bin/deep-context collect --include-candidates` plus the chosen flags.
4. **Estimate** — `bin/deep-context dry` with the exact synthesis scope flags.
5. **Confirm the cost** — present floor/ceiling; explicit OK; no spend before.
6. **Synthesize** — run the exact command `dry` printed (OpenAI, checkpointed,
   resumable; ~$0.005/contact).
7. **Compose** — `bin/deep-context compose` (free; dossiers + lookup index).

### [Merge] — duplicates, including candidate→person matches

- `bin/deep-context cluster --dry-run` (free count/cost preview; small bounded
  spend, typically well under $1) → explicit OK → `bin/deep-context cluster`.
  With candidates in the dossier set, this is also where a candidate who is the
  same human as an existing person gets judged and folded in — the deferred
  version of the import-time "suggested" match, now decided with full context.
- `bin/deep-context parents` (free) after inspecting the judge audit.

### [Identify] — verify, reverse-lookup once, synthesize the rest

- **Preview** — `bin/deep-context reconcile --dry-run` (free; task count +
  cost floor/ceiling).
- **Verify attached LinkedIns** — explicit OK, then `bin/deep-context
  reconcile`. High-confidence verdicts land in
  `.powerpacks/network-import/overrides/review.csv` as `approved=auto`
  (verify/detach); low-confidence rows wait as pending. User-touched rows are
  sticky. The spam screen writes machine-owned `llm_reject*` columns as usual.
- **Reverse-lookup identities ONCE** — the single research pass this whole
  redesign exists for. Eligible: the imports' **candidates** (now carrying
  dossiers) plus high-confidence wrong-link **detaches**, plus — with the
  plausibly-absent opt-in — people the judge thinks may have no LinkedIn:

  ```bash
  bin/deep-context reconcile-deep-research --dry-run --include-candidates --include-plausibly-absent
  # show the Parallel.ai estimate; get explicit approval; then EVERY time:
  bin/deep-context reconcile-deep-research --include-candidates --include-plausibly-absent \
    --approve --budget <displayed-approved-estimate>
  ```

  Budget defaults to zero so a changed queue can't spend against an unstated
  ceiling. Needs `PARALLEL_API_KEY`. Found LinkedIn → pending `retarget` row
  for the user to approve; nothing found → synthetic path below.
- **Assemble synthetic profiles** — `bin/deep-context assemble-synthetic`
  (free, local). Research artifacts for people/candidates with **no** real
  LinkedIn become people-schema rows in `overrides/synthetic-people.csv`;
  completeness ≥ 0.6 auto-approves, the rest wait pending for the review UI.
- **Open the review page** — `bin/deep-context review`. One row per person:
  matched LinkedIn(s), verdicts, evidence, dossier; candidates and synthetic
  rows carry their no-LinkedIn badge. Keep / Detach / Fix autosave into
  `overrides/review.csv`.
- **Wait for the user to finish reviewing — hard stop.** Tell them the page is
  open; do not proceed until they say they're done. Opening the page is not
  reviewing it.
- **Apply retargets** — disclose that approved LinkedIn URLs hit RapidAPI on
  cache misses; explicit OK; then `bin/deep-context apply-retargets` →
  enriched re-attach rows in `overrides/retarget-people.csv`.

**End the Identify phase with a summary of what changed**: detached links (with
one-line reasons), retargets (new URL + why), synthetic profiles minted,
candidates resolved vs still pending in the decisions table.

### [Realize] — merge, index, validate

- **Fan-in merge** (free, local) — `bin/deep-context realize`. Rebuilds
  `merged/people.csv` with every decision applied: detaches/verifies from
  review.csv, consolidations, approved retargets, approved synthetic rows —
  and the resolved candidates now exist as real people rows.
- **Modal index rebuild** — disclose that the full merged CSV uploads to a
  workspace-shared Modal volume (operator-prefixed paths, shared caches) and
  get explicit approval, then:

  ```bash
  uv run --project . python packs/indexing/modal/linkedin_modal_pipeline.py index-people \
    --people-csv .powerpacks/network-import/merged/people.csv
  ```

  Run in the background; **expect 5–30+ minutes, mostly quiet** — long silence
  is normal, trust the running process; it prints `{"status": "completed"}` on
  success (progress: `.powerpacks/runs/setup-gmail-modal/status.json`).
- **Validate** —

  ```bash
  uv run --project . python packs/indexing/primitives/validate_search_index/validate_search_index.py
  ```

  Pass only on `status: ok`; echo the `summary`.

---

## Done

Report a terse end-to-end summary: N people + C candidates processed into
dossiers, duplicate merges, verified/detached/retargeted links, synthetic
profiles minted, candidates resolved, merged network of M people, index
validated. Remind the user that `$deep-context` remains available for ad-hoc
lookups ("who is <name/phone>?"), re-reviews, and the review UI, and that
re-running `$deep-setup` is incremental (only new people/messages spend).

## Notes

- Fixed output dirs, overwrite in place; manifests + outputs only (no ledgers,
  no run ids). Artifacts live under `.powerpacks/deep-context/` and
  `.powerpacks/network-import/overrides/` — identical layout to `$deep-context`.
- Candidates never enter `people.csv` directly: they become searchable only as
  approved retargets (real LinkedIn found) or approved/auto synthetic rows —
  both through the override files the fan-in merge already ingests.
- Purge raw bodies (`rm -rf .powerpacks/deep-context/raw`) only after all
  judging stages and debugging are done.
