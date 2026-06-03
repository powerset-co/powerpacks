# Setup Enrichment Contract

Status: in progress
Owner: Codex
Created: 2026-06-02

## Product Contract

Setup has four separate phases:

1. Account Linking records source access and account/export paths only.
2. Import syncs or extracts raw vertical data into local artifacts.
3. Enrichment resolves imported candidates to LinkedIn profiles, runs RapidAPI on resolved LinkedIn profiles, and merges enriched profiles into the local network.
4. Indexing processes the merged enriched people into local search artifacts.

Enrichment must not trigger Gmail-to-msgvault sync, iMessage extraction, WhatsApp sync, or source-linking actions. Those belong to Import or Account Linking.

## Source Contracts

- LinkedIn: parse Connections.csv candidates, then run RapidAPI profile enrichment for LinkedIn URLs.
- Gmail: read existing msgvault/imported Gmail contact candidates, resolve emails/domains/names to LinkedIn using directory first and Parallel for unresolved candidates, apply those LinkedIn resolutions through the Gmail enrich/apply layer, and write source-attributed `people.csv`. RapidAPI hydration can be skipped/deferred if it is slow; Gmail enrichment must not expose alternate product providers.
- Messages: use reviewed/approved message candidates, use existing local/Powerset matches or deep research results to get LinkedIn URLs, then run RapidAPI for resolved LinkedIn URLs.
- Twitter/X: keep disabled until follower import and resolution are wired.

## Tasks

- [x] Audit current UI actions and primitive entrypoints for Import vs Enrichment phase mixing.
- [x] Add or reuse a primitive entrypoint for enrichment-only fan-in that can run Gmail directory lookup, Gmail Parallel resolution, Gmail apply/enrich to `people.csv`, Messages RapidAPI, LinkedIn RapidAPI, merge, and DuckDB without raw source sync/extraction.
- [ ] Gmail enrichment should combine and dedupe unresolved candidates across all linked Gmail accounts before provider lookup, then attribute resolved rows back to the contributing accounts/sources.
- [x] Wire `/setup` enrichment row buttons and Run All to enrichment-only commands.
- [x] Keep `/setup` import row buttons on raw source import/sync/extraction commands only.
- [ ] Fix enrichment stats so Candidates, Profiles found, Existing matches, and Not found/Skipped are derived from the correct per-source enrichment artifacts.
- [ ] Validate on `powerpacks-arthur` that Gmail enrichment does not run `msgvault sync-full`, and that import still can run Gmail sync.
- [x] Build the app and run only targeted primitive smoke checks needed for the changed entrypoints.
- [ ] Commit and push.

## Notes

- `--only-source gmail` currently stops after the source worker, so Gmail directory/Parallel/RapidAPI work has to happen in fan-in/enrichment, not in the raw Gmail source command.
- Setup should expose Gmail enrichment as one action, not provider modes. Internally, Gmail enrichment uses Parallel for unresolved email-to-LinkedIn resolution.
- Previous UI wiring reused import commands for enrichment rows, which is the core mistake to fix.
- `setup.py fan-in --resolve-gmail-linkedin` is the setup-facing Gmail enrichment switch. Do not expose `off/harness/parallel` provider modes in the setup UI or setup CLI.
- `import_network_pipeline --fan-in-only --only-source gmail --resolve-gmail-linkedin --dry-run` must not report `gmail_msgvault` or import worker groups.
