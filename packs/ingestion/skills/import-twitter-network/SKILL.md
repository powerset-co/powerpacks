---
name: import-twitter-network
description: Import a Twitter/X network into Powerpacks-local ingestion artifacts using the RapidAPI-backed crawl and local review pipeline.
---

# import-twitter-network

Use this skill when the user asks to import a Twitter/X network into Powerpacks-local ingestion artifacts.

## Workflow

Use the primitive at:

```bash
packs/ingestion/primitives/discover_contacts_pipeline/twitter/network_import.py
```

The source crawl is RapidAPI-backed only; do not ask for or use a local Twitter CSV as the production source. All artifacts must remain under `.powerpacks/network-import/discover/twitter/`.

Pipeline stages: RapidAPI Twitter crawl → local heuristic score → OpenAI MOE expert evaluation → free parallel LinkedIn pre-resolution → parallel RapidAPI LinkedIn validation → canonical `people.csv` formatting with temporary legacy alias.

## Commands

Start a run:

```bash
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/twitter/network_import.py run --handle <operator_handle> --max-pages <n>
```

The run stops before spend-bearing API calls. After explicit user approval:

```bash
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/twitter/network_import.py approve
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/twitter/network_import.py continue
```

Check status:

```bash
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/twitter/network_import.py status
```

## Guardrails

- Ask before RapidAPI Twitter crawl, OpenAI MOE evaluation, or RapidAPI LinkedIn validation.
- Never print API keys.
- No Postgres writes.
- No browser URLs containing bearer tokens.
- A hidden row cap exists only for tiny local smoke tests. Do not use it in real workflows.
- If using secrets from another local repo for a smoke test, source them into the process environment only and do not paste values into chat.

## Outputs

Summarize only counts and paths, not full datasets:

- `followers_dump.csv`
- `candidates.csv`
- `moe_evaluated.csv`
- `linkedin_resolved.csv`
- `linkedin_resolution_queue.csv`
- `linkedin_validated.csv`
- `people.csv`
- `people_harmonic_all.csv` temporary compatibility alias
