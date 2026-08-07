# Contacts Pack

Skills for browsing Powerset contacts through the Powerset Search MCP. These
are thin, app-equivalent skill wrappers: they use the same contacts API that
powers the web app and do not introduce semantic people search.

## Skills

- `search-contacts` — browse my contacts or contacts in a named set. Routes to
  MCP `list_contacts`, `list_sets`, and `list_set_contacts`.

People-at-company searches such as "people who work at OpenAI" are handled by
`$search` as a GTM people search with company constraints. Company-only local
relational questions route to `$search-sql`.

## Prerequisite

Install the Powerset Search MCP:

```bash
python3 packs/powerset/primitives/mcp_install/mcp_install.py install --host all
```
