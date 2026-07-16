---
name: deep-context
description: The single post-import people-processing workflow and per-person dossier surface. Use for $deep-context, "process/resolve/enrich my contacts", "build deep context", a dossier or identity lookup by name/phone/email, duplicate-person review, LinkedIn self-heal, or the staged people/LinkedIn UI. Builds dossiers for imported people and unresolved Gmail/iMessage/WhatsApp candidates, merges duplicates, asks the user only about uncertain additions, runs one budget-gated lookup for the editable Added pile plus eligible wrong-link recovery, verifies found LinkedIns, then realizes the approved network and index.
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
[Scope] Check sources, people, and unresolved candidates
[Context] Confirm the owner profile
[Context] Confirm group-body access and dossier depth
[Context] Collect messages for people and candidates
[Context] Preview and approve dossier synthesis
[Context] Build and validate dossiers
[Merge] Preview and approve duplicate resolution
[Merge] Build canonical people
[People] Open the Yes/No people stage and wait for completion
[Identify] Preview and approve attached-LinkedIn checks
[Identify] Preview and approve one lookup for Added candidates and eligible wrong links
[Identify] Assemble researched profiles without LinkedIn
[LinkedIn] Open the Yes/No LinkedIn stage and wait for completion
[Identify] Apply approved replacement LinkedIns
[Realize] Fan-in approved decisions to people.csv
[Realize] Approve and rebuild the Modal index
[Realize] Validate the index
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

Inspect `.powerpacks/deep-context/owner.json`. If it exists, show and confirm its
non-secret identity fields. If it does not, ask for the user's LinkedIn URL and
email. Disclose that a profile-cache miss calls RapidAPI and get approval before:

```bash
bin/deep-context owner --linkedin-url <url> --email <email>
```

### 2. Message scope

Ask both questions in this run:

1. Group bodies: DM-only (default) or small iMessage groups
   (`--include-groups`). Explain that group mode reads other participants'
   messages and costs more. Never include WhatsApp group bodies.
2. Depth per person/channel:
   - shallow: `--deep-cap 400`
   - medium: `--deep-cap 1600` (default/recommended)
   - deep: `--deep-cap 6400`

For full processing, candidates are always included:

```bash
bin/deep-context collect --include-candidates --deep-cap <cap> [--include-groups] [--force]
```

Collection is local/free. Preserve the exact approved flags through synthesis.

### 3. Dossiers

Run the free estimate:

```bash
bin/deep-context dry
```

Show its contact count and cost floor/ceiling. Get explicit approval, then run
the exact `bin/deep-context synthesize ...` command printed by `dry`. Do not
invent a different scope. Synthesis also produces the model's display-only
`network_worth` recommendation and reason.

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

Show the estimate and get explicit approval before `bin/deep-context cluster`.
Then inspect its audit output and run:

```bash
bin/deep-context parents
```

Candidate dossiers participate, so candidate-to-existing-person merges happen
with message context before any paid identity lookup. A candidate merged into an
existing person does not reappear in the People queue or paid lookup; reconcile
folds its email/phone/channel metadata onto the kept LinkedIn instead.

### 5. People decision gate

Launch the first binary stage in a background terminal:

```bash
bin/deep-context review --stage worth
```

The main queue shows only people the model marked `maybe`, one at a time with
Yes/No. Model Yes starts in the editable Added pile. Model No/spam, user No,
and legacy Exclude share the editable Rejected pile. The user can inspect either
pile and move people between them. When no maybes remain, the UI reveals one
Continue button; completing it records the handoff in the manifest. Everyone
currently in Added is eligible for the separately approved paid lookup.

Hard stop. Poll this fixed file:

```text
.powerpacks/deep-context/review/manifest.json
```

Continue only when all are true:

- `stage == "worth"`
- `status == "completed"`
- `counts.pending == 0`
- the manifest was rewritten after this stage was launched

Do not infer completion from the browser opening, from model decisions, from
`review.csv`, or only from the user saying "done". If the file is absent, stale,
malformed, or for the wrong stage, keep waiting and report the exact condition.

### 6. Identity preparation and one lookup

First preview the attached-LinkedIn judge:

```bash
bin/deep-context reconcile --dry-run
```

Show the OpenAI estimate, get fresh approval, then run
`bin/deep-context reconcile`.

Next preview the single Parallel pass. Candidate eligibility is the current
Added pile (model Yes unless the user removed it, plus anyone the user added).
The command also covers eligible wrong-link recoveries and plausibly-absent
LinkedIns:

```bash
bin/deep-context reconcile-deep-research --dry-run --include-candidates --include-plausibly-absent
```

Show gross eligible, completed-result reuse, duplicate handles skipped, net-new
submissions, price/person, total estimate, and the proposed budget. The approval
amount is based on net-new submissions only.
After explicit approval, always pass the approved cap:

```bash
bin/deep-context reconcile-deep-research --include-candidates --include-plausibly-absent \
  --approve --budget <approved-estimate>
```

Budget defaults to zero; never omit it on an approved run. Then build local
fallback profiles for researched Yes people with no real LinkedIn:

```bash
bin/deep-context assemble-synthetic
```

### 7. LinkedIn decision gate

Launch the second binary stage:

```bash
bin/deep-context review --stage linkedin
```

The first review server stays alive. This command activates and reuses it when
port 8765 already belongs to the deep-context reviewer, so do not kill the first
background process or start a second ad-hoc server. The waiting page also advances
automatically when lookup artifacts appear.

For a found/existing LinkedIn the question is simply whether it is the right
person. Yes verifies it; No detaches only that link, never rejects the person.
"Use a different LinkedIn" is secondary. For a synthetic result, Yes/No decides
whether to add the researched no-LinkedIn profile.

Hard stop and poll the same manifest. Continue only when:

- `stage == "linkedin"`
- `status == "completed"`
- `counts.pending == 0`
- it was rewritten after the LinkedIn stage launched

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
