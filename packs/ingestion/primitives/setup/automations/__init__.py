"""msgvault/Gmail OAuth setup automation subpackage.

Single-concern modules behind the thin CLI entry
`packs/ingestion/primitives/setup/msgvault_setup.py`:

- shell:          subprocess runners and JSON/progress output helpers.
- msgvault_home:  msgvault home state, config.toml, client secrets, msgvault binary.
- gcloud_project: gcloud auth/context, project create-choose-validate, Gmail API,
                  console URLs.
- mcp:            Codex MCP registration for the msgvault server.
- oauth_browser:  playwright-driven Google Console automation
                  (google_oauth_browser.js lives here with its driver).
- accounts:       account authorization, OAuth health checks, status payloads.
- setup_flows:    non-browser command flows (setup/configure/create-oauth-app/
                  add-account).
- browser_flows:  browser-driven command flows (browser-setup/add-test-users).

Docs only — no bootstrap here; script mode never imports this package
initializer before the entry's own repo-root stanza runs.
"""
