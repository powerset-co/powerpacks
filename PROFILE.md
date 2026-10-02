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

People searches, “who do I know for this job?”, posting URLs, pasted JDs,
“who in my network can help <company>?”, company hiring requests and search
refinements → `packs/search/skills/search/SKILL.md`. Users need not name a skill.
The skill owns network defaults, specialist routing and recovery; follow its
references rather than duplicating those procedures here.
