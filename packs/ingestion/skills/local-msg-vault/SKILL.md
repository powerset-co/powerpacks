---
name: local-msg-vault
description: Alias for msgvault local Gmail archive setup. Use for `$local-msg-vault`, local message vault setup, browser-assisted Google OAuth Desktop app creation named `local-msg-vault`, msgvault account auth, and msgvault MCP registration.
---

# local-msg-vault

This is an alias for the `msgvault` skill. Load and follow
`packs/ingestion/skills/msgvault/SKILL.md`.

Default setup command:

```bash
uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py browser-setup --email <gmail>
uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py add-test-users <gmail>
uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py add-account --email <gmail>
```
