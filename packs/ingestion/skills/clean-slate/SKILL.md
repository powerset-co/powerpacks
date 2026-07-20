---
name: clean-slate
description: Full pipeclean of derived Powerpacks pipeline state. Use for $clean-slate, "clean slate", "pipeclean", "start over from scratch", "scrub the derived state", "reset the pipeline but keep my LLM work". Moves ALL derived state (merged people, directory, review decisions, import dirs, cluster index/parents/dossiers, search index) to a timestamped backup OUTSIDE the repo, preserving every paid artifact (facts, cluster pair verdicts, LinkedIn-judge verdicts, deep-research results, profile caches, the LinkedIn import, message stores, logbook). Nothing is ever deleted. For clearing only human review decisions, use $deep-context restart instead.
---

<!--
Created: 2026-07-19
Changelog:
- 2026-07-19: New skill wrapping bin/clean-slate (built 2026-07-19 as the
  pipeclean reset; previously only routed inside $deep-context).
-->

# clean-slate

One purpose: reset the pipeline to "fresh install + paid caches" so the full
flow can be re-walked from a clean slate with every LLM/API stage cache-hitting.

This is the BIG hammer. The small hammer is `$deep-context restart`, which
clears only human review decisions and keeps all derived state — route there
when the user only wants to redo the review.

## Run it

From the canonical Powerpacks repo (`$POWERPACKS_REPO_ROOT`, else
`~/powerpacks`, else `~/workspace/powerpacks`):

1. **Stop any running review server first** (a live server would keep serving
   moved files):

   ```bash
   lsof -ti :8765 | xargs kill 2>/dev/null || true
   ```

2. **Dry run** (default — free, read-only) and show the user the table:

   ```bash
   bin/clean-slate
   ```

   Summarize what WOULD move (paths + total size + backup destination) and
   what stays. Do not paraphrase the preserve list from memory — read it from
   the command's JSON output.

3. **Confirm, then apply**:

   ```bash
   bin/clean-slate --apply
   ```

   Everything scrubbed is MOVED — never deleted — to
   `~/powerpacks-backups/clean-slate-<utc>/`, which also gets a
   `clean-slate-manifest.json` restore map (restore = move paths back).

4. **STOP.** Do not start the reimports yourself, do not launch the review,
   do not build any workflow plan. End by telling the user the pipeclean
   order and that each stage cache-hits on stable keys:

   ```text
   $setup            LinkedIn import + fan-in (no RapidAPI re-spend; caches preserved)
   $import-gmail     msgvault preserved -> delta sync, free
   $import-messages  wacli store preserved -> fast incremental pull
   $deep-context     facts/cluster/judge/research all cache-hit; review from the top
   ```

## Contract (what survives, why it's safe)

Preserved in place, keyed by stable identifiers — the reason a full re-walk
costs ~nothing:

- `deep-context/facts/` (OpenAI synthesis; keyed by contact identity)
- `deep-context/merge-verdicts.csv` (cluster pair judgments)
- `deep-context/reconcile/verdicts.*` (LinkedIn judge, keyed by pub)
- `deep-context/reconcile/deep-research/` (Parallel results)
- `network-import/import/linkedin/` (source import + enrichment caches)
- `network-import/profile_cache_v2/` (RapidAPI), owner bio, message stores,
  `logbook/`, `memory/`, `ingestion/` wiring

Scrubbed (all derived; regenerates free on the re-walk): `merged/`,
`directory.csv`, `overrides/` (review decisions incl. mirrors),
`import/gmail` + `import/messages*`, `discover/`, `search-index/`, the
deep-context index/parents/dossiers/merge-candidates/raw/review state, and
reconcile summaries.

Never run `--apply` without showing the dry run and getting an explicit yes
in this conversation. Never delete anything to "clean up further" — moving to
the backup dir is the only removal this skill performs.
