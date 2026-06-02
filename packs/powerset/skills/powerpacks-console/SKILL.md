---
name: powerpacks-console
description: Run the local Powerpacks Console web app for browsing .powerpacks artifacts, including network search and rerank results. Use when the user asks to preview, inspect, browse, or view Powerpacks results locally in a browser.
---

# Powerpacks Console

Powerpacks Console is the local browser UI in `app/`. It reads artifacts from
`.powerpacks/` via a Vite dev-server middleware and is intended to become the
shared top-level console for search, rerank/review, Sales Nav, messages, and
contact import outputs.

## Start in the background

From the Powerpacks repo root, run:

```bash
scripts/run-powerpacks-console.sh start
```

If the current Codex cwd is not the Powerpacks repo, resolve the installed
bundle first:

```bash
if [[ -n "${POWERPACKS_REPO_ROOT:-}" ]]; then
  "$POWERPACKS_REPO_ROOT/scripts/run-powerpacks-console.sh" start
elif [[ -x "scripts/run-powerpacks-console.sh" && -d "packs" ]]; then
  scripts/run-powerpacks-console.sh start
else
  "$HOME/.codex/powerpacks/scripts/run-powerpacks-console.sh" start
fi
```

The script:

- installs `app/` npm dependencies if needed
- starts Vite in the background
- binds to `0.0.0.0` by default for LAN/Tailscale preview
- reads `.powerpacks` from the repo root by default
- writes logs/PID under `.powerpacks/servers/`

Useful commands:

```bash
scripts/run-powerpacks-console.sh status
scripts/run-powerpacks-console.sh restart
scripts/run-powerpacks-console.sh stop
```

Open a specific app route:

```bash
scripts/run-powerpacks-console.sh start --path /onboarding --open
scripts/run-powerpacks-console.sh start --path /setup
```

Default URL is usually:

```text
http://localhost:5177/
```

Vite may choose the next open port if 5177 is already busy. Check the script
output or `.powerpacks/servers/powerpacks-console.log` for the actual URL.

## Alternate artifact root

To point the console at a different Powerpacks checkout or artifact directory
layout, set `POWERPACKS_REPO_ROOT`:

```bash
POWERPACKS_REPO_ROOT=/path/to/powerpacks /path/to/powerpacks/scripts/run-powerpacks-console.sh start
```

The app expects content in:

```text
$POWERPACKS_REPO_ROOT/.powerpacks/
```

## Current scope

Current implemented views:

- `/onboarding` guided setup flow for account linking, import, enrichment, and
  local indexing
- `/setup` detailed setup tables and stage controls
- `/setup/imessage/review` local messages review
- lists `.powerpacks/runs/search-network-*.json`
- reads run state and `expand_search_request` query expansion
- reads result JSONL in pages of 50
- reads hydrated profiles for the visible page only
- uses rerank CSV (`llm_rerank_candidates/query_results.csv`) when present
- shows app-style person/results table with tags, trait scores, overall score,
  and reasoning

Do not add Sales Nav review or unrelated write flows unless the user explicitly
asks. Setup and messages review are now first-class local console surfaces.
