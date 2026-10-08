# Install pipeline cleanup 🧹

Created: 2026-10-07

## Change log

- 2026-10-07: created.

## What runs today

`bin/onboard` → `install/onboard.py:277 main`:

1. Lock, start the one page server (`onboard.py:311`), Powerset account, credentials, agent
   connection, network (`Onboarding.run`, `onboard.py:239`).
2. Sources (`workflow.py:349`): tools, every login, every sync, then `sources.ready`
   (`workflow.py:390`, action kind `processing`).
3. Processing, in the same process (`onboard.py:330`): `ProcessingOnboarding.run`
   (`pipeline.py:283`):
   - `_prepare` (`pipeline.py:183`): LinkedIn connections on Modal when newer, `archive_v1`,
     owner, `load`, `collect`.
   - `_discover` (`pipeline.py:203`): `synthesize`, `dedupe`, `worth`, each estimated and gated
     at $500 (`_paid`, `_approval`).
   - `_enrich` (`pipeline.py:220`): `enrich`, skipped with `enrich.deferred` when it fails.
   - `_index` (`pipeline.py:233`): `realize`, then a Modal dispatch state machine
     (`pipeline.py:237-266`, `287-303`), validation, `search.ready`.

`bin/deep-context-v2 run` (`deep_context_v2/run.py:222`) walks the same seven stages again,
hard-coded a second time.

## Dead or duplicated

1. **Two homes for the stage order.** `pipeline.py:200-236` and `run.py:226-233` each list
   load → collect → synthesize → dedupe → worth → enrich → realize. They already drifted: the
   install never runs `share` after `realize` (`run.py:159` does).
2. **The spend approval.** Arthur's rule for v2 is "nothing to approve; the install budget
   always says yes", and `run.py:3-4` already runs every estimate. The install still carries
   `SpendStep`, `_paid`, `_approval`, `_AUTO_SPEND_USD`, `spend.approval` and
   `--approve-spend` (`onboard.py:286`, `bin/bootstrap:50`). It is broken as well as
   unwanted: `onboard.py:286` accepts `cluster` (a v1 name), and
   `SpendStep("cluster")` raises `ValueError`; the continuation it writes for `dedupe` or
   `worth` is refused by argparse.
3. **The index dispatch state machine.** sha256/mtime bookkeeping, `dispatch_path`, `_capped`,
   `index.recovery`, `index.resuming`, the `download --wait` command and a local
   `estimate_run` pass exist to ask before a large index and to avoid re-dispatching. With
   nothing to approve, the cap branch cannot happen. `run.py:9-10` relies on the Modal cache
   instead ("the second build costs about nothing") and builds the index with
   `index_command`/`index_env` and the operator id check (`run.py:165-190`); the install has
   its own copy of the command without the operator id check.
4. **Events nothing writes.** `discover.merging`, `discover.checking`, `discover.reusing`,
   `discover.composing`, `discover.validating`, `profiles.deferred` and `index.profiles` belong
   to v1's fan-in, compose, validate-dossiers and profile prefetch (`pipeline.py:11-13`). Only
   `status_prose.py` and `prose_cli._RUN` name them. `index.estimating` goes too, with item 3.
5. **The review step.** No event has `InstallStep.REVIEW` as its step, and `workflow.py:128`
   leaves it out of the plan, so the "Waiting for your review" row never renders. The review
   has been offered from the ready step since #701 (`InstallPage.tsx:107`). `title.review`
   (`InstallPage.tsx:88`) is unreachable for the same reason.

## Target shape

- `run.py` owns the order. A single `stages(conn, data_root, ...)` function returns the
  `(name, node)` pairs. `run` walks them, and so does the install, wrapping each one in its
  page event. `realize` (with `share`), `index_command`, `index_env` and
  `resolve_operator_id` come from `run.py` as well.
- `pipeline.py` keeps only what belongs to the install: the LinkedIn Modal import, the owner
  from the LinkedIn session, the page events, enrich deferred on failure, a foreground index
  build, validation and `search.ready`.
- Index rule: when `realize` leaves people.csv byte-identical and an index is present, the
  install skips the build and revalidates. Otherwise it runs `index_command`. Any other
  failure stops the step, and the rerun dispatches again.
- `--approve-spend` and every approval event or action kind are removed. The SKILL.md
  "Approval and repair" section says paid steps run without asking.

What the user sees stays the same: the same rows, the same lines for every stage that runs,
the same sign-in flow, the same validated "Search is ready: N people searchable", and the
same review offer and page at `/`. Two things change, both on purpose:

- The install now fails at the index without an operator id. This is the same rule as
  `run`/`finish`; onboarding writes the id from `/v2/team/me`.
- The share list is refreshed after `realize`.

## Untouched

- Account, sources, the logins, the default browser for sign-in, and `workflow.py`.
- Row labels, including Arthur's "Importing your data" wording, which is a separate taste
  change.
- `validate_search_index` and the validate step. `run`/`finish` do not validate, so this is
  not a duplicate.
- `enrich.deferred` and `left_to_fix`, which the Finish section of the skill reads.
- `index_progress.py`. `index-people` writes `setup-gmail-modal/status.json`, and the page
  reads it during the index step.
- `archive_v1`, which upgraders from 3.17 and earlier still need.
- The LinkedIn Modal import, which only the install does.
- `run.py`'s background index and review server.

## Verification (free only)

- `bin/onboard --help` and `bin/deep-context-v2 --help`. Import every touched module.
- `bin/status-prose` lists no removed event. `bin/status-prose play --every --no-open` plays
  to the end on a scratch folder.
- `bin/deep-context-v2 load|collect|realize` on a scratch data root under the scratchpad,
  never the real `.powerpacks`.
- `ProcessingOnboarding` on a scratch root with the paid nodes and the Modal subprocess
  stubbed, in the existing test module.
- `scripts/lint-powerpacks`, `scripts/test-powerpacks`, and `pnpm check` in `web/`.

Not verifiable offline: the claim that a re-dispatched index costs about nothing. It rests on
`run.py:9-10` and the 3.19 proof on Arthur's root.
