# Slice Planning

The V1 Powerpacks search loop should not jump from one natural-language request
to one giant retrieval pass.

The goal is to:

- decompose the request
- generate several bounded retrieval slices
- execute them independently
- merge and dedupe the frontier
- decide what to review next

## Good Slice Dimensions

- title strictness
- geography strictness
- seniority strictness
- currentness
- company strictness

## Slice Knobs

Each slice should declare the knobs it intentionally changed:

- `title_strictness`: exact, close variants, or adjacent
- `geography_strictness`: city only, metro, or regional
- `seniority_strictness`: as expanded, IC only, senior plus, manager plus, or any
- `currentness`: current only, past only, or any
- `company_strictness`: none, resolved company IDs, or company attributes
- `count_first`: whether to count before executing
- `candidate_limit`: max candidate IDs to return from the slice
- `hydrate_limit`: max profiles to hydrate from the slice

## Bad Slice Behavior

- generating near-duplicate slices that widen nothing useful
- widening title, geography, and company constraints all at once
- hiding why a slice exists
- reviewing a huge frontier without per-slice yield or overlap

## Review Heuristics

- keep slices explicit and few enough to inspect
- compare slice yield before hydrating broadly
- prefer tightening or widening one dimension at a time
- use `candidate_limit` and `hydrate_limit` deliberately; do not hydrate every
  candidate returned by a broad slice
- stop and present when the frontier is already coherent

## V1 Rule

Do not run expensive candidate scoring here.

Do not treat slicing as mandatory. It is one strategy available to
`$search-network`, not the only strategy.
