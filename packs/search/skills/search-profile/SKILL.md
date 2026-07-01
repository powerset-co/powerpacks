---
name: search-profile
description: DEPRECATED alias of $recruit. Run the recruiter profile-search loop for Powerpacks when the user provides a job posting URL, pasted job description, broad multi-trait role brief, or a LinkedIn profile URL to find more people like. Fetch/read job URLs or resolve the person's profile (local cache, local index, Postgres, then RapidAPI with approval), extract differentiating traits, design recruiter-style candidate profiles, execute one capped search per profile, merge and dedupe the pool, then run the automated evaluation primitive with seniority hard-gating and export a shortlist.
---

<!--
Changelog:
- 2026-06-30: DEPRECATED. Superseded by $recruit (search consolidation Stage 1). $recruit does the
  same JD→judged-shortlist job with a core-tagged plan, mixture-of-judges, a core-gate, and
  IC-track-aware seniority, and now accepts job-posting URLs too (recruit_loop.py --jd-url). This
  skill still works for back-compat; new work should use packs/search/skills/recruit/SKILL.md.
-->

> ⚠️ **Deprecated — use `$recruit`.** `$recruit` supersedes `$search-profile`: same job (JD /
> job-posting URL / role brief → judged shortlist), strictly more evolved (core-tagged plan,
> mixture-of-judges, core-gate, IC-track-aware seniority). Route new work to
> `packs/search/skills/recruit/SKILL.md`. This file remains for back-compat only.

# Search Profile

Use this for job posting URLs, pasted job descriptions, or broad role briefs
where one search would likely be too noisy or miss distinct candidate
patterns — and for **similar-person requests** ("find me more people like
<linkedin url>"), which run the same loop seeded from a person's profile
instead of a JD.

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

## Similar-Person Mode ("find me more people like <linkedin url>")

When the input is a LinkedIn profile URL (or the user names a person plus
their LinkedIn), seed the loop from that person's profile instead of a JD.
Everything after Task 1a is the same pipeline.

### S1. Resolve the person's profile

Run the profile-resolution primitive. It checks the cheapest sources first
and only hits RapidAPI with explicit approval:

```bash
uv run --env-file .env --project . python packs/search/primitives/fetch_person_profile/fetch_person_profile.py \
  --linkedin-url "<url>"
```

Lookup order: local RapidAPI profile cache → local DuckDB index → Postgres
`hydrated_context` → RapidAPI. The RapidAPI fallback runs automatically — no
approval needed — and seeds the shared profile cache. If the primitive
returns `status: not_found` or `failed`, tell the user the profile could not
be resolved and stop.

Write the result into the run directory as `source.json` /
`seed_profile.json`; the profile summary replaces `source.txt` as the trait
source.

### S2. Trait extraction from a person (not a JD)

Apply the same trait rules as Task 1b with these adjustments:

- Traits describe **what makes this person distinctive and replaceable-in-kind**:
  current role/function track, seniority band, domain/industry context,
  specific named skills or stacks visible in positions, and ownership level.
- Use the person's **current role** as the seniority anchor. "More people
  like X" means peers at X's level doing X's kind of work — not X's juniors,
  not executives above X (unless X is an executive; then peers are too).
- Do not turn employer names into traits ("worked at Stripe" is evidence,
  not a requirement) unless the user asks for company-alike candidates.
  Prefer the *kind* of company (stage, domain) when it is clearly part of
  the pattern.
- Location: use the person's metro as the default search scope; widen on
  user request. It is scope, not a trait.
- Set `usable_cutoff` from the person's current band, same wording rules as
  a JD run, and derive `seniority_bands` from the current role's explicit
  level the same way as Task 1b (one band up within the same track; never
  from years of experience).

### S3. Candidate profile design

Usually **one** candidate profile is enough — the person *is* the archetype.
Design a single capped search (limit 200) describing the person's role,
level, skills, and domain in plain English. Add a second profile only when
the seed person genuinely spans two distinct patterns (e.g. operator +
investor) and say which pattern each search targets.

The plan preview shows the seed person (name, current role/company, the
resolved source tier), the derived traits, the seniority-target line, and
the search query. Exclude the seed person themselves from the final
shortlist (match by person_id / LinkedIn URL at merge or evaluation time).

Then continue with Task 2 (run profile searches), Task 4 (merge), and
Task 5 (evaluate + export) exactly as for a JD run.

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

**Center of gravity comes first.** The first must-have must name the role's
center of gravity — what this person does day-to-day, phrased so a strong
candidate from a neighboring lane cannot fully satisfy it. "Backend
engineering for production APIs" lets a career SRE score strong on every
trait; "builds customer/developer-facing product APIs" does not. Reliability,
scale, and ownership wording without a product anchor is an SRE/platform
magnet — anchor the trait to the work product, not the systems touched.

Adjacent-lane test before finalizing traits: *could a strong candidate from a
neighboring lane (SRE/platform for product-backend, data analyst for data
engineering, consultant for in-house IC) score "strong" on every must-have?*
If yes, the traits are too loose — sharpen the center-of-gravity trait until
the adjacent lane caps at partial. There is no adjacent-lane verdict tier:
adjacent profiles that cannot evidence the core work are `out`, so loose
traits are the only way they leak into the shortlist.

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
- A candidate outside the seniority band can be `top_tier` or
  `high_potential` only when the current role is plausibly analogous after
  company-size context. A CFO, CEO, Founder, President, Partner, Board
  Member, or enterprise VP should be `out` unless the JD explicitly asks for
  that seniority.

Record the seniority policy in `plan.json` as `usable_cutoff` — the automated
evaluation primitive reads it and enforces it as a hard gate.

#### Hire stage (`hire_stage`)

Derive `hire_stage` in `plan.json` — the evaluation primitive uses it to pick
the excellence bar:

- `founding_early` — founding engineer/team, first N hires, seed/Series A
  startup roles, 0→1 language ("wear many hats", "build from scratch",
  "ship daily"). The evaluator weights trajectory steepness, 0→1 ownership,
  breadth, and speed of scope growth.
- `scaling_late` — growth/late-stage or established companies, roles about
  hardening and scaling ("productionize", "scale our platform", "reliability
at scale"). The evaluator weights depth + years of experience with continued
  growth, and evidence of turning MVPs/POCs into battle-tested production
  systems.

Default to `founding_early` when ambiguous and the company is small; say which
stage was chosen in the plan preview's `Targeting:` line context.

#### Seniority bands (hard retrieval filter)

Alongside `usable_cutoff`, derive canonical `seniority_bands` and record them
in `plan.json` (next to `usable_cutoff`). Task 2 pins these bands on every
profile search via `--seniority-bands`, so retrieval itself only returns
in-band candidates instead of paying hydration and evaluation cost on
out-of-band people.

A hiring JD always encodes an intended band — never leave the plan silent
about seniority. Explicit level language wins; when there is none, propose the
title's conventional band range marked as inferred, and let the user correct
it at the plan-preview hard stop.

Derivation rules:

1. **Explicit level language only.** Map level words in the JD title and
   requirements to canonical bands:
   - title modifiers: "Senior" → `senior`; "Staff" → `staff`; "Principal" →
     `principal`; "Lead" → `senior`; "Junior" → `junior`;
     "Intern"/"Internship" → `trainee`; "Entry-level"/"New grad" → `entry`
   - leadership titles: "Manager" → `manager`; "Director" → `director`;
     "VP"/"Vice President" → `vice-president`; "Head of X" → `director` +
     `vice-president`; "Chief X Officer"/"C-level"/"executive" (standalone) →
     `c-suite`; "Partner" → `partner`; "Owner" → `owner`
   - beware titles where "executive" is not a level: Account Executive,
     Executive Assistant, Executive Producer are not c-suite

   Canonical values (hyphenated, must match the index):
   `owner`, `partner`, `c-suite`, `vice-president`, `director`, `principal`,
   `staff`, `manager`, `senior`, `mid`, `junior`, `entry`, `trainee`.
2. **Adjacent-band tolerance: include one band up within the same track.**
   People one band above the JD's level still plausibly apply. Widen each
   derived band with its next band up — IC track: `entry` < `junior` < `mid` <
   `senior` < `staff` < `principal`; management track: `manager` < `director`
   < `vice-president` < `c-suite`. Examples: "Senior Data Engineer" →
   `["senior", "staff"]`; "Director of Engineering" → `["director",
   "vice-president"]`. Top-of-track bands (`principal`, `c-suite`) and
   `partner`/`owner` get no extra band. Explicit ranges override adjacency:
   "senior or above"/"senior+" → `["senior", "staff", "principal"]`;
   "director and above" → `["director", "vice-president", "c-suite"]`.
3. **Never derive bands from years of experience**, team size, scope, or
   impact language. "8+ years" does not mean senior; "owns the roadmap end to
   end" does not mean director. YOE is unreliable — only explicit level words
   (rule 1) or the title's conventional range (rule 4) count.
4. **No explicit level language → infer the title's conventional band range
   and mark it inferred.** Hiring JDs always have an intended band; propose
   the range the title conventionally spans so the user corrects a concrete
   proposal instead of discovering an unfiltered search later:
   - "Member of Technical Staff" → `["mid", "senior"]` (level-less IC title;
     local index confirms most MTS holders are mid/senior — NOT the `staff`
     band despite the word)
   - bare IC titles ("Software Engineer", "Data Scientist", "Designer") →
     `["mid", "senior"]`
   - numbered levels: "Engineer I" → `["entry", "junior"]`; "Engineer II" →
     `["junior", "mid"]`; "Engineer III"/"L4-L5" style → `["mid", "senior"]`
   - "Founding Engineer" → `["senior", "staff"]`
   Record these in `plan.json` like any other bands; the preview's
   `Targeting:` line (Task 1d) plus the hard stop is where the user confirms
   or corrects them. Only a brief with no role title at all (e.g.
   "interesting infra people in my network") leaves `"seniority_bands": []`
   with retrieval unfiltered and the `usable_cutoff` evaluation gate as the
   only enforcement.

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
- The first must-have names the role's center of gravity and passes the
  adjacent-lane test (an SRE, analyst, or consultant cannot score "strong" on
  every must-have).
- `hire_stage` is set (`founding_early` or `scaling_late`).
- No trait is just a soft skill, personality trait, mission phrase, or benefits
  text.
- No credential is invented from adjacent wording.
- Location/onsite/relocation/compensation/availability are not traits.
- Baseline qualifications are not hard gates when credible senior work history
  already implies them.
- The plan explicitly calibrates seniority (`usable_cutoff`) so junior profiles
  are not retrieved for senior roles and executives are gated for IC roles.
- `seniority_bands` contains only canonical band values derived from explicit
  level language plus one-band-up adjacency — never from years of experience —
  and is an empty list when the JD names no level.
- Screening/close concerns are ignored entirely.

### 1c. Candidate Profile Design

Design **2-3 candidate profiles**. Each profile is a distinct archetype of
person who could do this job, expressed as one natural-language query string
that will be passed to `$search`.

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
`$search`.** They must read like an English people-search request, not
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

**Budget every profile.** Each profile carries a `limit` (default 200; use
100 only for quick/cheap previews). The limit is passed to `$search`
and caps the candidates kept after retrieval, which caps the entire
downstream pipeline cost. A too-small limit lets a noisy archetype crowd out
better candidates before the aggregate evaluator ever sees them.

#### Profile fields (stored under `initial_probes` for schema compatibility)

```json
{
  "id": "profile_bigco_equivalent",
  "query": "Senior or staff data engineers in New York or San Francisco at large technology companies who build production ETL pipelines and data platforms with Python and SQL",
  "strategy": "role_focused",
  "limit": 200,
  "targets_traits": [
    "Senior hands-on data engineering experience",
    "ETL pipeline architecture"
  ]
}
```

### 1d. Plan Preview

Write `plan.json` to the run directory conforming to
`packs/search/schemas/search-network-jd-plan.schema.json`, then validate it:

```bash
uv run --project . python packs/search/primitives/validate_artifact/validate_artifact.py \
    --schema search-network-jd-plan --file <run_dir>/plan.json
```

Fix any reported violations before proceeding. This is the only supported
validation path — do not import `jsonschema` in ad-hoc scripts.

Important schema semantics:

- `set_scope` is execution metadata only.
- `search_scope` captures location or sourcing scope; it is not a job-fit trait.
- `usable_cutoff` is the seniority policy the evaluation primitive enforces.
- `seniority_bands` is the canonical retrieval filter derived in Task 1b;
  Task 2 pins it on every profile search. Empty list = no retrieval filter.
- `initial_probes[]` holds the candidate profiles (legacy field name).
- `initial_probes[].query` is the exact English query passed to
  `$search`; `targets_traits` must reference actual English trait
  names.

Show the plan compactly — traits, then each candidate profile with one line of
archetype description and its query — and ask exactly:

`Execute this search plan or modify it?`

**This checkpoint is a hard stop.** It applies even when the user's input
reads as an execution command (a pasted URL, a `$search ... in local`
invocation, "run this JD"). General harness autonomy rules like "don't stop at
a proposal — implement" do NOT apply here: searches spend money and the plan
preview is the user's only chance to correct traits and seniority before
spend. The only bypass is the user explicitly waiving the preview in this
session (e.g. "skip the preview", "don't ask, just run it").

Seniority in the preview: when the plan targets a specific seniority band,
include exactly one compact line, e.g.:

`Targeting: senior/staff hands-on ICs`

This gives the user a chance to correct the band before searching. Do not
recite the full `usable_cutoff` policy or explain the gating mechanics —
enforcement is a given (in-band ICs match; executives/founders/advisors are
gated at evaluation). Ask a question only if the band is genuinely ambiguous
and changes the search (e.g. "director-level OK?").

Title-inferred bands (Task 1b rule 4) show the same way as explicit ones —
just state them (`Targeting: mid/senior ICs`); the plan-preview hard stop is
where the user corrects them. In the rare case there is no role title at all
and `seniority_bands` is empty, say so in one line so the user can pin a band
before spend:

`Targeting: all levels (no role title to infer from) — pin a band?`

**If the user modifies the plan at this checkpoint** (corrects the seniority
band, adds/removes a trait, changes scope): apply the change, re-validate
plan.json, then re-show the changed lines — including the updated
`Targeting: ...` line when seniority changed — and ask the same
`Execute this search plan or modify it?` question again. Never proceed to
Task 2 directly from a plan mutation; the user confirms the corrected plan,
not the original one.

---

## Task 2 — Run Profile Searches

### Execution

Each candidate profile is one self-contained people search. Delegate each to
the `$search` skill by passing:

1. the profile's `query` string as the search query
2. the profile's `limit` (default 200) — `$search` appends
   `--limit <N>` to the pipeline command
3. **filter-only mode** — `$search` appends `--filter-only` so the run
   keeps the cheap conservative LLM filter (reject clear junk, pass anything
   uncertain) but skips the expensive per-search LLM rerank. Final ranking is
   owned by the evaluation primitive in Task 5, which sees the full JD context.
4. **pinned seniority bands** — when `plan.json` has a non-empty
   `seniority_bands`, append `--seniority-bands` with the comma-joined plan
   bands to the pipeline `execute_command`, e.g.:

   ```
   ... search_network_pipeline.py run ... --limit 200 --filter-only --seniority-bands senior,staff
   ```

   The flag works identically on `local_search_pipeline.py run` commands. It
   REPLACES whatever `seniority_bands` query expansion inferred from that one
   profile query — the plan's JD-derived bands are the hard constraint — and
   retrieval then only returns positions whose `seniority_band` is in the
   pinned set. If the plan's `seniority_bands` is empty, omit the flag
   entirely; never invent bands at search time.
5. **pinned current role (always)** — append `--current-role` on every
   profile search. This pins `is_current_role=true` as a hard retrieval
   filter so a person only qualifies on a CURRENT in-band position, not a
   past one. Without it, retrieval qualifies people on old roles — a current
   founder/CEO who was once a senior engineer matches on the stale role and
   leaks in. Query expansion only sets this when the query says "currently",
   which recruiter profile queries never do, so pin it explicitly; never rely
   on phrasing.

   ```
   ... search_network_pipeline.py run ... --limit 200 --filter-only --seniority-bands senior,staff --current-role
   ```

**Current-role is enforced with double redundancy.** `--current-role` is the
first layer (retrieval drops stale in-band positions); the JD evaluator's
seniority gate (Task 5) is the second (anyone whose CURRENT primary identity
is founder/CEO/exec/advisor is marked `too_senior` → `out`, even if a
concatenated current title also shows a senior-engineer role). Keep both on;
neither alone is sufficient — retrieval can still surface multi-current-role
profiles, and the eval gate alone would pay hydration/eval cost on stale
matches.

Do not call `search_network_pipeline.py` directly from this skill except to
append the `--limit`, `--filter-only`, `--seniority-bands`, and
`--current-role` flags to the `execute_command` that `$search`
produced.

The delegated input must be only the profile's English `query` value. Do not
send a JSON object or internal labels.

For each profile search:
1. Run `$search` with the profile's `query`, limit, filter-only mode,
   `--current-role`, and the plan's pinned seniority bands (when non-empty)
2. Skip the user approval gate — the plan approval covers all profile searches
3. Capture the `state` path from the pipeline's JSON output — it is the only
   thing Result Collection needs per search

Prefer sub-agents (one per profile) when the harness supports workers.
Otherwise run sequentially.

### TurboPuffer Permit Overflow Handling

If a profile search fails with `multi-query exceeds per-namespace concurrency
budget`:

1. **Do not retry the same query.** Too many company/sector filter dimensions.
2. **Rewrite as a role-only semantic query.** Strip industry/sector/company
   qualifier terms; move them into the role description so they land in
   `semantic_query` instead of company filters.
3. In Result Collection, pass the fallback run's state with
   `--probe-id <original_id>_role_only` and
   `--fallback-reason <original_id>_role_only=turbopuffer_permit_overflow`.

### Result Collection

After the profile searches finish, GENERATE `probe_summaries.json` (legacy
filename) by running the collector — do not hand-write the file:

```bash
uv run --project . python packs/search/primitives/merge_candidate_frontier/merge_candidate_frontier.py \
    collect-probes \
    --run-dir <run_dir> \
    --probe-id <profile_id_1> --state <state_json_1> \
    --probe-id <profile_id_2> --state <state_json_2>
```

- One `--probe-id`/`--state` pair per profile search, in plan order. The Nth
  `--probe-id` labels the Nth `--state`; use the plan's profile ids. Each
  `--state` is the task state JSON path the pipeline printed
  (`.powerpacks/runs/<task_id>-<slug>.json`).
- Add `--fallback-reason <probe_id>=<reason>` for permit-overflow fallback
  runs (see above).
- The command reads each state's `query` and persisted `artifacts`
  (artifact_dir, csv, row_count) and overwrites
  `<run_dir>/probe_summaries.json` deterministically; re-running with the same
  states produces the same file.

The canonical shape is a **bare JSON list** of probe objects
(`packs/search/schemas/probe-summaries.schema.json`). Hand-authoring or
wrapping the list in an object (`{"probes": [...]}`) is forbidden; downstream
primitives only tolerate the legacy wrapper for old runs.

Field reference (all produced by the command):

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

## Task 3 — Deep-Dive on the Best Lane (standard step)

After the initial shallow lanes are merged (Task 4) and evaluated once
(Task 5a), **always run one deep search on the single best-performing lane**.
This is not optional or conditional — it is the default flow. The shallow
lanes (limit 200, filter-only) only find the *shape* of the pool; the deep
dive is where you actually go get the candidates.

### Pick the lane (highest precision)

From the **first** evaluation pass, compute each lane's precision:

```
lane_precision = (lane candidates that are top_tier or high_potential)
                 / (lane's total evaluated candidates)
```

Use each candidate's `matched_probe_ids` to attribute them to lanes (a
candidate in multiple lanes counts for each). Pick the lane with the
**highest precision**; tie-break on the larger absolute count of good
candidates. Dead lanes (0 rows) are skipped. Report the per-lane precision in
one line so the choice is visible.

### Run that one lane deep

Run the winning lane's `query` through `$search` with a large limit
and the full reranker:

```
... search_network_pipeline.py run ... --limit 1000 --seniority-bands senior,staff --current-role
```

- **`--limit 1000`** (up to 2000) — go deep; this is the point of the step.
- **WITHOUT `--filter-only`** — the full `$search` pipeline
  (retrieval → hydrate → filter → **LLM rerank**) scores the deep pool for
  you, instead of dumping 1000+ raw candidates into the JD evaluator.
- Keep the plan's pinned `--seniority-bands` and `--current-role`. The deeper
  the pull, the more stale-role matches leak in, so `--current-role` matters
  most here (a limit-1000 run without it pulled in ~4× the current
  founders/execs).

Then merge the deep run's results into the frontier and **re-run the JD
evaluator (Task 5)** over the combined pool, then export from that final
evaluation. Only one lane goes deep — never run 1000-2000 on every lane.

Why this shape: a deep `--filter-only` run would push 1000-2000 candidates
straight into the JD evaluator (expensive, and the cheap filter barely
narrows). Letting `$search` rerank the deep pool first is the
cost-controlled way to go deep on the lane that already proved productive.

### Coverage / extra expansion (only if still thin)

If after the deep dive the pool is still thin (fewer than ~8 unique usable
candidates) or a must-have trait cluster has <2 plausible candidates, assess
whether the pool is sufficient before stopping.

### Extra expansion (rare)

If the deep dive still leaves gaps, expansion means **new profile searches**,
not re-sorting the pool:

- 1-2 expansion profiles on a genuinely different axis (e.g. a must-have
  trait cluster with <2 plausible candidates), same design and budget rules.
- If >50% of the pool matched most lanes, the lanes were not distinct; design
  any expansion on a different axis.
- After expansion, re-dedupe, re-evaluate, and report deltas.

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

It reads `probe_summaries.json` (the bare list written by `collect-probes`;
the legacy object wrapper from old runs is tolerated) and `plan.json`, dedupes
by `person_id` and normalized `linkedin_url`, and writes:

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

The evaluator runs **twice** in the standard flow: once on the shallow merged
pool to pick the deep-dive lane (Task 3), then again on the combined pool
after the deep dive. Both passes use the same command below; the export
(5b/5c) runs only on the **final** pass.

1. Merge shallow lanes (Task 4) → run 5a → use the per-lane precision to pick
   and run the deep-dive lane (Task 3).
2. Merge the deep run into the frontier (Task 4 again) → run 5a again → 5b →
   5c on this final pool.

### 5a. Automated Evaluation (primitive)

Run the evaluation primitive — do not hand-score candidates in chat:

```bash
uv run --env-file .env --project . python packs/search/primitives/evaluate_profile_candidates/evaluate_profile_candidates.py \
    --run-dir <run_dir>
```

The primitive:

- evaluates the full merged frontier by default (`--max-candidates 0`); set a
  cap only for very large frontiers
- loads hydrated profiles from the profile-search artifacts
- runs one async LLM evaluation per candidate against `plan.json` traits,
  `hire_stage`, and the `usable_cutoff` seniority policy
- the model returns judgments only — per-trait evidence levels, excellence
  subscores (trajectory / pedigree / impact), seniority fit, and
  material-flagged caveats; the **final score and verdict are computed
  deterministically in code** from those judgments
- per-trait evidence ladder (the model picks the bucket, code maps the value):
  `doing_now` 0.95 (doing this exact work now) · `experienced` 0.80 (clear
  prior direct experience) · `capable` 0.70 (enough to do it / pick it up
  quickly) · `foundational` 0.50 (adjacent background, could slot in) · `thin`
  0.25 · `missing` 0.0
- must-haves aggregate by **quorum/consensus**, not linear average: the
  candidate's weakest ~30% of must-haves are discounted, so a strong
  candidate with a gap or two is not punished proportionally (3/3 → 1.0,
  2/3 → 0.87, 1/3 → 0.43). Nice-to-haves are a small additive bonus (upside
  only, never a gate).
- verdict ladder (bar-raiser; default is `out`):
  - `top_tier` — the team would be lucky to get them: in-band, no missing
    must-have, trait coverage ≥ 0.80 (≈ "experienced" across must-haves),
    excellence ≥ 0.70
  - `high_potential` — can do the work / pick it up quickly: in-band,
    must-coverage ≥ 0.60, OR the diamond escape (must-coverage ≥ 0.45 rescued
    by trajectory ≥ 0.75)
  - `out` — everyone else; there is no "maybe" or adjacent-lane tier
- enforces seniority as a hard gate **in code**: `too_senior` / `too_junior` /
  `wrong_track` force verdict `out` and cap the score at 0.3, regardless of
  trait scores
- material caveats reduce the computed score (0.05 each, capped at 0.20)
- writes `candidate_evaluations.raw.jsonl` in the Task 5a schema, including
  `excellence` and `score_breakdown` blocks for auditability

This is where precision comes from. The per-search filter only rejected
obvious junk; this pass sees the full JD context and the seniority policy.
Because scoring is deterministic, **trait quality (Task 1b) is the single
point of failure** — tune the plan's traits, not the evaluator.

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

Optional: `--min-verdict high_potential` (default) or `--min-verdict
top_tier` for a tighter list; `--out-dir <dir>` to export somewhere
user-visible. Never use `--min-verdict out` for sendable shortlists.

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
- Keep per-profile limits at 200 (use 100 only for quick/cheap previews)
  unless the user explicitly asks for a different pool size.

## Cost model (why these defaults)

Two-phase by design: cheap-and-broad to find the productive lane, then deep on
just that one.

- **Shallow phase:** each lane runs `--filter-only` at `--limit 200` — the
  cheap conservative filter only, no per-lane rerank. Enough depth that a
  noisy lane cannot crowd out better candidates, without paying rerank on
  every lane. First JD evaluation runs on this merged pool.
- **Deep phase:** exactly **one** lane (highest precision) runs at
  `--limit 1000` WITHOUT `--filter-only`, so `$search`'s own rerank
  scores the deep pool before it reaches the JD evaluator. One deep lane, not
  all of them.
- The JD evaluator runs twice (shallow pool, then combined pool) and sees the
  full frontier by default. Net: a few hundred cheap filter calls + one deep
  reranked lane + two bounded evaluation passes — not tens of thousands of
  uncapped calls.

## End-to-end artifact chain

```
<run_dir>/
├── source.txt, source.json           ← Task 1
├── plan.json                          ← Task 1 (profiles under initial_probes)
├── probe_summaries.json               ← Task 2 (collect-probes; bare list, legacy filename)
├── lineage.json                       ← Tasks 1-5
├── candidate_frontier.json/.jsonl     ← Task 4
├── candidates.debug.csv               ← Task 4
├── merge_summary.json                 ← Task 4
├── candidate_evaluations.raw.jsonl    ← Task 5a (evaluation primitive)
├── candidate_evaluations.json/.jsonl  ← Task 5b
├── candidates.reranked.csv            ← Task 5b
└── shortlist.csv + manifest           ← Task 5c
```
