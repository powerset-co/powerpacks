# Ingestion pipeline audit — full session log 📋

Created: 2026-07-23
Changelog: (living record of the 2026-07-22/23 audit; temporary root file like HYGIENE.md)

## Why this started

Duplicate people in the worth review on Jake's mirror (`~/workspace/powerpacks-jake-mirror`, review server on 8798). Diagnosis was rebuilt from the files `$deep-context` actually reads (`facts/`, `index.json`, `overrides/review.csv`) after an earlier false trail through fossil files. Standing invariant set by Arthur mid-audit: **the only surfaces that matter are `$setup`, `$import-gmail`, `$import-messages`, `$deep-context`** (+ side skills like `$logbook`); anything not on those chains is a relic.

## The five bugs (all found, three fixed, two open)

| Bug | What | Status |
|---|---|---|
| BUG-1 | Stale `message-linkedin:*` ghost identities rendered duplicate worth cards | FIXED pre-audit (#304/#305 ghost-hide + legacy folding) |
| BUG-2 | Merge judge never saw phone identifiers (not mined, not rendered); same-name/same-phone pairs refused 0.63–0.84, one TRUE at 0.67 under the 0.70 bar | FIXED — **PR #313**: `slam_dunk_verdict` (identical name + shared identifier ⇒ 0.99 in code), mined `extra_phones`, computed `SHARED IDENTIFIERS` prompt section, judge rule; 104 mirror pairs now merge free |
| BUG-3 | `enrich_resolved` handoff dropped: legacy orchestrator enriched resolved-Gmail people; rewrite parked it ("deep-context owns both") and deep-context never received it → 2,687 people outside `merged/people.csv`, unmergeable era worth cards. 2,684/2,687 have usable cached RapidAPI profiles (`profile_cache_v2/`, 10,298 records) | REMEDIATION SHIPPED — **PR #316** `migrate-legacy` (see below); the paid apply+judge run is NOT yet executed |
| BUG-4 | 1,443 message match suggestions parked; root cause precisely located during audit: `match_local_candidates` tier-0 approvals read `research_review.csv`, whose producer was the retired review flow (deleted in #315) → gate can never pass on fresh installs, all identifier matches demote to `suggested` | OPEN — documented as Known gap in the module; replacement approval surface belongs to deep-context (design decision pending) |
| BUG-5 | Legacy Parallel resolution (per-email, ≥0.75, no judge) mislinked identities — live receipt: one contact's role-inbox row resolved to a different person's profile entirely | OPEN — becomes recoverable once migrate-legacy apply+judge runs (links enter the verify loop) |

## Merged PRs (main, mirror deployed)

- **#313** `feat(deep-context): shared identifiers decide merges in code, not model attention` — deterministic merges + identifier-aware judge. Mirror dry-run: 2,626 people / 1,275 pairs / **104 deterministic** / 1,171 to judge ($4.68–$23.42 if re-judged).
- **#315** `refactor!: delete the retired research-review flow and the before_split fossil` — **−6,924 lines**: `before_split.py` (3,465; live closure of 5 dispatched functions extracted to `gmail_import_steps.py`, 79 defs/1,487 lines), research-review quartet (`llm_review_contacts`, `prepare_research_queue`, `build_research_review_csv`, `review_research_web`), zero-ref `estimate_gmail_sync` + `gmail_metadata_sync`, dead tests. Flow-level A/B on the mirror: gmail import outputs identical except run-timestamp columns; messages byte-identical; deep-context worth/cluster/server identical.
- **#316** `feat(deep-context)!: migrate legacy parallel resolutions into the reviewable retarget loop` — `bin/deep-context migrate-legacy` (+SKILL step 4.6): still-unverified legacy links become pending `retarget` proposals in `overrides/review.csv` (the SOT); `--apply --judge` judges against CACHED profiles via the existing research-proposal judge, bounded pool, apply-only ($0 dry-run enforced after an initial bug spent ~$1–2). Validated dry-run on BOTH datasets: jake 5,437 legacy rows → **2,687 eligible** / 1,226 in-merged / 1,524 no-facts / 3 uncached (judge est. $10.74–$53.68); arthur 309 → 187 / 76 / 46 / 0 ($0.75–$3.74). ALSO removed `--resolve-legacy`/`--approve-parallel-spend` — directory-only is the only import mode; A/B validated.

## PR #318 — the running audit branch (`arthur/audit-gmail-import`, NOT merged)

Arthur reviews manually, drops comments; each batch = one commit, verdicts posted as PR comments. **No test-suite runs per instruction — compile/import/CLI smokes only; CI must run before merge.**

| Batch | Scope | Highlights |
|---|---|---|
| 1 | `import_contacts_pipeline/gmail.py` | try/except import dup → single bootstrap; docstrings; `GmailImportLedger` dataclass (correction: ledger was defined once, not multiply); loader renamed `load_gmail_import_steps`; dead `blocked_approval`/exit-20 removed (verified zero setters); orphaned constant + `auto_approvals` keys cut |
| 2 | `messages.py`, dispatcher, matcher | per-source dispatcher deleted (+its only test); helpers → util modules; `merge_matched_people_rows` renamed+documented; dead `directory_source_keys`/`messages_people_directory_keys` deleted; **BUG-4 root cause found via Arthur's "why approvals?" question**; correction: `__init__.py` can't host the bootstrap (script mode never imports it) |
| 3 | per-vertical restructure BOTH pipelines | `discover_contacts_pipeline/{gmail,linkedin,messages}/`, gmail split discover/sync/util; `import_contacts_pipeline/{gmail,messages,linkedin}/`; package `__init__` re-exports keep module imports working; all path callers swept; verified-live keeps: `gmail_network_import` (msgvault reader for deep_context/sources), `directory.py`, orchestrator |
| 4 | strictness | `import_whatsapp_wacli` verified live (6 consumers, kept); `discover()` `**_` removed — **exposed never-honored orchestrator kwargs** (`ledger_path`/`output_dir`/`operator_id`), phantom kwargs deleted |
| 5 | one config door | `resolve_discovery_inputs()` → frozen `GmailDiscoveryInputs` (explicit > accounts.json > discovery.config); `accounts_file` is the one param name everywhere; linkedin `**_` had the same phantom kwargs — strict now; 4-phase comment pass over discover() (incremental children APPEND-only; `gmail_incremental_input_id` = replay-dedup key); fixed self-inflicted latent NameError (`run_gmail_msgvault` moved to discover.py) |
| 6 | typed manifests | `StagePayload` base + `gmail/linkedin/messages models.py` typing every discover payload; all 10 emit sites construct models; last 6 duplicated import blocks standardized |
| 7 | docs | None-sentinel defaults convention documented on discover() |
| 8 | changelog sweep (3 parallel sub-agents) | history moved from function docstrings to module-top `Changelog:` blocks, docstrings present-tense; **restored two `__main__` guards batch 3 had dropped** (file invocation silently no-opped — the SKILLs' invocation mode); gmail/util unused imports pruned |
| 9 | retire marker chain; moves into deep_context | DELETED: `$enrich-email-markers` skill, `infer_linkedin_markers`, `build_resolution_queue`, `compare_resolution_ab`, `verify_gmail_resolution` (docstring-only "consumer"), `account_registry`, unrouted `linkedin-sync-csv` skill (+tests/routes/lists). MOVED into `deep_context/`: `build_email_context` (sources.py needs it; bare-module hack → package import) and `deep_research_contacts` (Parallel client; only reconcile_deep_research imports it) |
| 10 | discovery engines fold (sub-agent) + HYGIENE.md | `extract_imessage`, `whatsapp_wacli`, `normalize/merge_contacts` → `messages/`; `network_import` + `resolve_queue` → `gmail/`; linkedin/twitter `network_import` → their verticals; app/local-api + fix-powerpacks-state stale paths fixed (missed by earlier sweeps — **the console app invokes primitives by path**); `HYGIENE.md` added at root (agents read it before editing; folds into AGENTS.md later) |
| 11 | setup/enrich fold (sub-agent) | `setup_gmail`, `setup_linkedin_csv`, `msgvault_setup` (+oauth JS companion), `onboarding`, `bootstrap_network_from_exports`, `clean_slate`, `linkedin_mcp_import` → `setup/`; `enrich_people` → `enrich/`; app paths updated; jobs.ts whitelist drops deleted-dispatcher entry |
| 12 | import-stage fold (sub-agent) | `match_local_candidates` → `import_contacts_pipeline/messages/`; `merge_network_sources` → `import_contacts_pipeline/` (fan-in); references swept (index fan-in py_cmd, SKILL, 7 test files, docs); zero app/ references. DONE |
| 13 | relic skills + $setup pipeclean (sub-agent) | DELETED 8 skills (`onboard`, `ingestion-onboarding`, `import-gmail-network`, `import-linkedin-network`, `import-twitter-network`, `linkedin-sync-mcp`, `local-msg-vault` alias, `import-whatsapp`) + `bootstrap_network_from_exports` and `linkedin_mcp_import` primitives (+test/READMEs); adapter install lists pruned to live skills ONLY + new `RETIRED_SKILLS` scrub (also purges pre-audit fossils: `enrich-email-markers`, `import-contacts`, `recruit`, `deep-setup`, …); `$setup` SKILL audited command-by-command — **all current, incl. Step-5 `index_contacts_pipeline.py fan-in`** (same canonical merge door $import-gmail/$import-messages use; delegates to `merge_network_sources.py`). KEPT `setup/{onboarding,setup,setup_gmail,setup_linkedin_csv}.py` — console-app engine (jobs.ts/commands.ts/routes), NOT $setup's; $setup calls none of them. discover-contacts SKILL: Arthur's tone edit merged + restore-bundle & approve/continue resume sections deleted |

## Final target tree (post-batch-12)

```
packs/ingestion/primitives/
  discover_contacts_pipeline/   gmail/ (discover, sync, util, models, network_import, resolve_queue, import_steps)
                                messages/ (discover, extract_imessage, whatsapp_wacli, normalize_contacts, merge_contacts, models)
                                linkedin/ (discover, network_import, models)   twitter/ (network_import)
                                common, directory, discovery_config, discover_contacts_pipeline (orchestrator)
  import_contacts_pipeline/     gmail/ (importer, util)   messages/ (importer, util, match_local_candidates)
                                linkedin/ (importer)   merge_network_sources (fan-in), common, status
  enrich/                       enrich_people
  deep_context/                 22 modules + build_email_context + deep_research_contacts + migrate_legacy_resolutions
  logbook/                      unchanged
  setup/                        setup, setup_gmail, setup_linkedin_csv, msgvault_setup, onboarding,
                                clean_slate   (onboarding/setup* are console-app-only — $setup never calls them)
```

## Key numbers / state

- Jake mirror: deployed at main `3bc32b0` (#316). Worth pools 3,908 yes / 1,140 no / 2 maybe; 201 repeated-name groups (dupe scope report: `~/workspace/powerpacks-jake-mirror/.powerpacks/deep-context/review/dupe-name-report.md`). Review stage re-opened to `enrich` by forced test reruns (identical on both code versions; free re-preview moves it forward).
- Docs/artifacts: forensic gist https://gist.github.com/thearthurchen/5ed4488485c2034ea5f06440f299d4f9 ; clean pipeline-flow gist (incl. per-skill code map) https://gist.github.com/thearthurchen/29cba3708fc6853f79e76303d0d653fd ; local: `~/.claude/jobs/71864c44/tmp/{gmail-dupes-explainer.md, pipeline-code-inventory.md}`.
- Data fossils still on disk for `.bkup` cleanup: `.powerpacks/network-import/messages/people.messages.csv`, `.powerpacks/messages/contacts_review_decisions.csv`, the read-by-nothing `gmail-linkedin-resolution-*` dirs.

## Open items (ordered)

2. **CI/test suite on PR #318 before merge** — the whole audit ran under a no-local-tests instruction; expect fallout in path-listing/contract tests beyond what smokes caught.
3. **Run migrate-legacy for real** on the mirror: `--apply` (free) then `--apply --judge` (~$11–54) → auto-stands → Check-LinkedIn queue → apply-retargets admission. This is the BUG-3/BUG-5 remediation.
4. **BUG-4 decision**: replacement approval surface for message suggestions (deep-context suggestions review or conservative auto-attach).
5. Proposed-not-built: gmail apply step reads review.csv and skips SOT-rejected links; drop vestigial Powerset Step-0 gates from both import skills; archive dead legacy dirs.
6. ~~Relic-skill cluster~~ RESOLVED batch 13 — all 8 deleted. Remaining fork in that thread: `setup/{onboarding,setup,setup_gmail,setup_linkedin_csv}.py` survive as the console app's onboarding engine only; killing them means ripping out the app onboarding flow (jobs.ts/commands.ts/routes/onboarding+setup.ts + app/src UI) — Arthur's call.
7. `gmail_network_import` (1,662 LOC, now `gmail/network_import.py`) deserves its own audit pass.
8. Fold `HYGIENE.md` + this file into AGENTS.md / delete when the audit closes.
9. Coordinate with PR #314 (other session: evidence-fingerprint refresh; touches collect/synthesize/sources).
```
