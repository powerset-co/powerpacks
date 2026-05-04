# Powerpacks Task Flow

Powerpacks separates agent reasoning from deterministic execution.

## Current Flow

```mermaid
flowchart TD
  U[User asks /search-network query] --> S[search-network skill]
  S --> T[task_state init]
  T --> E[extract-search-query skill output]
  E --> R[record expand_search_request]
  R --> P[plan strategy and approval]
  P --> A{User choice}
  A -->|search only| X[approve search_only]
  A -->|rerank| XR[approve rerank]
  A -->|changes| C[request-changes]
  C --> E
  X --> ID[resolve IDs]
  XR --> ID
  ID --> PF[apply_prefilters]
  PF --> CT[count_candidates]
  CT --> D{direct or sliced}
  D -->|direct| EX[execute_role_search]
  D -->|sliced| SL[generate and execute slices]
  SL --> M[merge frontier]
  EX --> H[hydrate_people]
  M --> H
  H --> IO[persist_search_results]
  IO --> AR{rerank approved?}
  AR -->|no| OUT[return CSV/JSONL artifacts]
  AR -->|yes| PREP[agentic_candidate_review prepare]
  PREP --> SUB[host dispatches shard reviewers]
  SUB --> RED[agentic_candidate_review reduce]
  RED --> OUT
```

## Responsibilities

`search-network` is the high-level orchestrator. It owns the conversation,
approval gate, task state, and final response.

`extract-search-query` is the extraction sub-skill. It turns user intent into a
schema-valid `role_search_filters` payload and records no candidates.

Primitives are executable steps. They should not guess user intent:

- `resolve_education`
- `resolve_investors`
- `resolve_companies`
- `apply_prefilters`
- `count_candidates`
- `execute_role_search`
- `execute_search_slice`
- `hydrate_people`
- `persist_search_results`
- `agentic_candidate_review`

## State Contract

Each run has one JSON state file:

```text
.powerpacks/runs/search-network-<uuid>-<query-slug>.json
```

Every meaningful step is appended to `steps[]`:

```json
{
  "id": "expand_search_request",
  "status": "completed",
  "output": {
    "role_search_filters": {}
  }
}
```

Every write also appends an audit event:

```text
.powerpacks/runs/search-network-<uuid>-<query-slug>.json.events.jsonl
```

Downstream primitives read prior step outputs from state. For example,
`execute_role_search` reads:

- `expand_search_request.output.role_search_filters`
- resolved IDs from `resolve_education`, `resolve_companies`, and
  `resolve_investors`
- `base_candidate_ids` from `apply_prefilters`

## Extraction Harness

The primitive parity harness is intentionally lower level:

```mermaid
flowchart LR
  Y[Recall YAML] --> H[deterministic harness decomposition]
  H --> P[Powerpacks primitives]
  P --> R[recall report]
```

That proves: if the payload is right, do the primitives retrieve representative
data?

It does not prove that Codex, Claude Code, or NanoClaw can extract the right
payload from natural language.

The agent extraction harness should be separate:

```mermaid
flowchart LR
  Y[Recall YAML query] --> A[host agent running extract-search-query skill]
  A --> J[decomposed-query JSON artifact]
  J --> V[schema validation]
  V --> P[Powerpacks primitives]
  P --> R[recall report]
```

This is the harness that should evaluate query decomposition quality. It should
store the extracted JSON per case so misses can be debugged as either:

- extraction miss
- resolver/prefilter miss
- retrieval ranking/window miss
- hydration/artifact miss

The scaffold for this is `powerpacks/evals/run_agent_extraction_harness.py`.
It writes one prompt per recall case, invokes a caller-provided host command,
saves `<case>.extracted.json`, validates the minimum extraction contract, then
feeds the JSON into the same primitive execution path as
`run_recall_parity.py`.

## Skill Composition

Skills are not shell subroutines. Codex and Claude Code load skill
instructions, then the host agent orchestrates the sequence.

The intended composition is:

1. `search-network` decides this is a people search.
2. It invokes the `extract-search-query` instructions to produce
   `expand_search_request` JSON.
3. It records that JSON in task state.
4. It runs deterministic primitives from state.
5. It optionally invokes host-level shard reviewers only after hydration and
   explicit rerank approval.

This keeps extraction auditable while preserving the agentic UX.

## Handoff Artifacts

High-level workflows should chain skills through written artifacts, not memory.
For `search-network`, the canonical handoff is the task state file plus any
exported CSV/JSONL artifacts.

```mermaid
flowchart LR
  SC[search-company skill] --> CID[company_ids.json or resolve_companies step]
  CID --> SN[search-network skill]
  SN --> CAND[candidates.jsonl/candidates.csv]
  CAND --> FP[fix-people skill, only if requested]
  FP --> OUT[reviewed merge/fix artifact]
```

`search-network` can orchestrate `extract-search-query` and `search-company`
inside a normal search. It should only invoke cleanup or reconciliation skills
such as `fix-people` when the user explicitly asks for that post-search work.

## Title Inspection Gap

Some recall cases need more than static role aliases. Examples include:

- `ai engineers`
- `data science leaders`
- `people with gtm experience`
- domain-adjacent company searches

For those, extraction should plan a title-inspection step:

```mermaid
flowchart TD
  E[extract query] --> Q{title ambiguity?}
  Q -->|no| P[normal primitive plan]
  Q -->|yes| T[inspect indexed title clusters]
  T --> L[agent selects title clusters]
  L --> P
```

That title-inspection step should become a first-class primitive/skill pair,
not hidden in recall eval code.
