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

A **trait** is a concrete qualification that covers a cluster of related JD
requirements. If a candidate has the trait, you can assume they satisfy the
underlying requirements. Traits are what probes search for and what candidates
are evaluated against.

Read the full JD and extract:

#### must_have traits

The non-negotiable qualifications. Usually 2-4. A candidate who lacks these
cannot be presented regardless of how strong they are otherwise.

Examples:
- Location: "Los Angeles onsite" (when JD says no remote/hybrid)
- Credential: "CPA" (which implies bachelor's in accounting, US GAAP knowledge,
  progressive accounting experience — all covered by having the credential)
- Track: "Controller / accounting leadership track" (not FP&A-only, not
  engineering)

#### nice_to_have traits

Differentiators that separate good from great. Usually 2-4. Candidates missing
these stay in the pool but rank lower.

Examples:
- Industry context: "manufacturing, energy, or industrial environment"
- Tooling: "ERP systems, QuickBooks"
- Specialization: "CAS / government contract accounting" — a niche domain
  where few profiles will have explicit evidence. Flag these as
  `specialization: true` so expansion can handle sparse coverage.

#### What to ignore

Do not create traits for generic qualifications that any qualified candidate
would have:
- "Strong analytical skills"
- "Excellent communication skills"
- "Ability to manage competing priorities in a fast-paced environment"
- "Comfort operating at both detailed and strategic levels"
- "Bachelor's degree in related field" (when a stronger credential like CPA
  already implies this)

These are assumed if the candidate has the real traits.

#### Trait fields

```json
{
  "id": "cpa",
  "trait": "CPA, active or inactive",
  "type": "must_have",
  "covers": ["bachelor's in accounting/finance", "US GAAP knowledge", "3+ years progressive accounting"],
  "specialization": false
}
```

- `id` — short stable slug
- `trait` — the trait as you'd describe it to a recruiter
- `type` — `must_have` or `nice_to_have`
- `covers` — what JD requirements this trait subsumes (for auditability)
- `specialization` — true if this is a niche domain where few candidates will
  have explicit evidence. When a specialization trait has <3 candidates with
  evidence, the harness may downgrade it to `nice_to_have` with a caveat.

### 1c. Probe Design

Design 4-6 initial probes. Each probe is a natural-language query string that
will be passed to `search_network_pipeline prepare`, which calls
`expand_search_request` to generate `role_search_filters`.

#### Probe design rules

**Keep probes short and focused.** Each probe should target one angle on the JD.
Do not stuff the entire JD into one probe query.

**Do not list more than 3 industry/sector terms in a single probe query.**
When `expand_search_request` sees many sector terms, it generates
`company_semantic_queries` + `sector_types` + `company_sector_strategy` filters
that consume TurboPuffer multi-query permits. More than ~4 company/sector filter
dimensions causes permit overflow (requires 18 permits, max is 16).

Bad — will cause permit overflow:
```
Los Angeles Controller in aerospace, defense, hardware, manufacturing,
energy, industrial, or regulated technology companies with GAAP close,
audit, ERP, internal controls, and cost accounting experience
```

Good — industry terms in the role/semantic description, not as company filters:
```
Los Angeles Controller or Assistant Controller with CAS US GAAP internal
controls audit readiness ERP and accounting team leadership
```

**For industry-focused probes, put industry in the role description, not as
company qualifiers.** Say "accounting leaders with government contracting and
CAS experience" not "accounting leaders at government contracting companies."
The former generates `semantic_query` + `bm25_queries` (cheap). The latter
generates `company_semantic_queries` + `sector_types` (expensive permits).

**Each probe should have a strategy type:**

| Strategy | When to use | Filter shape |
|----------|------------|--------------|
| `role_focused` | Core title/function search with location | `bm25_queries` + `semantic_query` + `cities` + `seniority_bands` |
| `credential_focused` | Specific credential or qualification | `bm25_queries` + `semantic_query` with credential terms + `cities` |
| `career_path` | People who transitioned from X to Y | `semantic_query` describing the transition + `bm25_queries` for target role |
| `industry_semantic` | Industry/domain experience as semantic match | `semantic_query` with industry terms + `bm25_queries` for role. NO `company_semantic_queries` or `sector_types` |

**Link probes to traits.** Each probe should note which trait IDs it is designed
to surface candidates for. This enables coverage gap analysis in task 3.

#### Probe fields

```json
{
  "id": "p1_core_controller_la",
  "query": "Los Angeles Controller or Assistant Controller with CAS US GAAP internal controls audit readiness ERP and accounting team leadership",
  "strategy": "role_focused",
  "limit": 20,
  "targets_traits": ["controller_track", "cpa", "cas"]
}
```

### 1d. Plan Preview

Write `plan.json` to the run directory with these fields:

```json
{
  "route": "complex-JD recruiter loop",
  "normalized_archetype": "string — one-line description of the ideal candidate",
  "source_url": "string | null",
  "source_title": "string | null",
  "set_scope": { "name": "string", "set_id": "string" },

  "traits": {
    "must_have": [
      {
        "id": "la_onsite",
        "trait": "Los Angeles onsite",
        "covers": ["onsite full-time, no remote/hybrid"],
        "specialization": false
      },
      {
        "id": "cpa",
        "trait": "CPA, active or inactive",
        "covers": ["bachelor's in accounting/finance", "US GAAP", "3+ years progressive accounting"],
        "specialization": false
      }
    ],
    "nice_to_have": [
      {
        "id": "manufacturing_env",
        "trait": "manufacturing, energy, or industrial environment",
        "covers": ["experience in advanced manufacturing, energy, industrial, or technology-driven companies"],
        "specialization": false
      },
      {
        "id": "erp",
        "trait": "ERP systems, QuickBooks",
        "covers": ["ERP experience", "QuickBooks experience is a plus"],
        "specialization": false
      }
    ]
  },

  "initial_probes": [
    {
      "id": "p1_core_controller_la",
      "query": "...",
      "strategy": "role_focused",
      "limit": 20,
      "targets_traits": ["cpa", "la_onsite"]
    }
  ],

  "llm_review_budget": 100,
  "usable_cutoff": "score >= 0.3",
  "cluster_plan": ["cluster description 1", "..."],
  "expansion_strategy": "After initial probes, if <8 credible candidates or a trait cluster is empty, run focused expansion probes",

  "created_at": "ISO timestamp"
}
```

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
   Los Angeles Controller in aerospace, defense, hardware, manufacturing,
   energy companies with GAAP audit ERP controls
   ```

   Role-only fallback:
   ```
   Los Angeles controller director of accounting accounting manager GAAP
   audit ERP controls cost accounting aerospace defense manufacturing energy
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

4. **Location coverage** — if the JD requires onsite and few candidates are in
   the target metro, design a probe with broader metro area terms.

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

If the user asks to ignore, soften, or change a trait (e.g. "ignore CAS",
"don't require CPA"):

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

Tasks 4 (finalize_ranked_pool) and 5 (export_sendable_shortlist) consume these
artifacts. The final rerank evaluates each candidate against the extracted
traits — must_have traits gate inclusion, nice_to_have traits differentiate
ranking. Until those primitives exist, the harness can perform merge/dedupe,
trait-based rerank, and CSV export manually.
