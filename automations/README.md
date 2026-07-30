# Powerpacks automations

Portable Codex App automations live here. Each directory is installable with
[`codex-automations`](https://github.com/vltansky/codex-automations) and contains:

- `codex-automation.json` — portable package metadata
- `automation.toml` — the native Codex automation
- `README.md` — behavior, safety boundaries, and install instructions

Preview an automation before installing it:

```bash
npx -y codex-automations add ./automations/<name> --cwd "$PWD" --dry-run
```

Installs are paused unless `--activate` is supplied.
