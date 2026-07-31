# Powerpacks Agent Profile

This tracked file is the source template for generated local agent profiles.
`bin/agent-bootstrap` copies it into harness-specific files such as
`.codex/AGENTS.md` and appends non-secret clone/user context.

Generated profile files are local state. Do not commit them.

## Sub-agent delegation

The user explicitly authorizes Codex to use sub-agents for this repo. If skills
request sub-agents, use them. Leverage sub-agents to keep the main conversation
clean and concise.

## Local Powerset Defaults

When the generated profile includes an authenticated Powerset user or default
set, answer simple self-introspection questions from that generated context.
Do not run doctor checks, MCP set listing, network refreshes, or skill workflows
for that narrow question unless the user asks to verify live or change the set.

Never paste secret env values into chat.

## Powerpacks Skill Routing

- `$search`, people search, network search, role/title/location/school
  searches, or company-directory people lookups →
  `packs/search/skills/search/SKILL.md`
- ordinary people search → live legacy `search_network_pipeline.py` prepare,
  Review, and approved execution flow
- bare-person lookup → typed `packs.search.pipeline.search`; local fields are
  capability-derived and Powerset supports set-scoped person ID, name, handle,
  and profile URL lookup only
- job posting URLs, pasted job descriptions, or complex role briefs → canonical
  legacy `$search` recruiting through `deep_search_loop.py` until atomic cutover
- company lookup/resolution → live `$search-company`; relational local output →
  `$search-sql`; contact-field output → `$search-contacts`
- `packs.search.pipeline.search` → deterministic bare-person lookup owner;
  otherwise an additive opt-in candidate path for
  deterministic tests and approved read-only validation only, never an implied
  production cutover or paid-run authorization
- ambiguous intent → `needs_input` and one clarification, with no retrieval
