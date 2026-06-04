# Apollo MCP primitive

Registers the public `apollo-mcp` stdio MCP package with Codex and/or Claude
Code, reading `APOLLO_API_KEY` from the shell or `.env` without printing it.

```bash
uv run --project . python packs/apollo/primitives/apollo_mcp/apollo_mcp.py status
uv run --project . python packs/apollo/primitives/apollo_mcp/apollo_mcp.py install --host codex
uv run --project . python packs/apollo/primitives/apollo_mcp/apollo_mcp.py prepare-leads --input leads.csv
```

Apollo sequence/campaign operations generally require a Master API key from
Apollo settings. The primitive does not call Apollo APIs directly; MCP tools do
that after the host starts the registered server.
