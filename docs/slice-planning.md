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

## Bad Slice Behavior

- generating near-duplicate slices that widen nothing useful
- widening title, geography, and company constraints all at once
- hiding why a slice exists
- reviewing a huge frontier without per-slice yield or overlap

## Review Heuristics

- keep slices explicit and few enough to inspect
- compare slice yield before hydrating broadly
- prefer tightening or widening one dimension at a time
- stop and present when the frontier is already coherent

## V1 Rule

Do not run expensive candidate scoring here.

Do not treat slicing as mandatory. It is one strategy available to
`$search-network`, not the only strategy.
