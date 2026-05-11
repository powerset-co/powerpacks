# linkedin_mcp_import

LinkedIn MCP setup/status wrapper for `stickerdaniel/linkedin-mcp-server`.

This primitive does not vendor or import that WIP MCP server. It helps users add
it to their MCP client and records non-secret setup state in:

`.powerpacks/ingestion/accounts.json`

Connection export is WIP upstream, so this is currently not a full connection
sync replacement for the CSV flow.

```bash
uv run --project . python packs/ingestion/primitives/linkedin_mcp_import/linkedin_mcp_import.py instructions
uvx linkedin-scraper-mcp@latest --login
uv run --project . python packs/ingestion/primitives/linkedin_mcp_import/linkedin_mcp_import.py mark-linked --username <profile-url-or-username>
```

Known upstream tools include `get_my_profile`, `get_person_profile`,
`search_people`, `get_company_employees`, messaging/feed/job tools.
