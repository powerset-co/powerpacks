# Pond prompt routing

## Decision

The existing recruiter-plan model chooses one pond prompt family from the full JD, title, and an optional source-department hint. The saved plan freezes that choice for Pond 1 and every later pond.

Supported families are:

- `engineering`
- `marketing-sales`
- `customer-support`
- `operations-finance-people`
- `design`
- `general`

The model follows the occupation that owns the recurring work. A source department is supporting evidence, not an authoritative mapping. Unsupported roles use `general`. No regex router or second model call is used.

## Production path

```text
JD + source hint
  -> existing recruiter-plan call
  -> plan.json: pond_prompt_family
  -> family Pond 1 prompt + production precedent retrieval
  -> same saved family for every next-pond prompt
```

An explicit `--system-file` still overrides the selected Pond 1 prompt for controlled experiments. Reusing an approved plan also reuses its family, which keeps prompt comparisons matched.

## Evidence

A controlled 170-JD comparison held saved plans, production RAG, model, reasoning, and cards constant. The combined prompt regressed every cohort against its split control:

| Cohort | JDs | Split mean F1 | Combined mean F1 | Split arm match | Combined arm match |
| --- | ---: | ---: | ---: | ---: | ---: |
| Engineering | 108 | 0.9472 | 0.8050 | 95.7% | 56.5% |
| Marketing & Sales | 43 | 0.6405 | 0.5716 | 70.0% | 70.0% |
| Customer & Support | 19 | 0.7177 | 0.6116 | 60.0% | 60.0% |

The accepted Operations, Finance, and People prompt completed 32/32 JDs with 124 calls and improved all five labeled rows over its baseline. The accepted Design prompt completed 9/9 JDs with 35 calls.

## Acceptance criteria

- One semantic family selection in the existing planning call.
- The saved plan is the only routing authority during a search.
- Pond 1 and next-pond use the same selected family.
- Explicit general fallback for unsupported work.
- Family prompt snapshots are versioned with production code.
- No title hardcoding, regex department mapping, or copied human chains.
