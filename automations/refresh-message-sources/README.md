# Refresh message sources

Runs every day at 6:00 AM in the machine's local timezone.

The automation:

1. refreshes every already-configured Gmail account through `$import-gmail`
   with the bounded three-year window;
2. refreshes both iMessage and WhatsApp through `$import-messages`;
3. imports deterministic metadata-only contact changes and performs the normal
   all-source fan-in merge;
4. stops before Deep Context, paid providers, indexing, or uploads; and
5. writes the latest source counts and state to
   `.powerpacks/automations/refresh-message-sources/latest.json`.

Existing Gmail OAuth, Full Disk Access, and WhatsApp links are reused. A run
that needs new authorization, permission changes, or re-linking stops and
reports the blocker instead of opening an unattended consent flow.

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
