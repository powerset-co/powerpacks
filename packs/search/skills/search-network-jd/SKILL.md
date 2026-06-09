---
name: search-network-jd
description: Run the complex JD recruiter loop for Powerpacks when the user provides a job posting URL, pasted job description, or broad multi-trait role brief. Fetch/read job URLs, classify JD requirements, build a bounded multi-probe search plan, execute probes with automatic TurboPuffer fallback, assess coverage gaps and expand, then hand off to final rerank and export.
---

# Search Network JD

Use this only for job posting URLs, pasted job descriptions, or broad role briefs
where one search would likely be too noisy or miss distinct candidate patterns.

Tasks 1-3 (intake through expansion) are harness-driven. The harness does the
JD reading, requirement classification, probe design, execution, and expansion.
Tasks 4-5 (finalize ranked pool, export shortlist) will be primitives.

Reference task spec: `packs/search/tasks/search-network-jd.task.json`

Plan schema contract:
`packs/search/schemas/search-network-jd-plan.schema.json`

---

## Task 1 — Prepare JD Plan

### 1a. Source Intake

If the input is a URL, fetch the page and persist artifacts. If the input is
pasted text, persist it directly. Do not infer search criteria from a URL string
alone.

Create a run directory:

```
.powerpacks/search-network-jd/<slug>-<timestamp>/
```

Write these source artifacts:

| File | Content |
|------|---------|
| `source.txt` | Clean text extracted from the page or pasted content |
| `source.json` | `{ source_url, source_title, fetched_at }` |
| `source.html` | Raw HTML if fetched from URL (optional, for debug) |

Use the complex route when the content has several of:

- title, responsibilities, qualifications, department, location
- hard filters plus soft/preferred filters
- OR-style experience families
- many nice-to-have skills
- archetype language (founder-capable, technical cofounder, etc.)

### 1b. Trait Extraction

Most JD text is fluff — generic qualifications like "strong communication
skills" or "ability to manage competing priorities" that any qualified candidate
would have. The real work is identifying the 4-6 traits that actually
differentiate candidates.

A **trait** is a concrete, profile-evaluable qualification that covers a cluster
of related JD requirements. If a candidate has the trait, you can assume they
satisfy the underlying requirements. Traits are what probes search for and what
candidates are evaluated against.

Traits should answer: **can this person do the job well?** Do not use traits to
model whether the company can close the candidate or whether the candidate will
accept the employment terms.

Before keeping any trait, apply this test:

1. Could this plausibly appear in a LinkedIn/profile/work-history record?
2. Would it materially change who we retrieve or how we rank?
3. Is it more specific than generic competence?

If the answer is no, do not make it a trait.

Read the full JD and extract:

#### must_have traits

The non-negotiable, profile-evaluable qualifications. Usually 2-4. A candidate
who clearly lacks these should rank below qualified matches and may be capped
low, but not every miss is an automatic exclusion. Use judgment: a truly central
named requirement can be disqualifying; an adjacent or partially evidenced
requirement should usually remain visible at a lower score.

Examples:
- Credential, license, or clearance only when the JD explicitly requires it and
  profiles can plausibly show it
- Role/function track when the JD requires a specific career lane and adjacent
  lanes would be wrong
- Seniority or ownership level when junior profiles would not plausibly perform
  the job well

#### nice_to_have traits

Differentiators that separate good from great. Usually 2-4. Candidates missing
these stay in the pool but rank lower.

Examples:
- Industry or customer context when it changes fit
- Tooling, systems, or technical stack when it is specific and profile-visible
- Niche specialization where few profiles will have explicit evidence. If the
  JD explicitly requires it, keep it as `must_have` with
  `specialization: true`; do not silently downgrade it. If the user later says
  to ignore or soften it, record a trait mutation and rerank/expand.

#### screening_gates

Some JD requirements are real but not good search traits because profiles rarely
prove them. Track these separately as `screening_gates`, not as
`must_have`/`nice_to_have` traits and not as probe targets.

Examples:
- Eligibility, authorization, or future-clearance questions when the JD does not
  require already-held profile-visible evidence
- Willingness to relocate or commute
- Work authorization questions
- Extended hours/weekends unless it is an unusual schedule requirement
- Compensation, availability, willingness to accept terms, or other close-risk
  questions

Only turn a gate into a searchable trait when the JD asks for already-held,
profile-visible evidence.

#### Search scope, not fit

Location can be a search scope when the user wants local candidates, but it is
not a job-fitness trait by itself. A probe may say "in Greater Los Angeles" when
the sourcing run is geographically scoped. Do not add "onsite candidate" as a
trait, do not put it in `targets_traits`, and do not score/rerank someone as
unable to do the job because their profile does not prove daily commute
willingness.

#### Basic requirements and seniority calibration

Do not promote every Basic Qualification into a trait. For mid-level, senior,
staff, lead, manager, or executive roles, many baseline requirements are implied
by credible work history in the target function. Do not create separate traits
for broad degrees, generic communication, generic analytical ability, or routine
fundamentals unless the JD makes them unusually specific and profile-visible.

Vague requirements are not gates. If the JD says something underspecified like
"data analysis tools", "systems aptitude", "engineering fundamentals", or
"modeling tools" without naming a tool, method, standard, or unusual depth,
treat it as a baseline assumption when the candidate has credible role-level
experience. Use it as light color in reranking at most; do not make it a
must-have trait or a retrieval probe.

Instead, calibrate the role level:

- Prefer seniority/ownership traits that exclude clearly junior candidates.
- Treat generic basics as assumed when the candidate has credible mid+ work
  history in the required lane.
- Keep a basic requirement as a trait only when it is a true differentiator,
  e.g. a named credential, named technical stack, named regulatory framework,
  active clearance, or specific hands-on domain that would not be implied by the
  title alone.

#### What to ignore

Do not create traits for generic qualifications that any qualified candidate
would have:
- "Strong analytical skills"
- "Excellent communication skills"
- "Ability to manage competing priorities in a fast-paced environment"
- "Comfort operating at both detailed and strategic levels"
- Broad degree requirements when a stronger profile-visible trait already
  implies them
- Generic baseline skills already implied by credible mid+ work history in the
  target function
- Vague tool or fundamentals language that does not name a specific
  profile-visible tool, method, framework, standard, or unusual depth
- company mission / urgency / accountability / transparency language
- compensation, benefits, EEO, and application boilerplate

These are assumed if the candidate has the real traits.

#### Trait fields

Human-facing trait names must be plain English. Do not show snake_case labels as
the trait. If an internal stable slug is useful for lineage, keep it in `key`,
but the displayed `trait` and `targets_traits` values should be readable
English.

```json
{
  "trait": "Required credential or license named in the JD",
  "type": "must_have",
  "covers": ["credential requirement", "closely related baseline requirements"],
  "specialization": false,
  "key": "required_credential"
}
```

- `trait` — the trait as you'd describe it to a recruiter
- `type` — `must_have` or `nice_to_have`
- `covers` — what JD requirements this trait subsumes (for auditability)
- `specialization` — true if this is a niche domain where few candidates will
  have explicit evidence. Do not downgrade an explicit JD requirement unless the
  user asks to soften it.
- `key` — optional internal slug for lineage. Do not use this in user-facing
  summaries.

#### Trait extraction quality checks

Before writing `plan.json`, check:

- 4-6 total traits across `must_have` and `nice_to_have`.
- No trait is just a soft skill, personality trait, mission phrase, or benefits
  text.
- No credential is invented from adjacent wording. For example, do not create a
  credential trait unless the JD explicitly asks for that credential.
- Broad degree requirements are folded into stronger track/credential traits
  unless the degree is unusually specific and differentiating.
- Location/onsite/relocation/compensation/availability are not traits. Keep
  location as search scope only when useful.
- Baseline Basic Qualifications are not hard gates when credible senior work
  history already implies them.
- Vague requirements are not hard gates. They can only become traits when the JD
  names a specific profile-visible tool, method, framework, standard, or unusual
  depth.
- The plan explicitly calibrates seniority so junior profiles are not retrieved
  for senior roles unless the JD is actually junior-friendly.
- Operational requirements are not collapsed too far. A credential or role title
  does not automatically cover a separate hands-on operating requirement if the
  JD makes that work central to the role.
- Non-searchable gates are in `screening_gates`, not probes.

### 1c. Probe Design

Design 4-6 initial probes. Each probe is a natural-language query string that
will be passed to `search_network_pipeline prepare`, which calls
`expand_search_request` to generate `role_search_filters`.

#### Probe design rules

**Keep probes short and focused.** Each probe should target one angle on the JD.
Do not stuff the entire JD into one probe query.

**Probe queries are the exact natural-language input passed to
`$search-network`.** They must read like an English people-search request, not
JSON, not trait IDs, and not schema labels. Do not pass `targets_traits`,
`must_have`, `nice_to_have`, or snake_case keys to `$search-network`; those are
only harness metadata.

**Do not list more than 3 industry/sector terms in a single probe query.**
When `expand_search_request` sees many sector terms, it generates
`company_semantic_queries` + `sector_types` + `company_sector_strategy` filters
that consume TurboPuffer multi-query permits. More than ~4 company/sector filter
dimensions causes permit overflow (requires 18 permits, max is 16).

Bad — will cause permit overflow:
```
Required location role in sector A, sector B, sector C, sector D, sector E,
and sector F companies with core skill A, core skill B, core skill C, and
specialization D
```

Good — industry terms in the role/semantic description, not as company filters:
```
Required location role with core responsibility A, core responsibility B,
required specialization, and relevant domain experience
```

**For industry-focused probes, put industry in the role description, not as
company qualifiers.** Prefer "people with domain experience" over "people at
companies in domain A/domain B/domain C." The former is more likely to generate
role/semantic filters; the latter often generates permit-heavy company filters.

**Each probe should have a strategy type:**

| Strategy | When to use | Filter shape |
|----------|------------|--------------|
| `role_focused` | Core title/function search with location | `bm25_queries` + `semantic_query` + `cities` + `seniority_bands` |
| `credential_focused` | Specific credential or qualification | `bm25_queries` + `semantic_query` with credential terms + `cities` |
| `career_path` | People who transitioned from X to Y | `semantic_query` describing the transition + `bm25_queries` for target role |
| `industry_semantic` | Industry/domain experience as semantic match | `semantic_query` with industry terms + `bm25_queries` for role. NO `company_semantic_queries` or `sector_types` |

**Link probes to traits.** Each probe should note which trait IDs it is designed
to surface candidates for. Use the exact English `trait` strings, not internal
slugs, in user-facing plan previews. This enables coverage gap analysis in task
3 without leaking implementation labels.

Do not target `screening_gates` in probes unless the gate is profile-evaluable
and already-held. Querying for eligibility or willingness usually produces
noise.

#### Probe fields

```json
{
  "id": "p1_core_role",
  "query": "Required location role with required credential, core responsibility, and core domain skill",
  "strategy": "role_focused",
  "limit": 20,
  "targets_traits": [
    "Required role/function track",
    "Required domain skill"
  ]
}
```

### 1d. Plan Preview

Write `plan.json` to the run directory and make it conform to
`packs/search/schemas/search-network-jd-plan.schema.json`.

Important schema semantics:

- `set_scope` is execution metadata only. Do not repeat set names inside probe
  query text.
- `search_scope` captures location or sourcing scope. It is not a job-fit trait
  and must not appear in `traits` or `targets_traits`.
- `traits.must_have` and `traits.nice_to_have` contain English,
  profile-evaluable job-fit traits.
- `baseline_implied` contains broad or vague requirements that credible
  seniority/work history already implies.
- `screening_gates` contains company-side close or application constraints.
- `initial_probes[].query` is the exact English query string to pass to
  `$search-network`.
- `initial_probes[].targets_traits` must reference actual English trait names
  from `traits.must_have` or `traits.nice_to_have`.

Show the plan compactly and ask exactly:

`Execute this search plan or modify it?`

---

## Task 2 — Run Retrieval Probes

### Execution

Each probe is a self-contained people search. Delegate each probe to the
`$search-network` skill by passing the probe's `query` string as the search
query. `$search-network` owns the full pipeline: expand -> resolve -> prefilter
-> retrieve -> hydrate -> LLM filter -> LLM rerank -> persist. It also handles
company-directory fast paths, preview, and execution gating.

Do not call `search_network_pipeline.py` directly from this skill. Let
`$search-network` handle it — it has a ton of logic and expertise for single
query execution.

The delegated input must be only the probe's English `query` value, for example:

```
Required location role with required credential, core responsibility, and core domain skill
```

Do not send a JSON object or internal labels to the subagent or
`$search-network`.

For each probe:
1. Run `$search-network` with the probe's `query` string
2. Skip the user approval gate — the JD plan approval covers all probes
3. Collect the probe's result: artifact_dir, csv path, found_count

Prefer sub-agents (one per probe) when the harness supports workers. Otherwise
run sequentially.

### TurboPuffer Permit Overflow Handling

If a probe fails with `multi-query exceeds per-namespace concurrency budget`
(requires N permits, max is 16):

1. **Do not retry the same query.** The `expand_search_request` output has too
   many company/sector filter dimensions.

2. **Rewrite as a role-only semantic probe.** Strip all industry/sector/company
   qualifier terms from the query. Move them into the role description so they
   land in `semantic_query` instead of `company_semantic_queries`/`sector_types`.

   Failed query:
   ```
   Required location target role at sector A, sector B, sector C, sector D,
   and sector E companies with core skill A and core skill B
   ```

   Role-only fallback:
   ```
   Required location target role with core skill A, core skill B, and relevant
   domain experience
   ```

3. Record the fallback in `probe_summaries.json` with:
   - original probe id + `_role_only` suffix
   - `fallback_reason: "turbopuffer_permit_overflow"`
   - the rewritten query

### Probe Result Collection

For each completed probe, record:

| Field | Value |
|-------|-------|
| `id` | probe id |
| `status` | completed / failed |
| `query` | the query string used |
| `artifact_dir` | path to pipeline artifacts |
| `csv` | path to the probe's result CSV |
| `state` | path to task state JSON |
| `found_count` | number of rows in CSV |
| `fallback_reason` | null or reason for fallback |

Write `probe_summaries.json` to the run directory.

Write `lineage.json` to the run directory, appending events:
- `source_fetched` — after intake
- `plan_created` — after plan.json written
- `initial_probes_completed` — after all probes finish, with per-probe summaries

---

## Task 3 — Expand on Coverage Gaps

After initial probes complete, assess whether the pool is sufficient.

### Coverage Assessment

Dedupe candidates across all probe CSVs by person_id / LinkedIn URL /
public_identifier. Then check:

1. **Total pool size** — if fewer than ~8 unique usable candidates (score >= 0.3),
   expansion is needed.

2. **Trait coverage** — for each trait (must_have and nice_to_have), estimate
   how many candidates have evidence. If a trait cluster is empty or has <2
   candidates, design a focused expansion probe for it. Pay special attention
   to specialization traits — these are expected to be sparse.

3. **Seniority distribution** — if all candidates are too senior or too junior
   for the role, design a probe that targets the right seniority band.

4. **Search-scope coverage** — if the sourcing run is intentionally local and
   few candidates are in the target metro, design a probe with broader metro
   terms. Do not treat commute or onsite willingness as job-fit evidence.

### Expansion Rules

- Expansion means **new search probes**, not re-sorting the existing CSV.
- Each expansion probe follows the same design rules as initial probes (short,
  focused, no sector filter overload).
- Typical expansions: 2-4 additional probes.
- Ask the user before running expansion probes unless they pre-approved fan-out.
- After expansion, re-dedupe and report deltas:

```
Expansion: +N new unique candidates, +M scoring >= 0.30, +K scoring >= 0.50
```

### Trait Mutations

If the user asks to ignore, soften, or change a trait:

1. Record the mutation in lineage: `{ type: "trait_mutation", trait_id,
   old_type, new_type, reason }`
2. Update the working plan — move the trait between must_have/nice_to_have or
   remove it
3. If needed, run new probes with the updated criteria and re-assess coverage
4. Do not treat the previous shortlist as final — rerank or expand with the
   updated traits

### Lineage Events

Append to `lineage.json`:
- `expansion_plan_created` — with new probe IDs and target
- `expansion_probes_completed` — with delta counts
- `trait_mutation` — if the user changed a trait

---

## Execution Rules

- Do not run doctor or setup checks before a search unless the primitive
  fails with an unclear auth/env/setup error.
- Do not write new retrieval scripts during a search run.
- Do not inspect repo docs, source, memory, or prior result files on the
  happy path. The task spec and this skill file are the reference.
- Do not mention skip-rerank, alternate execution modes, internal ledgers, or
  internal artifact paths in user-facing output.
- Each probe's `execute_command` already includes `--execute-approved`; do not
  ask for another approval per probe.

## Handoff to Tasks 4-5

After tasks 1-3 complete, the run directory contains:

- `source.txt`, `source.json` — JD source
- `plan.json` — traits, probes, scoring policy
- `probe_summaries.json` — per-probe results with CSV paths
- `lineage.json` — event log
- `probes/` — per-probe artifacts from `search_network_pipeline`

Task 4 is a deterministic merge primitive. It consumes probe run CSVs and emits:

- `candidate_frontier.json`
- `candidate_frontier.jsonl`
- `candidates.debug.csv`
- `merge_summary.json`

The candidate frontier should conform to:
`packs/search/schemas/search-network-jd-candidate-frontier.schema.json`.

Task 5 is harness evaluation plus primitive capture/export:

1. The harness evaluates `candidate_frontier.json` batches against the full
   `plan.json`, preferably with subagents when available.
2. The harness writes `candidate_evaluations.raw.jsonl` with candidate ids,
   scores, requirement evidence, duplicate-signal interpretation, rationale, and
   caveats.
3. The `capture_jd_candidate_evaluations` primitive validates and persists:
   - `candidate_evaluations.json`
   - `candidate_evaluations.jsonl`
   - `candidates.reranked.csv`
   - `candidates.reranked.debug.json`
4. The `export_candidate_shortlist` primitive formats the sendable
   `shortlist.csv` from captured evaluations plus source attribution.

The evaluation artifact should conform to:
`packs/search/schemas/search-network-jd-candidate-evaluations.schema.json`.

This keeps large candidate/profile context in files and lets the main harness
work with ids, ranks, scores, and concise reasoning instead of loading the full
candidate pool into chat memory.
