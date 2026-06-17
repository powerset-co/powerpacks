# 🗺️ Pipeline File DAG — LinkedIn & Gmail

> Created: 2026-06-16
>
> **Purpose:** single source of truth for every input/output file in the LinkedIn
> and Gmail ingestion → merge → index flows, which script produces it, and which
> consumes it. Built to kill the recurring bug class: *the same logical resource
> has two string representations and a hand-passed `--path` arg picks the wrong
> one* (double-nested dirs, `/data` vs volume-relative keys, discovery-CSV vs
> people-CSV). Paths here are derived from code (`file:line` cited inline in the
> session that produced this), not memory.
>
> **Changelog**
> - 2026-06-16: initial inventory (Gmail + LinkedIn + Modal handoff + risk register).

---

## 0. 📌 Canonical path constants (the only values that should ever be hard-coded)

| Constant | Defined in | Resolved value |
|---|---|---|
| `DEFAULT_BASE_DIR` | `discover_contacts_pipeline/common.py:19` | `.powerpacks/network-import` |
| `DISCOVER_DIR` | `discover_contacts_pipeline/common.py:20` | `.powerpacks/network-import/discover` |
| `DEFAULT_DIRECTORY_CSV` | `discover_contacts_pipeline/common.py:23` | `.powerpacks/network-import/directory.csv` |
| `DEFAULT_IMPORT_DIR` | `import_contacts_pipeline/common.py:28` | `.powerpacks/network-import/import` |
| `DEFAULT_OUTPUT_DIR` (merge) | `merge_network_sources.py:51` | `.powerpacks/network-import/merged` |
| `DEFAULT_PEOPLE_CSV` (index) | `index_contacts_pipeline.py:31` | `.powerpacks/network-import/merged/people.csv` |
| `OPERATOR_ROOT` (modal, **mount view**) | `linkedin_modal_pipeline.py:115` | `/data/operators/<operator-id>` |
| `op_prefix` / `operator_volume_prefix()` (modal, **key view**) | `linkedin_modal_pipeline.py:130,421` | `operators/<operator-id>` |

**Rule of thumb:** everything under `.powerpacks/network-import/` is keyed off
`DEFAULT_BASE_DIR`. The Modal volume has TWO views of the same location — see §4.

---

## 1. 🟦 Gmail flow (DAG)

| # | Script : subcommand | Inputs (path — source) | Outputs (path — source) |
|---|---|---|---|
| 1 | `discover_contacts_pipeline/gmail.py : discover` | `accounts.json` (`.powerpacks/ingestion/accounts.json` — `--accounts`)<br>`msgvault.db` (`~/.msgvault/msgvault.db` — `--msgvault-db`/state) | **aggregate** `discover/gmail/{contacts.csv, linkedin_resolution_queue.csv, manifest.json}` (hard-coded) |
| 1a | ↳ per-account child `gmail_network_import.py : msgvault` | `--db`, `--account-email`, `--output-dir = DEFAULT_BASE_DIR` (the **base**, not the final dir) | `discover/gmail/<acct-slug>/{people.csv, linkedin_resolution_queue.csv, manifest.json, accounts.csv, gmail_threads.csv, …}` — **callee appends `discover/gmail/<slug>`** via `gmail_discover_dir()` (`gmail_network_import.py:440,1511`). `people.csv` carries `interaction_counts={"gmail": N}` (`:875`) |
| 2 | `import_contacts_pipeline/gmail.py : run` | `discover/gmail/manifest.json` (hard-coded `:93`) → `gmail_artifacts_from_discovery()` selects **per-account `people.csv`** from `children[].artifacts.people_csv`, **validated** for `interaction_counts` (`:89,112`) | `import/gmail/{people.csv, manifest.json, ledger.json}`<br>appends `directory.csv` |
| 3 | `merge_network_sources.py : run` | `--input` × N: `import/gmail/people.csv`, `import/linkedin/people.csv`, messages contacts, … | **`merged/people.csv`** (canonical) + `network_contacts.csv`, `network_companies.csv`, `merge_manifest.json` |
| 4 | `linkedin_modal_pipeline.py : index-people` | `--people-csv = merged/people.csv` | uploads → volume key `operators/<id>/input/people.csv` (`:552`, via `operator_volume_prefix()`); run → `operators/<id>/runs/gmail-index/{local-search.duckdb, manifest.json}` → download local |

**Gmail edges:**
`accounts.json` + `msgvault.db` → **discover** → `discover/gmail/<slug>/people.csv` (+ aggregate `contacts.csv`/queue) → **import** (`gmail_artifacts_from_discovery`) → `import/gmail/people.csv` → **merge** → `merged/people.csv` → **modal index-people** → `runs/gmail-index/local-search.duckdb`.

---

## 2. 🟩 LinkedIn flow (DAG)

| # | Script : subcommand | Inputs | Outputs |
|---|---|---|---|
| 1 | `setup_linkedin_csv.py : run` | `Connections.csv` (`--csv`), `--source-user`, `accounts.json` | stages `discover/linkedin/Connections.csv`; status under `.powerpacks/runs/setup-linkedin-csv/` |
| 2 | `linkedin_network_import.py` | `--csv`, `--output-dir = DEFAULT_BASE_DIR`, ledger `discover/linkedin/linkedin_network_import.ledger.json` | `connections_for_enrichment.csv`, `source_people.csv` (run dir) |
| 3 | `enrich_people.py` (RapidAPI 💸) | `source_people.csv` (from ledger), profile cache `DEFAULT_BASE_DIR/profile_cache_v2` | `people.csv` (run dir) + `rapidapi_cache_{hits,misses}.csv`, queues |
| 4 | `import_contacts_pipeline/linkedin.py : run` | `accounts.json` → `linkedin_csv_path()`, enriched `people.csv` | `import/linkedin/{people.csv, manifest.json}`; appends `directory.csv` |
| 5 | `merge_network_sources.py : run` | `--input import/linkedin/people.csv` (+ other sources) | **`merged/people.csv`** ⟵ *shared with Gmail* |
| 6a | `index_contacts_pipeline.py` (local) | `DEFAULT_PEOPLE_CSV = merged/people.csv` | `.powerpacks/search-index/local-search.duckdb` |
| 6b | `linkedin_modal_pipeline.py : pipeline` (cloud) | `--csv Connections.csv` | upload `operators/<id>/input/connections.csv` → `run_linkedin.py` → `operators/<id>/input/people.csv` → `run_indexing.py` → `runs/linkedin-index/local-search.duckdb` |

**LinkedIn edges:**
`Connections.csv` → **import/enrich** → `import/linkedin/people.csv` → **merge** → `merged/people.csv` → **index** (local `index_contacts_pipeline` *or* modal `index-people`/`pipeline`) → `local-search.duckdb`.

> ⚠️ `merge_network_sources` filters rows via `keep_people_csv_row()` (`:209,553`):
> a row needs a stable LinkedIn key **and** a usable RapidAPI profile to reach
> `merged/people.csv`. Incomplete enrichment silently drops rows here.

---

## 3. 🔗 Shared join point

Both flows converge on **`.powerpacks/network-import/merged/people.csv`** (produced by `merge_network_sources`, consumed by every indexer). This is the one file the Modal index is *supposed* to consume. Everything upstream is per-source; everything downstream is source-agnostic.

---

## 4. ☁️ Local → Modal handoff (the `/data` vs volume-key trap)

The Modal Volume is **written** with volume-relative keys and **read** (inside the sandbox) at the `/data` mount. Same location, two strings. Writing the mount-view string as a key produces a phantom `data/operators/...` key the sandbox never reads — this was the "indexed the stale 277-row file" bug.

| Local file | Volume **write** key | Sandbox **read** path |
|---|---|---|
| `merged/people.csv` | `operators/<id>/input/people.csv` | `/data/operators/<id>/input/people.csv` |
| `Connections.csv` | `operators/<id>/input/connections.csv` | `/data/operators/<id>/input/connections.csv` |
| `search-index/*` artifacts | `cache/artifacts/*`, `cache/seeds/*` | `/data/cache/artifacts/*` |
| (run output) | `operators/<id>/runs/<label>/local-search.duckdb` | `/data/operators/<id>/runs/<label>/local-search.duckdb` |

`run_indexing.py` then feeds the **same** sandbox `people.csv` to both
`build_processing_pipeline.py --input` and
`build-local-duckdb-shim.py --person-profiles-csv` (`run_indexing.py:194-202`),
so the duckdb's `interaction_counts`/`total_interactions` come from that one file.

---

## 5. 🚩 Risk register — the four "two representations of one thing" classes

| Class | Where it lives | Symptom when wrong | Status |
|---|---|---|---|
| **base-dir vs final-dir** | `gmail_discover_dir()` appends `discover/gmail/<slug>` onto `--output-dir`; caller must pass the **base** | `…/discover/gmail/raw/<acct>/discover/gmail/<acct>/` double-nest | ✅ fixed (working tree) |
| **mount-view vs key-view** | modal writes must use `operators/<id>/…`, reads use `/data/operators/<id>/…` | upload lands at `data/operators/…`, sandbox reads stale file | ✅ fixed `:552`; ⚠️ `:421` still hand-builds `op_prefix` (correct value, but not via the helper) |
| **discovery-CSV vs people-CSV** | `contacts.csv` (has `total_messages`, no `interaction_counts`) vs `people.csv` (people schema) | counts column present but all 0 | ✅ guard added (`_valid_gmail_people_csv`) |
| **missing vs blank** | `normalize_people_row` defaults every column to `""` (`people_schema.py:114`) | wrong-shape input laundered into valid-but-empty output | ⚠️ only `messages.py` has a staleness guard; gmail path now rejects at the seam |

---

## 6. 🎯 Direction (toward hard-coded, un-confusable paths)

The fix for the whole class is to stop passing ambiguous strings and give each
representation **one owner + one assertion**:

1. **A single `paths` module** that defines every canonical path in §0 once and
   exposes typed helpers instead of raw strings:
   - `gmail_account_dir(email)` → the one true per-account dir (no caller ever
     appends `discover/gmail/<slug>` by hand).
   - `volume_key(p)` vs `sandbox_path(p)` → impossible to write a `/data` string
     as a volume key (generalize `operator_volume_prefix()`; retire the hand-built
     `op_prefix` at `:421`).
   - `merged_people_csv()` → the join point in §3.
2. **Primitives default to these constants**; keep `--arg` overrides only for
   tests, never as the normal call path. (This is the "no one actually uses these
   as real CLIs" point — the agent is the only caller, so the default should be
   the canonical path and the agent should pass nothing.)
3. **Assert-at-boundary, don't coerce:** `require_people_schema(csv)` raises on a
   discovery-shape CSV at every seam that expects people schema.

> Current state: the three fixes (gmail seam, path double-nest, modal upload key)
> are present in the working tree, **uncommitted**. This doc is step 1 (the
> inventory); the `paths` module + hard-coding is the follow-up that prevents
> regression.
