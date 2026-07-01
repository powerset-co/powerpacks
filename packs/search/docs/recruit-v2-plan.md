# `$recruit` v2 — local-execution sandbox + technical-skills surfacing 🧭

> **Created:** 2026-07-01
> **Status:** proposed (design + punch-list for the follow-up to PR #153)
>
> **Changelog**
> - 2026-07-01 — initial draft. Scopes the two capabilities the v1 engine did not
>   leverage (local-exec sandbox, technical-skills signal) plus the punch-list of
>   deferred robustness/doc caveats from the 4-agent review of PR #153.

---

## Where v1 landed (context)

PR #153 shipped the `$recruit` engine (source → mixture-of-judges → core-gate →
expand-from-anchor → converge) and consolidated `search-*` into one `$search`
door. A four-agent review found it a **viable v1**: the JD→shortlist pipeline
connects end-to-end, the core-gate is correct, routing holds a reproduced
**0.9375 strict** baseline, and the new tests are substantive. One real
robustness bug (single flaky probe aborting the whole shotgun) was fixed before
merge; a second (a thin/JS-rendered JD silently producing a garbage plan) is
fixed in this branch.

This doc scopes the two capabilities the user flagged as **not yet leveraged**,
both of which are genuinely absent today (no doc ever claimed them) and both of
which have the substrate already present in the repo.

---

## Capability A — local-execution sandbox (DuckDB / TurboPuffer) 🏖️

### Current state (verified)
`$recruit` sources **exclusively through the cloud path**:

```
recruit_loop → robust_source → run_shotgun
             → search_network_pipeline.py run --search-only
             → TurboPuffer hybrid retrieval + Postgres hydrate, scoped by set_id
```

There is **zero DuckDB in the recruit primitives** (`grep duckdb
packs/search/primitives/recruit/` is empty). Meanwhile a full **local DuckDB
backend already exists for `$search`** but is never imported by recruit:

- `packs/search/primitives/local/local_duckdb_store.py`
- `packs/search/primitives/local/local_search_backend.py`
- `packs/search/primitives/local/local_filter_eval.py`
- `packs/search/primitives/local/local_search_verticals.py`

It consumes `.powerpacks/network-import/merged/people.csv` and the
`.powerpacks/search-index/` artifacts — i.e. the user's **imported personal
network**, no Powerset set / TurboPuffer / Postgres required.

### The gap
`$recruit` cannot run against the local network. A user with an imported
LinkedIn/Gmail/messages graph but no Powerset set gets nothing from `$recruit`.
The user's note: *"sandbox / local execution against local DuckDB or
TurboPuffer — I never built that on the cloud/app version; we can leverage it
here."* This is a capability unique to the local Powerpacks context.

### Proposed design (phased, keep it small)
1. **Local sourcing backend for recruit.** Give `run_shotgun` / `robust_source`
   a `--backend {cloud,local}` switch. `local` dispatches each probe to the
   existing `local_search_backend` (DuckDB) instead of `search_network_pipeline
   run`, emitting the **same `ledger.json` + union shape** so `build_union` and
   everything downstream are untouched. This is an adapter, not a rewrite —
   mirror `$search`'s local subroute (`route_query.py` already classifies
   `network → local`).
2. **Sandboxed relational lane (optional, higher value).** Let the loop run
   ad-hoc read-only SQL over the local DuckDB (the `$search-sql` capability)
   as an in-loop sourcing probe — e.g. "2+ startup stints AND worked at a
   company in the JD's sector". This is where local execution beats the cloud
   set: arbitrary career-shape predicates the vector probes can't express.
   Reuse `$search-sql`'s read-only guardrails; no write path.

### Open questions (for the user)
- Local-only, or **hybrid** (local DuckDB for personal-network probes +
  TurboPuffer for the team set in the same run, merged in `build_union`)?
- Does the local judge stay `codex`/`gpt`, or do we want a local/offline judge
  so the whole loop can run with no spend?
- Is the target the **imported personal network** (people.csv) or also the
  local mirror of a set?

---

## Capability B — technical-skills surfacing + inference 🛠️

### Current state (verified)
`tech_skills` **exists as a first-class, indexed field** — this is better than
"not started":

- Built by the indexing pipeline into `person_tech_skills.jsonl`.
- A **filterable field** in retrieval (`search_network_pipeline.py` allows
  `tech_skills` filters).
- Carried on hydrated profiles and used as **free-text seed enrichment** in
  `run_shotgun.py`, `expand_from_anchor.py`, `triage_candidates.py`.

**But it is not a surfacing or ranking signal, and there is no inference:**

- `grep tech_skills judge_consensus.py build_eval_inputs.py` → **0 hits**. Skills
  are not part of the plan's must-haves or the core-gate; the judge rubric even
  treats seniority as separate from skills and does no skills scoring.
- Skills only nudge retrieval phrasing — they never gate or rank.
- **No inference** of skills from title/company/postings exists anywhere.

### The gap (as the user framed it)
1. Skills aren't used to **surface candidates faster** (they're indexed but idle).
2. The signal is **sparse** — not everyone lists `tech_skills`, so a naive
   skills filter/gate silently drops good people who simply didn't self-report.
3. Idea: **infer** implied skills from the person's role/company and from the
   company's own job postings, so sparse profiles still get a skills signal.

### Proposed design (phased)
1. **Make skills a surfacing signal (cheap, no new infra).** Add a
   skills-oriented probe archetype in `decompose_jd` / `run_shotgun` (probe on
   the JD's must-have technologies), and optionally a `skills` **core/table_stakes
   trait** in `build_eval_inputs` so the core-gate *can* require a demonstrated
   skill — but only when the corpus coverage justifies it.
2. **Graceful sparsity (the robustness the user worries about).** Never hard-gate
   on `tech_skills` presence. Treat "skill demonstrated in a position/JD" and
   "skill listed" as equivalent evidence; absence ≠ disqualification. Log corpus
   coverage so the gate auto-softens when skills are sparse.
3. **Skills inference for sparse profiles.** Derive implied skills from
   `(title + company + company job-postings)`. The substrate is already here:
   `fetch_jd.py` pulls postings, profiles carry `positions` + company history,
   and `decompose_jd` / `build_eval_inputs` already make LLM calls — so an
   inference step can fold implied skills into probes or a skills-aware trait
   **without new infrastructure**. Cache inferred skills alongside the profile so
   we don't re-infer per run.

### Open questions (for the user)
- Infer skills **eagerly at index time** (enrich `person_tech_skills.jsonl`) or
  **lazily in the loop** (only for candidates the judge is about to score)? Lazy
  is cheaper and avoids a big offline enrichment pass.
- Company-postings inference means fetching a company's live JDs — spend + a new
  fetch surface. Worth it, or is title+company enough for v2?

---

## Punch-list — deferred caveats from the PR #153 review 📋

Small, well-scoped items surfaced by the four review agents. None blocked v1;
each is a clean follow-up.

**Robustness (recruit engine)**
- [ ] **codex-judge preflight.** Default `--judge codex` hard-depends on the
      `codex` CLI + ChatGPT-subscription auth with no preflight. Add a
      `shutil.which("codex")` check at loop start with an actionable error
      ("install codex or pass `--judge gpt`"). *(Deferred from this PR only
      because a startup check needs the existing default-judge tests to mock
      `shutil.which` so CI without codex stays green — do that alongside.)*
- [x] **thin-JD guard** — done in this branch (`recruit_loop` rejects a
      sub-400-char fetched JD before sourcing).
- [ ] **`export_candidate_shortlist` in the loop.** The loop's terminal artifact
      is `shortlist/ground_truth_ranked.json`; the sendable CSV (with the real
      `source_operator`/`source_channel` provenance columns) is never exported by
      `recruit_loop`. Wire the export as the loop's last step.

**Docs / accuracy**
- [ ] **SKILL.md overstates "mixture-of-judges / cross-vendor panel."** The
      automated loop runs consensus over **one** judge file; multi-judge is only
      reachable via the manual flow. Reword the SKILL headline, and drop the
      "Claude-CLI judge" line (no such judge exists in code).
- [ ] **SKILL.md artifact-name drift** — `candidates_union.jsonl`/`BRIEF.md`/
      `candidates.json` in the recipe don't match what the loop writes
      (`union.jsonl` + `master_union.jsonl`, no `BRIEF.md`,
      `candidate_frontier.jsonl`).
- [ ] **Stale `search-network` command strings** in secondary surfaces:
      `packs/search/docs/search-surface.md` (and the test that *enforces* it,
      `tests/test_core_layout.py:207`), `adapters/pi/install.sh:103` summary echo,
      `.pi/team/manifest.yaml:17`, nanoclaw example strings, and the generated
      `docs/skills-map.html` (regenerate).

**Test coverage**
- [x] **`run_shotgun` partial-failure + success paths** — added in PR #153's
      final commit.
- [ ] **End-to-end multi-epoch convergence test** — every `recruit_loop` test
      stops at epoch 0 / the plan gate; the expand-from-anchor → re-judge →
      converge cycle is only tested piecewise.
- [ ] **`fetch_jd.fetch` network error paths** (non-200 / redirect / timeout).

---

## Recommended sequencing

1. **Skills as a surfacing signal (B.1 + B.2)** — highest value / lowest cost;
   the field is already indexed, so this is mostly wiring + a graceful-sparsity
   rule. Ships candidate-quality wins fast.
2. **Local-exec sandbox (A.1)** — unlocks `$recruit` for the imported personal
   network; medium effort, mostly an adapter over the existing local backend.
3. **Skills inference (B.3)** and **sandboxed SQL lane (A.2)** — the deeper bets;
   do after 1–2 validate, and after the two open-question sets are answered.
4. Fold the punch-list items in opportunistically as each area is touched.
