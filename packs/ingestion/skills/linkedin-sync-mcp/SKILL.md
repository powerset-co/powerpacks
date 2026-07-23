---
name: linkedin-sync-mcp
description: Configure a LinkedIn MCP server and track local non-secret LinkedIn MCP account state.
---

# LinkedIn Sync MCP

Use when a user wants to connect LinkedIn through an MCP rather than uploading a CSV.

Current recommended MCP candidate:

- https://github.com/stickerdaniel/linkedin-mcp-server
- package: `linkedin-scraper-mcp@latest`

This is WIP for connection export, so do not represent it as equivalent to CSV ingestion yet.
Use it for setup, authenticated profile/search tools, and future connection export once upstream exposes it.

```bash
uv run --project . python packs/ingestion/primitives/setup/linkedin_mcp_import.py instructions
uvx linkedin-scraper-mcp@latest --login
uv run --project . python packs/ingestion/primitives/setup/linkedin_mcp_import.py mark-linked --username <profile-url-or-username>
```

Do not store LinkedIn credentials or cookies in Powerpacks.
