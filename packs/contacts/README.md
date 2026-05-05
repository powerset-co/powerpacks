# Contacts Pack

Skills for browsing Powerset contacts and company directories through the
Powerset Search MCP. These are thin, app-equivalent skill wrappers: they use the
same contacts/company-directory APIs that power the web app and do not introduce
semantic people search unless another skill explicitly does so.

## Skills

- `search-contacts` — browse my contacts or contacts in a named set. Routes to
  MCP `list_contacts`, `list_sets`, and `list_set_contacts`.
- `company-directory` — list people who work at a company. Routes to MCP
  `list_company_people`, which wraps the existing company-directory REST
  endpoint with `include_people=true`.

## Prerequisite

Install the Powerset Search MCP:

```bash
python3 packs/powerset/primitives/mcp_install/mcp_install.py install --host all
```
