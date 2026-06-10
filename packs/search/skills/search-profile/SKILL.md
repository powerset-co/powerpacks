---
name: search-profile
description: Run the recruiter profile-search loop for Powerpacks when the user provides a job posting URL, pasted job description, or broad multi-trait role brief. Fetch/read job URLs, extract differentiating traits, design 2-3 recruiter-style candidate profiles, execute one capped search per profile, merge and dedupe the pool, then run the automated evaluation primitive with seniority hard-gating and export a shortlist.
---

# Search Profile

Use this for job posting URLs, pasted job descriptions, or broad role briefs
where one search would likely be too noisy or miss distinct candidate patterns.

## The recruiter mindset

A recruiter sourcing for a role does not run six overlapping keyword searches.
They picture **2-3 distinct candidate profiles** — concrete archetypes of
people who could do this job — and source each one separately.

Derive the archetypes from the JD itself: what distinct kinds of people would
plausibly succeed in this role? Useful separation axes include company
context, career path, ownership level, and domain — but do not force any
fixed bucket set (e.g. always big-co vs startup). The right decomposition
depends on the role. A JD that emphasizes end-to-end ownership suggests an
ownership archetype; a JD with a central industry vertical suggests a domain
archetype; a generic role may only need one core profile plus one genuinely
different angle.

Each profile is one capped, cheap search. Quality comes from the final
evaluation pass, not from running more searches. Start with 2-3 profiles;
expand only when coverage gaps prove it is needed or the user asks.

Tasks 1-3 (intake through expansion) are harness-driven. Tasks 4-5 use the
merge, evaluate, capture, and export primitives.

Note on artifact naming: on-disk artifacts keep legacy field names
(`initial_probes`, `probe_summaries.json`, `matched_probe_ids`) for primitive
compatibility. In user-facing output always say "profile" / "profile search",
never "probe".

Reference task spec: `packs/search/tasks/search-network-jd.task.json`
Plan schema contract:
`packs/search/schemas/search-network-jd-plan.schema.json`

---

## Task 1 — Prepare Profile Plan

### 1a. Source Intake

If the input is a URL, fetch the page and persist artifacts. If the input is
pasted text, persist it directly. Do not infer search criteria from a URL string
alone.

Create a run directory:

```
.powerpacks/search-profile/<slug>-<timestamp>/
```

Write these source artifacts:

| File | Content |
|------|---------|
| `source.txt` | Clean text extracted from the page or pasted content |
| `source.json` | `{ source_url, source_title, fetched_at }` |
| `source.html` | Raw HTML if fetched from URL (optional, for debug) |

Use this route when the content has several of:

- title, responsibilities, qualifications, department, location
- hard filters plus soft/preferred filters
- OR-style experience families
- many nice-to-have skills
- archetype language (founder-capable, technical cofounder, etc.)

### 1b. Trait Extraction

Most JD text is fluff — generic qualifications like "strong communication
skills" that any qualified candidate would have. The real work is identifying
the 4-6 traits that actually differentiate candidates.

A **trait** is a concrete, profile-evaluable qualification that covers a cluster
of related JD requirements. Traits are what candidate profiles target and what
the final evaluation scores against.

Traits should answer: **can this person do the job well?** Do not use traits to
model whether the company can close the candidate or whether the candidate will
accept the employment terms.

Before keeping any trait, apply this test:

1. Could this plausibly appear in a LinkedIn/profile/work-history record?
2. Would it materially change who we retrieve or how we rank?
3. Is it more specific than generic competence?

If the answer is no, do not make it a trait.

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
  to ignore or soften it, record a trait mutation and re-evaluate/expand.

#### Search scope, not fit

Location can be a search scope when the user wants local candidates, but it is
not a job-fitness trait by itself. Do not add "onsite candidate" as a trait, do
not put it in `targets_traits`, and do not score someone as unable to do the
job because their profile does not prove commute willingness.

#### Basic requirements and seniority calibration

Do not promote every Basic Qualification into a trait. For mid-level, senior,
staff, lead, manager, or executive roles, many baseline requirements are implied
by credible work history in the target function.

Vague requirements are not gates. If the JD says something underspecified like
"data analysis tools" or "engineering fundamentals" without naming a tool,
method, standard, or unusual depth, treat it as a baseline assumption when the
candidate has credible role-level experience.

Instead, calibrate the role level:

- Prefer seniority/ownership traits that exclude clearly junior candidates.
- Treat the JD seniority band as a strict hiring constraint. We are matching
  analogous hires, not selling to advisors, cofounders, fractional executives,
  or overqualified network contacts.
- A candidate outside the seniority band can be `strong` or `maybe` only when
  the current role is plausibly analogous after company-size context. A CFO,
  CEO, Founder, President, Partner, Board Member, or enterprise VP should be
  `out` unless the JD explicitly asks for that seniority.

Record the seniority policy in `plan.json` as `usable_cutoff` — the automated
evaluation primitive reads it and enforces it as a hard gate.

#### What to ignore

Do not create traits for:
- Generic soft skills, communication, analytical ability, organization
- Broad degree requirements implied by a stronger profile-visible trait
- Vague tool/fundamentals language without a specific named tool or method
- Company mission, urgency, accountability, transparency language
- Compensation, benefits, EEO, application boilerplate
- Screening/close concerns: clearance eligibility, relocation willingness,
  work authorization, commute, extended hours, compensation questions

#### Trait fields

Human-facing trait names must be plain English. Do not show snake_case labels.

```json
{
  "trait": "Required credential or license named in the JD",
  "type": "must_have",
  "covers": ["credential requirement", "closely related baseline requirements"],
  "specialization": false,
  "key": "required_credential"
}
```

#### Trait extraction quality checks

Before writing `plan.json`, check:

- 4-6 total traits across `must_have` and `nice_to_have`.
- No trait is just a soft skill, personality trait, mission phrase, or benefits
  text.
- No credential is invented from adjacent wording.
- Location/onsite/relocation/compensation/availability are not traits.
- Baseline qualifications are not hard gates when credible senior work history
  already implies them.
- The plan explicitly calibrates seniority (`usable_cutoff`) so junior profiles
  are not retrieved for senior roles and executives are gated for IC roles.
- Screening/close concerns are ignored entirely.

### 1c. Candidate Profile Design

Design **2-3 candidate profiles**. Each profile is a distinct archetype of
person who could do this job, expressed as one natural-language query string
that will be passed to `$search-network`.

#### Profile design rules

**Think like a recruiter, not a keyword permuter.** Each profile must describe
a *different kind of person*, not a different keyword slice of the same person.
Ask: "would a recruiter open a separate sourcing lane for this?" If two
profiles would mostly return the same people, merge them.

Possible separation axes (pick what the JD itself supports, do not force a
template):
- **Ownership level**: people doing the role inside an established team vs
  people who owned the function end-to-end
- **Company context**: only when the JD genuinely implies it changes the
  candidate pool
- **Career path**: people currently in the exact role vs people one adjacent
  step away with direct evidence
- **Domain**: only when the JD makes the industry/domain genuinely central

Bad separation (do not do this): same role with different tool synonyms, same
role with the trait list reshuffled, one profile per must-have trait.

**Diversity check before executing.** Profiles should differ in their primary
title anchors or their company-context framing. If every profile query starts
with the same role phrase and the same cities, they will retrieve the same
people and waste the whole fan-out. At most 2 profiles may share a primary
title anchor, and only when their company-context framing clearly differs.

**Keep profile queries short and focused.** One archetype per query. Do not
stuff the entire JD into one query.

**Profile queries are the exact natural-language input passed to
`$search-network`.** They must read like an English people-search request, not
JSON, not trait IDs, and not schema labels.

**Do not list more than 3 industry/sector terms in a single profile query.**
Many sector terms cause TurboPuffer permit overflow (company filter
dimensions exceed the multi-query budget). Put industry context in the role
description ("people with healthcare data experience") rather than as company
qualifiers ("people at healthcare/biotech/medtech companies").

**Each profile has a strategy type:**

| Strategy | When to use |
|----------|------------|
| `role_focused` | Core title/function search with location |
| `credential_focused` | Specific credential or qualification |
| `career_path` | People who transitioned from X to Y, or ownership archetypes |
| `industry_semantic` | Industry/domain experience as semantic match. No company sector filters |

**Link profiles to traits.** Each profile notes which traits (exact English
strings) it is designed to surface candidates for. This enables coverage gap
analysis in Task 3.

**Budget every profile.** Each profile carries a `limit` (default 100,
maximum 150). The limit is passed to `$search-network` and caps the candidates
kept after retrieval, which caps the entire downstream pipeline cost.

#### Profile fields (stored under `initial_probes` for schema compatibility)

```json
{
  "id": "profile_bigco_equivalent",
  "query": "Senior or staff data engineers in New York or San Francisco at large technology companies who build production ETL pipelines and data platforms with Python and SQL",
  "strategy": "role_focused",
  "limit": 100,
  "targets_traits": [
    "Senior hands-on data engineering experience",
    "ETL pipeline architecture"
  ]
}
```

### 1d. Plan Preview

Write `plan.json` to the run directory conforming to
`packs/search/schemas/search-network-jd-plan.schema.json`.

Important schema semantics:

- `set_scope` is execution metadata only.
- `search_scope` captures location or sourcing scope; it is not a job-fit trait.
- `usable_cutoff` is the seniority policy the evaluation primitive enforces.
- `initial_probes[]` holds the candidate profiles (legacy field name).
- `initial_probes[].query` is the exact English query passed to
  `$search-network`; `targets_traits` must reference actual English trait
  names.

Show the plan compactly — traits, then each candidate profile with one line of
archetype description and its query — and ask exactly:

`Execute this search plan or modify it?`

Do not echo the seniority policy / `usable_cutoff` in the plan preview. It is
a given: in-band ICs match, executives/founders/advisors are gated at
evaluation. Mention seniority only if the user asks, or if the JD's band is
genuinely ambiguous and you need a decision (e.g. "director-level OK?").

---

## Task 2 — Run Profile Searches

### Execution

Each candidate profile is one self-contained people search. Delegate each to
the `$search-network` skill by passing:

1. the profile's `query` string as the search query
2. the profile's `limit` (default 100) — `$search-network` appends
   `--limit <N>` to the pipeline command
3. **filter-only mode** — `$search-network` appends `--filter-only` so the run
   keeps the cheap conservative LLM filter (reject clear junk, pass anything
   uncertain) but skips the expensive per-search LLM rerank. Final ranking is
   owned by the evaluation primitive in Task 5, which sees the full JD context.

Do not call `search_network_pipeline.py` directly from this skill except to
append the `--limit` and `--filter-only` flags to the `execute_command` that
`$search-network` produced.

The delegated input must be only the profile's English `query` value. Do not
send a JSON object or internal labels.

For each profile search:
1. Run `$search-network` with the profile's `query`, limit, and filter-only mode
2. Skip the user approval gate — the plan approval covers all profile searches
3. Collect the result: artifact_dir, csv path, found_count

Prefer sub-agents (one per profile) when the harness supports workers.
Otherwise run sequentially.

### TurboPuffer Permit Overflow Handling

If a profile search fails with `multi-query exceeds per-namespace concurrency
budget`:

1. **Do not retry the same query.** Too many company/sector filter dimensions.
2. **Rewrite as a role-only semantic query.** Strip industry/sector/company
   qualifier terms; move them into the role description so they land in
   `semantic_query` instead of company filters.
3. Record the fallback in `probe_summaries.json` with the original id +
   `_role_only` suffix and `fallback_reason: "turbopuffer_permit_overflow"`.

### Result Collection

For each completed profile search, record in `probe_summaries.json`
(legacy filename):

| Field | Value |
|-------|-------|
| `id` | profile id |
| `status` | completed / failed |
| `query` | the query string used |
| `artifact_dir` | path to pipeline artifacts |
| `csv` | path to the result CSV |
| `state` | path to task state JSON |
| `found_count` | number of rows in CSV |
| `fallback_reason` | null or reason for fallback |

Write `lineage.json` events: `source_fetched`, `plan_created`,
`initial_probes_completed`.

---

## Task 3 — Expand on Coverage Gaps

After the initial profile searches complete, assess whether the pool is
sufficient. Default to **not expanding**: 2-3 well-crafted profiles with
limit 100 give a 200-300 candidate pool, which is normally enough for a
shortlist. Returning candidates for user review beats running more searches.

### Coverage Assessment

Dedupe candidates across all profile CSVs by person_id / LinkedIn URL. Then
check:

1. **Total pool size** — if fewer than ~8 unique usable candidates,
   expansion is needed.
2. **Trait coverage** — if a must-have trait cluster has <2 candidates with
   plausible evidence, design one focused expansion profile for it.
3. **Seniority distribution** — if the pool skews too senior or too junior,
   design a profile targeting the right band.
4. **Profile overlap** — compute the share of candidates that appeared in
   multiple profile searches. If >50% of the pool matched most profiles, the
   profiles were not distinct; note this in the lineage and design any
   expansion profile on a genuinely different axis.

### Expansion Rules

- Expansion means **new profile searches**, not re-sorting the existing pool.
- 1-2 expansion profiles, same design and budget rules.
- Ask the user before running expansion profiles unless they pre-approved
  fan-out.
- After expansion, re-dedupe and report deltas.

### Trait Mutations

If the user asks to ignore, soften, or change a trait:

1. Record the mutation in lineage: `{ type: "trait_mutation", trait_id,
   old_type, new_type, reason }`
2. Update the working plan
3. Re-run the evaluation primitive (Task 5) with the updated plan; expansion
   searches only if coverage demands it

---

## Task 4 — Merge Candidate Frontier

Run the merge primitive to dedupe candidates across all profile CSVs:

```bash
uv run --project . python packs/search/primitives/merge_candidate_frontier/merge_candidate_frontier.py \
    --run-dir <run_dir>
```

It reads `probe_summaries.json` and `plan.json`, dedupes by `person_id` and
normalized `linkedin_url`, and writes:

| File | Content |
|------|---------|
| `candidate_frontier.json` | Full frontier document |
| `candidate_frontier.jsonl` | One JSON object per candidate |
| `candidates.debug.csv` | Flat CSV for quick inspection |
| `merge_summary.json` | Counts, overlap stats, per-profile yield |

Report the overlap share from `merge_summary.json` in one line. Log
`frontier_merged` in `lineage.json`.

---

## Task 5 — Evaluate Candidates and Export Shortlist

### 5a. Automated Evaluation (primitive)

Run the evaluation primitive — do not hand-score candidates in chat:

```bash
uv run --env-file .env --project . python packs/search/primitives/evaluate_profile_candidates/evaluate_profile_candidates.py \
    --run-dir <run_dir> \
    --max-candidates 200
```

The primitive:

- selects the top `--max-candidates` frontier candidates by best per-search
  score (default 200; 0 = all)
- loads hydrated profiles from the profile-search artifacts
- runs one async LLM evaluation per candidate against `plan.json` traits with
  the `usable_cutoff` seniority policy
- enforces seniority as a hard gate **in code**: `too_senior` / `too_junior` /
  `wrong_track` force verdict `out` and cap `jd_score` at 0.3, regardless of
  trait scores
- writes `candidate_evaluations.raw.jsonl` in the Task 5a schema

This is where precision comes from. The per-search filter only rejected
obvious junk; this pass sees the full JD context and the seniority policy.

Useful flags: `--model`, `--reasoning-effort`, `--concurrency`,
`--max-candidates`.

Fallback: if the primitive is unavailable or fails, the harness may evaluate
candidates directly (read profiles, score traits, apply the seniority gate,
write the same raw JSONL schema), then use `--evaluator-mode
harness_single_agent` or `harness_subagents` in 5b.

### 5b. Capture Evaluations

```bash
uv run --project . python packs/search/primitives/capture_jd_evaluations/capture_jd_evaluations.py \
    --run-dir <run_dir> \
    --evaluator-mode primitive \
    --evaluator-model <model>
```

Writes `candidate_evaluations.json`, `candidate_evaluations.jsonl`,
`candidates.reranked.csv`, `candidates.reranked.debug.json`.

### 5c. Export Shortlist

```bash
uv run --project . python packs/search/primitives/export_candidate_shortlist/export_candidate_shortlist.py \
    --run-dir <run_dir>
```

Optional: `--min-verdict maybe` (default) or `--min-verdict strong` for a
tighter list; `--out-dir <dir>` to export somewhere user-visible. Never use
`--min-verdict weak` for sendable shortlists.

Append lineage events: `evaluations_captured`, `shortlist_exported`.

---

## Execution Rules

- Do not run doctor or setup checks before a search unless a primitive fails
  with an unclear auth/env/setup error.
- Do not write new retrieval scripts during a run.
- Do not inspect repo docs, source, memory, or prior result files on the happy
  path.
- In user-facing output say "profile" / "profile search", never "probe", and do
  not mention internal artifact paths or legacy field names.
- Each profile search's `execute_command` already includes
  `--execute-approved`; do not ask for another approval per search.
- Keep per-profile limits at 100 (max 150) unless the user explicitly asks for
  a bigger pool.

## Cost model (why these defaults)

- Per-search LLM rerank is skipped (`--filter-only`); ranking happens once in
  the evaluation primitive with full JD context.
- `--limit 100` caps retrieval, hydration, filter, and evaluation volume.
- 3 profiles × limit 100 ≈ a few hundred cheap filter calls + ≤200 evaluation
  calls, instead of tens of thousands of uncapped filter/rerank calls.

## End-to-end artifact chain

```
<run_dir>/
├── source.txt, source.json           ← Task 1
├── plan.json                          ← Task 1 (profiles under initial_probes)
├── probe_summaries.json               ← Task 2 (legacy filename)
├── lineage.json                       ← Tasks 1-5
├── candidate_frontier.json/.jsonl     ← Task 4
├── candidates.debug.csv               ← Task 4
├── merge_summary.json                 ← Task 4
├── candidate_evaluations.raw.jsonl    ← Task 5a (evaluation primitive)
├── candidate_evaluations.json/.jsonl  ← Task 5b
├── candidates.reranked.csv            ← Task 5b
└── shortlist.csv + manifest           ← Task 5c
```
