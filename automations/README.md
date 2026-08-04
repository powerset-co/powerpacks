# Powerpacks automations

Portable Codex App automations live here. Each directory contains:

- `automation.toml` — the native Codex automation template
- `README.md` — behavior, safety boundaries, and install instructions

Preview an automation before installing it:

```bash
bin/install-codex-automation <name> --workspace "$PWD" --dry-run
```

Install it paused:

```bash
bin/install-codex-automation <name> --workspace "$PWD"
```

Pass `--activate` to enable its schedule immediately. The installer writes the
rendered native file under `$CODEX_HOME/automations/`, which the Codex App reads
for its Scheduled UI.
