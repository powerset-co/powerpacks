# Task Harness

Powerpacks uses tasks as the plan/execute ledger for `$search-network`.

## Contract

- Skills define reasoning workflow.
- Tasks define step order, input/output expectations, and state.
- Primitives do executable work or deterministic state updates.

## Search Network Task

The task template lives at:

- `powerpacks/tasks/search-network.task.json`

Each run should create state that validates against:

- `powerpacks/schemas/task-run.schema.json`

The run state should include:

- original query
- active constraints
- every step output
- strategy decisions
- counts and slice yields
- frontier assessment
- final summary

## Execution Rule

Run one step, record its output, then decide the next step from current state.
Do not assume slicing is mandatory.

## V1 Constraints

- no summary search
- no company-signal search
- no LLM candidate enrichment
- no LLM filter
- no expensive scoring or reranking

Query expansion can still use the existing extraction stack. That is the
decomposition stage, not candidate enrichment.
