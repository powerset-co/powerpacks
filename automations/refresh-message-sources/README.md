# Refresh message sources

Runs every day at 6:00 AM in the machine's local timezone.

The automation:

1. runs `$import-gmail sync`, which refreshes every already-configured healthy
   Gmail account with the bounded three-year window;
2. runs `$import-messages sync`, which refreshes whichever of iMessage and
   WhatsApp are already configured;
3. imports deterministic metadata-only contact changes and performs the normal
   all-source fan-in merge;
4. stops before Deep Context, paid providers, indexing, or uploads; and
5. writes the latest source counts and state to
   `.powerpacks/automations/refresh-message-sources/latest.json`.

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
