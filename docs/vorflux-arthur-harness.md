# Arthur Vorflux harness defaults

This document collates the operating patterns Arthur has been steering toward in
recent Vorflux/Codex work. It is intentionally model-agnostic: the harness should
route work by task shape, preserve deterministic artifacts, and add lightweight
cross-examination where it improves quality.

## Default posture

- **Prefer routing, not user hand-holding.** If the request clearly maps to a
  Powerpacks skill or repo surface, load that skill/context and proceed. Ask a
  clarifying question only when the same wording can plausibly mean two different
  product surfaces.
- **Do the work, do not only plan.** For “update”, “fix”, “ship”, or similar
  action verbs, inspect patterns, make the change, validate it, and report the
  evidence. A plan is an internal ledger, not the final deliverable.
- **Keep status terse.** Use short progress updates and final summaries with
  changed files, test commands, and blockers. Avoid long narration of obvious
  steps.
- **Never expose secrets.** It is fine to verify that a secret such as
  `ARTHUR_VORFLUX_API_KEY` is present and to rely on it through environment
  variables or secret tooling, but never echo it, paste it, commit it, or pass a
  literal secret value as a CLI argument when an env var/header/secret manager
  path is available.

## Scope and source of truth

- `AGENTS.md` is the boot-time repo instruction sheet for agents working inside
  `powerpacks`.
- `PROFILE.md` is the template rendered by `bin/agent-bootstrap` into local,
  gitignored host profiles such as `.codex/AGENTS.md`.
- This file is the longer rationale and examples document. Keep behavioral
  requirements summarized in `AGENTS.md`/`PROFILE.md`; keep elaboration here.

These changes make Arthur's defaults available to Powerpacks-backed sessions and
generated local profiles. If Vorflux adds a global account-level routing config,
mirror these policy defaults there rather than treating this repo-local doc as a
runtime enforcement layer.

## Routing policy

Use the platform default routing unless the task shape strongly suggests a
specialist:

| Task shape | Preferred route |
| --- | --- |
| Straightforward docs/config/small code edits | Default builder |
| Broad implementation, refactor, or cross-repo wiring | High-effort build agent |
| Reproduction, failing tests, production errors | Debug agent with a narrow repro |
| Noisy validation, large test batches, recall suites | Testing sub-agents |
| Ambiguous architecture/product tradeoff | Explore/review agent, then main-agent decision |
| Final quality pass on non-trivial work | 1–2 adversarial review agents |

The goal is not to hard-code favorites for individual model names. If a model is
acting shallow/lazy, narrow the prompt, provide concrete acceptance criteria, and
add independent review rather than accepting the first pass. If a model is
willing to do work but misses quality, use it for bounded execution and make a
separate reviewer challenge the result.

## Work loop

1. **Read before deciding.** Inspect `AGENTS.md`/`CLAUDE.md`, relevant skill
   files, docs, schemas, tests, and existing call sites before choosing an
   implementation.
2. **Track the task.** Maintain a concise todo/plan for multi-step work.
3. **Make bounded edits.** Preserve user changes and avoid unrelated cleanup.
4. **Validate narrowly first.** Run the smallest deterministic check that covers
   the change, then broader checks when risk or repo guidance calls for them.
5. **Cross-examine when useful.** For non-trivial changes, ask independent
   reviewers to look for regressions, missing tests, privacy/security mistakes,
   stale docs, and cross-repo coupling.
6. **Close the loop.** Fix real reviewer findings, rerun affected checks, and
   report changed files plus exact validation evidence.

## Testing posture

- **Powerpacks:** new tests live in `tests/` and run with
  `uv run --project . python -m unittest discover -s tests`. Prefer the venv
  Python if local learned knowledge says bare `python3` misses dependencies.
- **Network Search API:** use PR-safe checks for normal PRs:
  `uv run ruff check api_v2/ shared/ --select F821`, unit tests with a dummy
  OpenAI key, and component tests. Live verification/eval/recall tests are a
  separate tier.
- **Recall suites:** never run all recall tests in one shot. Batch by category
  and use testing sub-agents for parallelism/noisy output.
- **Data/pipeline mutations:** dry-run or `--limit 5/10` first, use dev for
  writes, and only scale after schema/auth/resume behavior is verified.
- **Docs-only changes:** run a lightweight grep/readback/self-review unless the
  docs are generated or the repo has a markdown check.

## Cross-examination agents

For meaningful code, workflow, or shipping changes, use one or two reviewers when
the host exposes sub-agent/review tooling. If it does not, do an explicit
self-review using the same checklist and state that independent review was not
available in that host.

### Correctness reviewer

Prompt with:

- the user request;
- the files/diff changed;
- relevant repo instructions/contracts;
- tests already run and their output.

Ask it to find correctness issues, edge cases, contract drift, missing tests,
and safer validation commands. It should return actionable findings with file
and line references where possible.

### Adversarial reviewer

Prompt with the same context, but ask it to challenge assumptions:

- Did the implementation actually satisfy the user’s intent?
- Are there hidden cross-repo, deployment, permissions, or data-migration
  consequences?
- Are privacy/security constraints preserved?
- Did the main agent over-scope, under-scope, or skip obvious verification?

Reviewers should not make edits by default. The main agent decides which findings
to accept, applies fixes, and reruns targeted tests.

## Shipping and release work

- If a task touches multiple repos, do not claim it is shipped until every
  touched repo has a PR/branch disposition and the requested deploy target is
  verified.
- For preview/prod shipping in `network-search-api`, use the repo’s canonical
  cross-repo shipping script and session-specific state file.
- Include PR links, workflow/deploy URLs, and concrete post-deploy verification
  in the final response.
