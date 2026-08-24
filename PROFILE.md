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
- person lookup, GTM, and recruiting → persist one typed `SearchSpec` and run
  `packs.search.pipeline.search`; canonical layers return person-grain
  `CandidateFrontier` data in typed `StageResult` outputs
- people at a company → GTM with company constraints
- company-only local relational/directory questions and other relational local
  output → `$search-sql`; contact-field output → `$search-contacts`
- no public company-search command and no backend fallback
- ambiguous intent → `needs_input` and one clarification, with no retrieval
