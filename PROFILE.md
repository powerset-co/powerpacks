# Powerpacks Agent Profile

This tracked file is the source template for generated local agent profiles.
`bin/agent-bootstrap` copies it into harness-specific files such as
`.codex/AGENTS.md` and appends non-secret clone/user context.

Generated profile files are local state. Do not commit them.

## Sub-agent delegation

The user explicitly authorizes Codex to use sub-agents for this repo. If skills
request sub-agents, use them. Leverage sub-agents to keep the main conversation
clean and concise.

For Arthur's Vorflux usage, use independent cross-examination on non-trivial
work when the host exposes sub-agent or review tooling: after implementing, ask
one or two review/testing agents to challenge correctness, missing tests,
privacy/security, cross-repo effects, and whether the change actually satisfies
the request. Reviewers should report findings; the main agent owns fixes and
final validation. If no review mechanism is available, do an explicit
self-review and say the independent review path was unavailable.

## Arthur/Vorflux operating defaults

Prefer intent-based routing and do the work rather than stopping at a plan. Use
default model routing unless a task clearly needs a specialist: builder for broad
implementation, debug for reproductions, testing agents for noisy suites, and
review agents for adversarial checks. If a model seems shallow, narrow the task
and add an independent reviewer instead of accepting under-verified work.

Validate with the smallest deterministic check first, then broader repo-guided
checks for risky changes. Keep status concise, preserve unrelated user changes,
and never paste or commit secret values.

## Local Powerset Defaults

When the generated profile includes an authenticated Powerset user or default
set, answer simple self-introspection questions from that generated context.
Do not run doctor checks, MCP set listing, network refreshes, or skill workflows
for that narrow question unless the user asks to verify live or change the set.

Never paste secret env values into chat.
