# Developing Powerpacks

Read this guide only when changing Powerpacks source code or preparing a PR or
release. Product workflows follow the usage guidance in the root `AGENTS.md`
and their skill instructions.

## Engineering conduct

1. **An explanatory question does not authorize code changes.** "why X",
   "what is X", "where does X happen", "did we...", "isn't it..." → answer
   and stop. No edits, no branch,
   no PR, no adjacent fix. Name an obvious fix in one line and wait for go. The
   owner should never have to type "no changes just answer me".

2. **Read the artifact before describing it.** Every behavioral claim comes from
   something read this session — the file, the CSV row count, the manifest, the
   real HTTP response. Not a summary, not memory, not "the code probably". If you
   haven't read it, say "haven't checked"; that is a complete answer.

3. **Answer in the first sentence**, mechanism after in ≤3 bullets, under ~150
   words, with the specific `file.py:line` / function / number / which-judge.
   No restating the question, no "you're absolutely right", no "that's working
   as designed", no caveat paragraph, no speculative finding stapled to a
   finished task.

4. **Severity discipline.** "critical"/"P0"/"blocker" means loses data, spends
   money wrongly, or breaks the shipped path — state the trigger. No
   reproducible trigger → "possible, unverified". When the owner pushes back:
   re-derive from the artifact and reply in one line, `confirmed: <fact>` or
   `I was wrong: <fact>`, then keep working. No apology paragraph, no re-telling
   the mistake.

5. **Use the repo's nouns**: parent, child, person, candidate, worth, review,
   dossier. Do not invent a new noun (pool, sibling, fossil, baton, dual-key) —
   it forces the owner to learn your model to read his own system. If a name in
   the code is bad, propose renaming it.

6. **Never invent an approval gate.** Confirmation is for spend and destruction
   only (see `common/gates.py`). Free, local, idempotent, under-threshold, or
   already answered this session → just run it. When a prompt is redundant,
   delete the prompt; recent releases have been mostly deleting ceremony pages,
   confirm steps, and sequencing guards that never should have shipped.

7. **Never patch a bug with a new field, flag, concept, or fallback.** Remove the
   condition that made it ambiguous — "guaranteed by design", not circular
   fixes. See "Data pipeline simplicity" and "Development ground rules" below;
   this repo is a single-user, single-process, local, file-based tool and gets
   written that way.

8. **Finish the whole ask, then ship.** Requests arrive batched; the boring UI
   fix counts as much as the interesting investigation. Report per item and say
   what you skipped. "Done" means verified in the real surface (page loaded, CLI
   printed it, count matched) — a green suite is not the surface. If the owner
   already said push/merge this session, push, merge, and hand back the link.

9. **Sub-agents** are for
   parallel, file-disjoint, writable-down work. Never for taste work the owner
   will look at — UI, copy, layout. Verify a sub-agent's output yourself before
   reporting it. If it is bad, revert it wholesale rather than patching on top.

## GitHub PR tooling

If you are running on Vorflux (Vorflux PR tools or the `vflux` CLI are available), use them for PR creation, editing, commenting, reviewing, and merging. Prefer the Vorflux GitHub App identity; only use a connected personal account when the user explicitly asks for personal attribution, and confirm the token path via the tool's `used_user_token` indicator.

Otherwise (local sessions, no Vorflux tooling), use `gh` directly. Before mutating PR actions, run `gh api user --jq .login` to verify the active account is the intended identity, and mention the verified login in your status update.

## Vorflux PR body checklist guardrail

When a Vorflux session touches multiple repositories, every non-docs PR body must include the exact `## Cross-Repo Ship Checklist` section before the PR is considered ready. Add it at PR creation time with the Vorflux PR tool body/body-file; if it is missing or stale, update the PR body with `vflux pr edit` instead of pushing a no-op commit.

Required checklist fields:
- `**Touched repos:**` - comma-separated `org/repo` slugs for every repo changed in the session.
- `**Companion PRs:**` - links to the other PRs in the session, or `N/A` only when a single repo was touched.
- `**Production deploy plan:**` - what will be deployed/merged and in what order, or why no production deploy is required.
- `**Post-deploy verification plan:**` - exact health, workflow, UI, or artifact checks that prove the shipped change is live.

If a repo has a `scripts/cross_repo_ship.py` helper, prefer using its `prepare-pr` command to generate/update this section. Before finalizing, verify the latest edited PR-body-triggered checklist run passes when the repo has a Cross-Repo Ship Checklist workflow; do not treat an older failed run as current after the body has been fixed.

## Release Please / Conventional Commit guidance

Powerpacks uses `googleapis/release-please-action` on `main`. Do not manually push version tags for normal releases. Land conventional commits on `main`; Release Please opens or updates a release PR; merging that release PR creates the GitHub release and component tag.

Releasable commit shapes for this repo:
- `fix: ...` - patch release.
- `feat: ...` - minor release, e.g. `0.1.0` -> `0.2.0`.
- `feat!: ...`, `fix!: ...`, `refactor!: ...`, or any conventional commit with a `BREAKING CHANGE:` footer - major release.
- `deps: ...` - Release Please treats dependency updates as releasable.
- `docs: ...` - can be releasable for Java/Python release types; avoid relying on docs-only commits when you need a guaranteed minor/major bump.

Usually non-releasable unless breaking: `chore: ...`, `ci: ...`, `build: ...`, `test: ...`, `refactor: ...`, `style: ...`.

This repo has a single Release Please package: `.` as Python package `powerpacks`, tagged like `powerpacks-vX.Y.Z`.

To intentionally cut a Powerpacks minor release such as `0.2.0`, merge a PR with a `feat: ...` commit/message after the release-please setup is on `main` (for example `feat: document Powerpacks 0.2.0 pipeline release`). To intentionally cut a major release, use `feat!: ...` or include a `BREAKING CHANGE:` footer. After that commit lands, wait for the `release-please` workflow to open/update the release PR, review the generated changelog/version bumps, then merge the release PR.

Manual release escape hatch: run the Release Please workflow/CLI with an appropriately scoped token only if automation is blocked. Prefer the normal release PR flow so versions, changelogs, manifests, and tags stay consistent.

**Merging the release PR is the ship gate, not bookkeeping.** Installs follow
published release tags, not `main`: `bin/powerpacks-channel` resolves the newest
`powerpacks-vX.Y.Z` tag and `bin/update-powerpacks` moves the checkout onto it.
So a fix that has landed on `main` reaches nobody until a release PR is merged —
if releases sit unmerged for weeks, users sit on weeks-old code and it looks like
the pipeline is broken when it is only unreleased.

Channels are `stable` (newest release, the default), `rc` (newest candidate or
release), and `edge` (unreleased tip of `main`, the pre-2026-07-24 behavior).
The channel is remembered as the checkout's local branch name
(`powerpacks-stable` / `-rc` / `-edge`), overridden per run by
`POWERPACKS_CHANNEL`, and `POWERPACKS_REF=powerpacks-v0.18.0` pins one exact
version for a rollback. To cut a release candidate, land a commit with a
`Release-As: 1.1.0-rc.1` footer and merge the resulting release PR; the anchored
tag pattern keeps candidates out of `stable` while `rc` testers pick them up.

## Reference fidelity

For web UI work, read `design.md` and reuse its shared tokens and table component.

Do not be lazy when the user asks to copy, mirror, reference, or base work on an
existing implementation. For frontend work, copy the referenced style, layout,
component choices, spacing, colors, and code structure as close to 1:1 as
possible unless underlying functionality makes that impossible. For backend
work, copy the same interface, logic, and behavior as closely as possible unless
the user explicitly asks not to copy it or asks for a different approach.

---

## Data pipeline simplicity (do not overengineer)

This is a small, local, file-based data pipeline. Keep it that way. Most
"robustness" features you might reach for are over-engineering here and are
explicitly unwanted.

Hard rules for any ingestion/discovery/enrichment/indexing change:

- **No ledgers.** Do not add `*-ledger.json`, step ledgers, or per-step
  state machines. A stage writes its output files plus one `manifest.json`
  in its own directory. That is the entire state contract. (Existing legacy
  ledgers are being phased out — do not add more or extend them.)
- **No run ids or batch ids.** No per-run UUIDs, no batch/job identifiers, no
  run-scoped subdirectories. Each stage writes to a single, fixed directory
  (e.g. `.powerpacks/network-import/discover/gmail/`) and overwrites in place.
  Reruns are idempotent because the output path is stable, not because of an id.
- **Manifest + outputs only.** The durable artifacts a stage produces are its
  output CSV/JSONL files and `manifest.json` (use the existing
  `write_manifest` in `imports/common.py`). Counts, status,
  timestamps, and timing go in the manifest — not a separate state store.
- **Progress goes in a file, like LinkedIn.** For user-facing progress, write
  human-readable progress into the stage's manifest/output directory and let the
  FE render it the same way the LinkedIn flow does. Do not invent a new progress
  store or a parallel event stream the FE doesn't already read.
- **Lean on what's already solved; don't rebuild it.** Resumability,
  incrementality, and dedup mostly already exist. Gmail/msgvault is already
  resumable: compute the latest synced message, pass `--after`, sync, update —
  see `infer_msgvault_sync_after` in
  `discover/gmail/msgvault/sync.py`. Do not build a new resume mechanism on
  top of it.
- **Orchestrate the per-source discover/import primitives directly.** Chain the
  existing `discover/<source>/` and
  `imports/<source>/` commands. There is no orchestrator layer
  (the generic `discover.py` runner was deleted) — do not
  build one for new pipeline flows.
- **Do not fingerprint the shared `directory.csv`.**
  `.powerpacks/network-import/directory.csv` is a cross-source aggregate, not a
  source-owned output. Treating it as a per-source fingerprint makes restored
  imports look stale and re-runs cached enrichment.

When in doubt, do the smaller thing: fewer files, fewer concepts, one
directory, one manifest. If you think a change genuinely needs more machinery
than this, stop and ask the user before building it.

---

## Development ground rules

Distilled from the 2026-07 ingestion audit (absorbs the former root
`HYGIENE.md`). These are how code in this repo is written, moved, and deleted.
The examples are ingestion-flavored but the rules apply repo-wide.

### Structure — primitives match stages, one concern per file

- Pipeline packages mirror the stages: `discover/`, `imports/`, `enrich/`,
  `deep_context/`, `logbook/`, `setup/`, with per-vertical subpackages
  (`gmail/`, `messages/`, `linkedin/`, `twitter/`) and a vertical-local
  `util.py`. No flat primitive dumps. A file that belongs to another stage is
  misfiled even if it works — move it (`imports/gmail/import_steps.py` and
  `imports/directory.py` were both discover-stage squatters once).
- Naming is symmetric across verticals: `extract_<source>.py` reads a raw local
  store into contacts (`extract_imessage`, `extract_whatsapp`, `extract_gmail`);
  the external-binary lifecycle client is a separate module or package
  (`discover/messages/wacli/` for the wacli binary, `gmail/msgvault/` for the
  msgvault binary). Reader and binary-client never share a file.
- Large drivers decompose into a clearly-named subpackage of ~200–300-line
  single-concern modules (`setup/automations/`, `gmail/msgvault/`,
  `discover/messages/wacli/`, `imports/gmail/steps/`,
  `discover/messages/channels/`); the original CLI path stays a thin entry so
  skill commands never change.

### The orchestrator pattern (channel + store)

- A **channel/step class** owns its fixed output paths (plain instance
  attributes set in `__init__` — never `@property` stubs or call-time constant
  reads added "so tests can patch") and its step chain, recording contributions
  on `self.artifacts`. A **store/orchestrator class** owns the output dir (one
  mkdir), the run loop (stop at first blocked/failed payload), and the typed
  stage manifest. Copy this *shape* per vertical; do NOT extract a base class
  unless method bodies are genuinely identical — a base that owns a 3-line loop
  is ceremony.
- **Construct-and-run.** The class constructor resolves its own config; callers
  do `GmailDiscovery(account_emails=[...]).run()`. No `discover()`/`resolve()`
  wrapper functions around a class you can init.
- **CLI = thin argparse over the same class.** `main()` parses, constructs,
  calls the method inline, `emit(payload)`, maps status → exit code. No
  `cmd_*(args)` dispatcher functions, no `set_defaults(func=...)` indirection.
  Every CLI entry keeps its `if __name__ == "__main__"` guard (skills invoke by
  file path; a missing guard is a silent no-op).

### In-process, never self-subprocess

- **Never `run_cmd(py_cmd("<our own .py>"))`.** Our primitives are classes;
  callers in the same repo import and call them, branching on the returned
  payload's `status` — not on a subprocess exit code through a JSON pipe.
- Subprocess is only for genuinely external things: the `msgvault` and `wacli`
  binaries, `gcloud`, `qrencode`, macOS `open`, and Modal re-hosting.

### Explicit inputs — no hidden registries

- Selection and configuration are explicit CLI/caller arguments: gmail is a
  repeatable `--account-email`, messages is `--include-imessage`/
  `--include-whatsapp`. No state-file fallbacks. The `accounts.json` registry
  died because nothing wrote most of it and nothing read the rest — if a config
  layer has no writer or no reader, delete it; do not defend it with defaults.
- One config door per primitive: a single resolver returning a frozen dataclass
  with documented precedence (explicit override > config default). `| None`
  params are inherit-sentinels only where a lower layer actually exists.
- Spend gates are explicit flags, not state machines: `--approve-spend`, and a
  `needs_approval` payload + exit 20 (`common/gates.py`) emitted BEFORE any
  paid call. Resume comes from artifacts on disk (fixed paths, mtimes,
  fingerprints), never from `approve`/`continue` ledger runners.

### Data plumbing style (2026-07)

- **Parse at the boundary, once.** Anything crossing into a stage — a manifest,
  a CSV row set, a queue record — is parsed into a frozen dataclass at the edge;
  everything downstream takes typed values. The smells that mean the parse
  happened too late: `isinstance(x, dict)` in business logic,
  `str(x.get("a") or x.get("b") or "")` chains, `Path(str(...))` wrapping, and
  two state keys one letter apart. Fix the boundary; do not add another guard.
- **Steps return results; nothing mutates a shared blob.** A step takes typed
  inputs and returns a typed result; the orchestrator composes returns and
  renders the manifest from them at the end. Threading an untyped
  `state`/`artifacts` dict through steps is the banned shape — transient
  bookkeeping is not data flow, and a key written but never read is dead on
  arrival.
- **Policy is a visible decision.** Classification and status-mapping logic
  lives in one small first-rule-wins function or a literal table that fits on
  one screen; loops consume its output (`classify(row) -> label`, then bucket).
  If reviewing a decision requires simulating an accumulation loop with three
  parallel lists, extract the decision.
- **Grossness is legal in exactly one place.** Cope-with-old-installs code
  lives in `primitives/common/legacy.py`, called first at stage entry, each
  entry dated with a removal condition ("delete once no install predates
  vX.Y.Z") — a countdown, not a fixture. Dead code is deleted, never
  quarantined. Tolerance for user-provided files stays at that file's parser.
  Everything after the scrub call may assume current shapes.

### Code shape and prose (2026-08, absorbed from FAB's agent notes)

- **Human-facing text is strict minimum.** Comments, commit messages, replies:
  as few words as carry the point, each picked deliberately. No superlatives,
  no praise, no "you're absolutely right" — state the cold fact and stop.
- **Name magic values.** A recurring or meaningful literal becomes a
  module-level `UPPER_SNAKE` constant, an `Enum`, or a `Literal`; a value fixed
  by a spec (HTTP 200, exit 20) is named even when used once. Self-explanatory
  one-off values stay inline.
- **Flat beats nested.** Guard clauses and early `return`/`continue` over
  arrow-shaped if-trees; no one-line `if cond: stmt` suites. A function needing
  a third indent level usually hides a decision — extract it (see "Policy is a
  visible decision").
- **No boolean traps.** A bare positional `True` at a call site is unreadable:
  make flags keyword-only, and use an `Enum`/`Literal` when the flag is really
  a mode with a name.
- **Private by default.** Module and class internals get a leading underscore.
  Promoting `_helper` to public is an interface change — do it only for a real
  external caller, and say so in the PR.
- **Let the reader breathe.** One blank line between logical blocks. A short
  what/why comment only where names don't already say it — the comment policy
  below still governs (constraints, not narration).
- **Minimal diffs.** Don't touch code unrelated to the change: no drive-by
  comments, renames, or reformatting in blocks you didn't modify.
- **Bug fix = failing test first.** Write the test, watch it fail, write the
  fix, watch it pass. The test names the bug; no fix ships without it.
- **Commit messages** (on top of conventional commits): subject imperative
  after the type prefix — it completes "if applied, this commit will …" — at
  most 72 characters, aim for 50; blank line before the body; body wrapped at
  72; body says what and why, never how — the diff is the how.

### One home per concept

- Generic helpers live once: `primitives/common/{jsonio,proc,paths,
  contact_fields,gates,manifests}.py`; cross-stage contracts in
  `packs/ingestion/schemas/`; CSV IO on `packs.shared.csv_io.CsvIO`. Before
  writing a helper or a column list, grep for it — this repo once carried
  `now_iso` ×11, `write_json` ×10, and a 33-function byte-identical fork.
- Never leave compatibility shims or package-`__init__` re-export layers after
  a move; update every call site (skills, tests, docs, bin, adapters,
  `py_cmd`-style path strings) and finish with a zero-stale-reference grep.
- Deliberately divergent variants are PINNED and documented at the definition:
  the source-tuned `normalize_name` match keys, the fingerprinted-LF vs plain
  writers, `bundle_evidence_fingerprint` (its serialization is a paid-cache
  key — changing it silently re-bills every dossier). Don't "unify" them; say
  why they diverge where they live. A pin is not a licence to diverge on
  IDENTITY: the LinkedIn slug/URL normalizers live once, in
  `schemas/people_schema.py`. `extract_gmail` used to pin a non-percent-decoding
  copy and it silently split people into two rows at the fan-in merge.

### Moving & deleting

- Verify consumers by REAL imports/invocations (grep code, not doc mentions)
  before keeping or deleting anything. Then delete dead code together with its
  tests, doc rows, skill routes, adapter list entries, and config keys — a
  retired skill also goes into every adapter's `RETIRED_SKILLS` scrub list.
- Prefer wholesale deletion over surgical preservation of surfaces nobody uses
  (the console app went as one cut). Git history is the archive; `.bkup` is for
  data files only, never code.

### Docstrings, docs, comments

- Module docstrings state CURRENT behavior, present tense, with a terse
  what-this-actually-does flow block for stage entries. Change history lives in
  a dated module-top `Changelog:` block — never in function docstrings, never
  as inline "we changed X" comments.
- No `<file>.README.md` sidecars. At most one `README.md` per directory,
  describing the directory: a mermaid data-flow plus a per-file
  role/reads/writes table.
- CLIs emit JSON; progress is terse one-line stderr with a stable prefix.

### Tests

- Patch the concrete module/class where the thing is DEFINED, never a
  re-export. Never add prod indirection to make patching easier — tests pass
  explicit params (`out_dir=...`) instead.
- Fixtures are obviously-synthetic (`Jordan Bravo`, `casey@example.com`,
  `+15550100`); never real contact PII, in code or in git history.
- After a structural refactor, lock behavior with a REAL run: execute the
  actual skill flow against real local data (a worktree gives you a free
  cold-start environment) and diff the outputs against the previous run —
  byte-identical, or every delta explained. Fast iteration is compile +
  targeted suites; full CI gates the merge.

### Checking work against these rules

The rules above are not self-enforcing. Every rule in this section has been
violated by code that shipped with a green suite: a fourth phone parser landed
next to "generic helpers live once", dict rivers landed next to "parse at the
boundary once", and 36 loose modules landed next to "one home per concept". A
passing test suite says nothing about any of them.

So before a structural change is called done, one pass must read the changed
FILES (not the diff) and answer, by name, against this section:

- Does every module hold one kind of thing? A `models.py` that also queries, or
  parses, or formats, is three files.
- Is every helper the only one of its kind? Grep before believing it is new.
- Does every name describe current behaviour rather than history?
- Is every closed vocabulary an enum, and does the logic that picks a value live
  on the type rather than in a caller?
- Is every value that could be absent handled the same way, and is that way
  "fail", not an invented default?
- Is every invariant that the database can enforce enforced in the DDL rather
  than by a Python guard?
- Does any config get resolved anywhere other than once, at construction?

A diff review cannot answer these — they are properties of whole files. Do this
pass FIRST, before correctness verification, because everything built afterwards
inherits the shape.

### Working the repo

- Fan mechanical work out to sub-agents with disjoint file ownership; each
  agent reads this section first; the parent verifies the combined tree,
  runs the affected suites, and commits. Serialize agents whose scopes touch
  the same files.
- Conventional commits; `BREAKING CHANGE:` when a CLI/contract changes. Write
  the subject as what changed, not which internal batch produced it.

---

## Development privacy

- **Development privacy — anonymize contact PII in anything committed or shared.**
  You may READ real pipeline data (`.powerpacks/`, downloaded exports, dossiers,
  review/merge CSVs, message stores) to analyze behavior, but never copy a real
  person's data out of it into a committed or externally-visible artifact —
  tests/fixtures, code comments, docstrings, commit messages, PR titles/bodies, or
  review comments. Replace real contact names, emails, and phone numbers with
  obviously-synthetic placeholders (`Jordan Bravo`, `casey@example.com`,
  `+15550100`) that preserve only the *structure* a case needs (nickname vs full
  name, shared surname, initial-only surname), never a real identity. Git history
  and GitHub are permanent and shareable: if a real contact's data already landed
  in a commit or PR, rewrite the commit / edit the body to remove it rather than
  leaving it. Investigate on real data, ship synthetic. (The mailbox owner's own
  identity may be referenced where useful — it's their data; their contacts' is
  not.)
