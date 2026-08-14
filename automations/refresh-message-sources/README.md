# Refresh message sources

Runs every day at 6:00 AM in the machine's local timezone.
Each run titles its Codex task `Refresh message sources MM/DD/YY` using that
local run date and archives the completed automation task after its snapshot and
memory are durable. Manual `$refresh-message-sources` runs stay active.

The automation:

1. runs `$refresh-message-sources`, which delegates to `$import-gmail sync` to
   refresh every already-configured healthy
   Gmail account with the bounded three-year window;
2. delegates to `$import-messages sync`, which refreshes whichever of iMessage and
   WhatsApp are already configured;
3. imports deterministic metadata-only contact changes without fan-in;
4. stops before Deep Context, paid providers, indexing, uploads, or any other
   processing;
5. writes the latest source counts and state to
   `.powerpacks/automations/refresh-message-sources/latest.json`; and
6. archives the Codex task only when the exact
   `Automation ID: refresh-message-sources` metadata is present.

Existing Gmail OAuth, Full Disk Access, and WhatsApp links are reused.
Unconfigured or human-blocked sources are skipped and reported without blocking
the other configured sources.

## Preview

From the Powerpacks repository:

```bash
bin/install-codex-automation refresh-message-sources \
  --workspace "$PWD" \
  --dry-run
```

## Install paused

```bash
bin/install-codex-automation refresh-message-sources \
  --workspace "$PWD"
```

## Install and activate

```bash
bin/install-codex-automation refresh-message-sources \
  --workspace "$PWD" \
  --activate
```

The native file is installed at
`$CODEX_HOME/automations/refresh-message-sources/automation.toml`.
