# msgvault_setup

Guided setup for msgvault Gmail OAuth and Codex MCP registration.

```bash
uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py status
uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py browser-setup --email you@gmail.com
uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py setup --email you@gmail.com
uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py setup --client-secret ~/Downloads/client_secret.json --email you@gmail.com
uv run --project . python packs/ingestion/primitives/setup/msgvault_setup.py create-oauth-app --email you@gmail.com
```

The primitive stores secrets only under `~/.msgvault/`, updates
`~/.msgvault/config.toml`, runs `msgvault init-db`, and can register the Codex
MCP server with `codex mcp add msgvault -- msgvault mcp`.

`browser-setup` opens Google Console in a persistent Chrome profile, lets the
user finish Google login/security screens, then attempts to create a project,
enable Gmail API, configure the OAuth screen, create a Desktop OAuth client
named `local-msg-vault`, download the client secret JSON, and feed it back into
msgvault setup.
