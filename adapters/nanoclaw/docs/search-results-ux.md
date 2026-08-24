# Search Results UX

The search loop should feel like an agentic recruiting workspace, not a one-off
chat answer.

## Artifact Model

Every completed `/search` run should produce a canonical run directory:

- `.powerpacks/search-runs/<run>/result.json`: `search.stage_result.v1` stage result
- `.powerpacks/search-runs/<run>/search_spec.json`: reviewed `search.spec.v1` request and execution metadata
- `.powerpacks/search-runs/<run>/candidates.csv`: spreadsheet-friendly candidate table
- `.powerpacks/search-runs/<run>/candidates.jsonl`: one candidate per line for refinement and automation
- `.powerpacks/search-runs/<run>/manifest.json`: artifact paths, counts, and integrity metadata

The directory name is only the run ID. User-visible query, profile, backend, and
corpus metadata come from `search_spec.json`; clients must not derive a query
from the directory name. A missing or malformed spec makes the run invalid.
The stage result is the run state. The JSONL is the refinement substrate. The
CSV is for humans and interoperability.

## Refinement Loop

Users should be able to say:

- "based on that search, only senior/staff"
- "drop the non-SF candidates"
- "show me people from infra companies too"
- "hydrate the next 50"
- "export the Cursor/Datadog-looking people"
- "make a new slice around backend infra"
- "approve"
- "yolo"
- "change it to senior/staff only before running"

The agent should load the prior task state and artifacts, create a child task,
and write a new artifact set.

Approval language should be first-class:

- `approve` continues the proposed next step
- `yolo` lets the agent continue routine branch decisions for the current run,
  while still logging each major action
- any other instruction at the approval gate becomes a change request and
  updates the plan before execution

## Terminal UX

V1:

- compact terminal table from JSON task state
- CSV/JSONL artifacts for external tools
- final answer includes state, manifest, CSV, and JSONL paths
- optional curses TUI with chat transcript on the left, persistent input at the
  bottom, and a right pane for either prior search runs or candidates
- plain text in the TUI goes through a one-shot NanoClaw chat command when the
  bridge is configured
- the TUI can start the local NanoClaw daemon, but NanoClaw still needs its
  CLI channel, container image, and credentials configured
- review actions are appended to a JSONL review log beside the search artifacts

V2:

- profile detail pane with matched evidence and slice provenance
- visible slice provenance and filter expression
- review events written to JSONL
- command-driven child searches from review/refinement events
- tighter embedding into NanoClaw's live session/socket instead of a one-shot
  command bridge

This can evolve into a recruiting-native TUI that feels closer to Claude Code or
Codex: the agent plans searches, executes slices, persists artifacts, and the
human reviews/manipulates the frontier quickly.
