# `$search` decision eval — agent-in-the-loop routing benchmark

_Created: 2026-07-01 (supersedes routing_eval.md)._

_Changelog:_
- _2026-07-01: rewritten as the agent decision eval. `route_query.py` is deleted — the model
  IS the router — so the eval now runs a REAL agent (codex/claude CLI) per case against the
  SKILL's own decision rules, and scores the recorded `decision.json` axes
  (surface / backend / depth). Fixture relabeled + extended to 68 cases._
- _2026-07-01: (as routing_eval.md) folded $recruit into $search deep mode._
- _2026-06-30: initial as routing_eval.md. Deterministic `route_query.classify` + 48-case
  labeled fixture; recorded baseline strict 0.9375._

## What is measured

Each labeled query is sent to a real agent together with the decision rules extracted
**verbatim** from `packs/search/skills/search/SKILL.md` (between the
`<!-- decision-rules:start/end -->` markers — the eval can never drift from what production
agents read). The agent returns the Step-1 decision JSON; we score three axes against labels:

- **surface** — `people | company | sql | contacts`
- **backend** — `powerset | local` (explicit user words win; unstated falls to the case's
  `env` assumptions, default `{local_db: true, remote_creds: true}` → `powerset`)
- **depth** — `fast | deep` (JD / job-posting URL / explicit deep asks → `deep`; depth is
  unlabeled — `null` — for non-people surfaces and skipped in scoring)

Strict = all labeled axes exact. Lenient = each axis within label ∪ its `acceptable_<axis>`
alternates. Per-axis accuracy and confusion matrices are reported so a backend miss and a
depth miss read as the different bugs they are. Agent errors (timeout / unparsable output)
count as misses, never dropped.

## Fixture

`packs/search/evals/decision/cases.json` — 68 labeled queries:

- every surface ≥ 8 cases; each backend condition ≥ 6; `deep` ≥ 6 (enforced by
  `tests/test_search_decision.py::TestCasesIntegrity`)
- explicit-backend wording ("search in powerset ...", "search local: ...", "offline",
  set names, team network)
- unstated-backend cases with varied `env` (local-only, remote-only, both)
- `reg-*` regression cases for the old classifier's live-reproduced misroutes
  ("worked with Kubernetes", "early career", "look up Jane Doe")
- depth cases: pasted JD / job URLs (auto-deep), explicit deep asks, an explicit fast
  override on a URL, and input-shape crosses (JD+local, URL+powerset)

## Run it

```bash
# codex (default; subscription auth, no per-call cash; ~68 cases in a few minutes)
uv run --project . python packs/search/evals/run_decision_eval.py --harness codex

# claude CLI
uv run --project . python packs/search/evals/run_decision_eval.py --harness claude

# any agent, via a command template with a {prompt_path} placeholder
uv run --project . python packs/search/evals/run_decision_eval.py \
  --command-template 'claude -p "Follow {prompt_path}; output only the JSON."'
```

Report lands in `packs/search/evals/decision/report.json` (committed as the recorded
baseline). `--only <id>` runs single cases; `--min-accuracy` turns the run into a floor check.

## Floor & nondeterminism policy

Agent decisions are nondeterministic, so the floor is recorded from **two** consecutive codex
runs: floor = min(run1, run2) strict, minus one case's worth (~1.5%). The old classifier's
0.9375 route accuracy is the reference bar the surface axis must not regress.

**Recorded baseline (codex, reasoning_effort=low, 2026-07-01):** two consecutive runs both
scored **strict 0.9853 / lenient 1.0000** (surface 1.0, backend 1.0, depth 0.9762; 0 errors).
The single strict miss both times was an ambiguous under-specified case predicting `deep`
where `fast` is labeled with `deep` acceptable. **Floor: strict ≥ 0.97** (min-run minus one
case). Committed report: `packs/search/evals/decision/report.json`.

Rerun and re-record **before merging any change to the SKILL's decision-rules block** or the
fixture. CI never spawns agents; the deterministic contract (schema, fixture integrity, SKILL
drift guard, scorer) is pinned by `tests/test_search_decision.py`.
