# NanoClaw Plan Harness

Run plan-only search-network evals against NanoClaw.

This harness is intentionally not part of the normal search flow. It asks
NanoClaw to produce and optionally initialize a task plan without retrieval,
hydration, LLM filtering, or scoring. The goal is to inspect whether the claw
chooses sane primitives, constraints, contracts, and next actions.

## Render Prompts Only

```bash
python powerpacks/adapters/nanoclaw/primitives/nanoclaw_plan_harness/nanoclaw_plan_harness.py \
  --cases powerpacks/evals/search-network-plan/cases.json \
  --render-only
```

## Live NanoClaw Run

```bash
python powerpacks/adapters/nanoclaw/primitives/nanoclaw_plan_harness/nanoclaw_plan_harness.py \
  --nanoclaw-dir /path/to/nanoclaw \
  --cases powerpacks/evals/search-network-plan/cases.json \
  --case software-engineers-sf
```

Outputs are written to `.powerpacks/plan-evals/<timestamp>/`.

The harness uses a long wall-clock ceiling by default (`--timeout 3600`) and
polls NanoClaw health instead of treating slow responses as failures. It fails
early only when the runtime is unhealthy or idle longer than `--idle-timeout`
(default 300 seconds). Use `--timeout 0` to remove the wall-clock ceiling.

Inside NanoClaw agent containers, the installed Powerpacks directory is expected
at `/workspace/extra/powerpacks`. The installer wires this through
`groups/*/container.json` and the NanoClaw mount allowlist.

## What It Checks

- NanoClaw returns a plan, not a full retrieval.
- The response mentions required plan concepts for the case.
- The response avoids explicitly banned concepts such as expensive scoring,
  LLM enrichment, company signals, or summary generation.
- The response is saved for inspection even when checks fail.
- The response should end at an approval gate: approve, yolo, or request
  changes.
