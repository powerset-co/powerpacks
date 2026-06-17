# 🗺️ Pipeline File Registry & DAG — LinkedIn & Gmail

> Created: 2026-06-16
>
> **Purpose:** make the pipeline deterministic by giving **every input and every
> output exactly one canonical location constant and one attached schema**. A
> stage never receives a hand-built `--path`; it writes to a registry location
> and asserts the registry schema at every read boundary. This kills the
> recurring bug class — *one resource, two string representations, picked wrong*
> (double-nested dirs, `/data` vs volume-relative keys, discovery-CSV vs
> people-CSV, missing-vs-blank coercion). Everything below is derived from code
> (`file:line`), not memory.
>
> **Changelog**
> - 2026-06-16: initial inventory (Gmail + LinkedIn + Modal handoff + risk register).
> - 2026-06-16: restructured to a **File Registry** — every IO now carries a
>   location constant **and** a schema constant, with `exists / 🆕 to-create`
>   status; added §6 spec for a single `pipeline_files.py` registry module.

---

## 0. 📒 File Registry — every input/output = (location constant, schema constant)

Legend: ✅ constant exists · ⚠️ exists but duplicated / stage-scoped · 🆕 must be created.
"Location" = canonical path/key. "Schema" = expected CSV header (or kind for non-CSV).

### Gmail flow

| Artifact | Location constant | Status | Schema constant | Status |
|---|---|---|---|---|
| accounts.json | `DEFAULT_ACCOUNTS` (`import_contacts_pipeline/common.py:27`) | ⚠️ dup in `index_contacts_pipeline.py:30` | _(json)_ | — |
| msgvault.db | `DEFAULT_MSGVAULT_DB` (`discover_contacts_pipeline/common.py:24`) | ✅ | _(sqlite)_ | — |
| discover/gmail/**contacts.csv** | `DISCOVER_CONTACTS_CSV` (`setup_gmail.py:48`) | ⚠️ defined only in setup_gmail; discover/import re-derive | `GMAIL_DISCOVERY_COLUMNS` (`discover_contacts_pipeline/gmail.py:71`) | ✅ |
| discover/gmail/**linkedin_resolution_queue.csv** | 🆕 `GMAIL_DISCOVER_QUEUE_CSV` | 🆕 | `LINKEDIN_RESOLUTION_QUEUE_COLUMNS` (`gmail_network_import.py:183`) | ✅ |
| discover/gmail/**manifest.json** | 🆕 `GMAIL_DISCOVER_MANIFEST` | 🆕 | _(manifest)_ | — |
| discover/gmail/**`<acct>`/** (per-account dir) | 🆕 `gmail_account_dir(email)` | 🆕 *(the double-nest origin)* | — | — |
| discover/gmail/`<acct>`/**people.csv** | 🆕 `gmail_account_people_csv(email)` | 🆕 | `PEOPLE_SCHEMA_COLUMNS` (`people_schema.py:19`) | ✅ |
| import/gmail/**people.csv** | 🆕 `GMAIL_IMPORT_PEOPLE_CSV` (= `DEFAULT_IMPORT_DIR/"gmail"/"people.csv"`) | 🆕 | `PEOPLE_SCHEMA_COLUMNS` | ✅ |
| **directory.csv** | `DEFAULT_DIRECTORY_CSV` (`discover_contacts_pipeline/common.py:23`) | ✅ | `DIRECTORY_COLUMNS` (`directory.py:44`) | ✅ |

### LinkedIn flow

| Artifact | Location constant | Status | Schema constant | Status |
|---|---|---|---|---|
| Connections.csv (staged) | `DISCOVER_CONNECTIONS_CSV` (`setup_linkedin_csv.py:53`) | ⚠️ setup-scoped | `LINKEDIN_DISCOVERY_COLUMNS` (`discover_contacts_pipeline/linkedin.py:57`) | ✅ |
| profile_cache_v2/ | `DEFAULT_PROFILE_CACHE_DIR` (`import_contacts_pipeline/common.py:29`) | ⚠️ re-built as string `setup_linkedin_csv.py:235` | _(profile cache)_ | — |
| import/linkedin/**people.csv** | 🆕 `LINKEDIN_IMPORT_PEOPLE_CSV` | 🆕 | `PEOPLE_SCHEMA_COLUMNS` | ✅ |
| search-index/**local-search.duckdb** | 🆕 `LOCAL_SEARCH_DUCKDB` | 🆕 *(hand-built `output_dir/"local-search.duckdb"` ~5×)* | _(duckdb)_ | — |

### Shared join point

| Artifact | Location constant | Status | Schema constant | Status |
|---|---|---|---|---|
| **merged/people.csv** | `DEFAULT_PEOPLE_CSV` (`index_contacts_pipeline.py:31`) — also `DEFAULT_OUTPUT_DIR/"people.csv"` (`merge:51`) | ⚠️ two names for one file | `MERGED_COLUMNS` (`merge_network_sources.py:52`) | ✅ |

### Modal handoff (each location has TWO views — write key vs read path)

| Artifact | Write **key** constant | Read **path** constant | Status | Schema |
|---|---|---|---|---|
| operator root | — | `OPERATOR_ROOT` (`linkedin_modal_pipeline.py:117`) | ✅ | — |
| operator prefix | 🆕 `OPERATOR_VOLUME_PREFIX` | `OPERATOR_ROOT` | 🆕 *(`op_prefix` hand-built `:421`; fn `:130`)* | — |
| input/people.csv | 🆕 `OP_INPUT_PEOPLE_KEY` | 🆕 `OP_INPUT_PEOPLE_PATH` | 🆕 | `MERGED_COLUMNS`/`PEOPLE_SCHEMA_COLUMNS` |
| input/connections.csv | 🆕 `OP_INPUT_CONNECTIONS_KEY` | 🆕 `OP_INPUT_CONNECTIONS_PATH` | 🆕 | `LINKEDIN_DISCOVERY_COLUMNS` |
| runs/`<label>`/local-search.duckdb | 🆕 `op_run_duckdb_key(label)` | `run_vol_path(label)` (`:127`) | 🆕 key | _(duckdb)_ |

> **Reading the status column:** every 🆕 and ⚠️ row is a place the pipeline today
> re-derives a path or schema by hand — i.e. a spot the next wrong-path bug can
> enter. The schemas mostly already exist; the **bindings** (location↔schema, one
> owner) do not.

---

## 1. 🟦 Gmail flow (DAG) — references registry names from §0

`accounts.json` + `msgvault.db`
→ **discover** (`discover_contacts_pipeline/gmail.py`) writes `gmail_account_people_csv(email)` (schema `PEOPLE_SCHEMA_COLUMNS`, carries `interaction_counts`) + aggregate `DISCOVER_CONTACTS_CSV`/queue/manifest
→ **import** (`import_contacts_pipeline/gmail.py`, `gmail_artifacts_from_discovery` validates `interaction_counts` at the seam) writes `GMAIL_IMPORT_PEOPLE_CSV`; appends `DEFAULT_DIRECTORY_CSV`
→ **merge** (`merge_network_sources.py`) writes `DEFAULT_PEOPLE_CSV` (schema `MERGED_COLUMNS`)
→ **modal index-people** uploads `OP_INPUT_PEOPLE_KEY`, sandbox reads `OP_INPUT_PEOPLE_PATH`, run writes `op_run_duckdb_key("gmail-index")`.

> per-account child: `gmail_network_import.py msgvault` is passed `--output-dir = DEFAULT_BASE_DIR` (the **base**) and internally appends `discover/gmail/<slug>` via `gmail_discover_dir()` (`:440,1511`). `gmail_account_dir(email)` must be the single owner of that final path so no caller ever appends it again.

## 2. 🟩 LinkedIn flow (DAG)

`Connections.csv` (`DISCOVER_CONNECTIONS_CSV`, schema `LINKEDIN_DISCOVERY_COLUMNS`)
→ **import/enrich** (RapidAPI 💸, cache `DEFAULT_PROFILE_CACHE_DIR`) writes `LINKEDIN_IMPORT_PEOPLE_CSV` (schema `PEOPLE_SCHEMA_COLUMNS`)
→ **merge** writes `DEFAULT_PEOPLE_CSV`
→ **index** (local `index_contacts_pipeline.py` → `LOCAL_SEARCH_DUCKDB`, or modal `index-people`/`pipeline`).

> ⚠️ `merge_network_sources.keep_people_csv_row()` (`:209,553`) drops rows lacking a stable LinkedIn key **and** a usable RapidAPI profile — silent attrition into `merged/people.csv`.

---

## 3. 🔗 Shared join point

Both flows converge on **`DEFAULT_PEOPLE_CSV` = `.powerpacks/network-import/merged/people.csv`** (schema `MERGED_COLUMNS`), produced by `merge_network_sources`, consumed by every indexer. Upstream = per-source; downstream = source-agnostic.

---

## 4. ☁️ Local → Modal handoff (the `/data` vs volume-key trap)

The Modal Volume is **written** with volume-relative keys and **read** in the sandbox at the `/data` mount — same location, two strings. Writing the mount-view string as a key produces a phantom `data/operators/...` key the sandbox never reads (the "indexed the stale 277-row file" bug). Each row of the Modal table in §0 therefore has **two** constants (`*_KEY` and `*_PATH`); a `volume_key(path)` helper must be the only way to derive one from the other.

`run_indexing.py` feeds the **same** sandbox `people.csv` to both `build_processing_pipeline.py --input` and `build-local-duckdb-shim.py --person-profiles-csv` (`run_indexing.py:194-202`), so the duckdb's `interaction_counts`/`total_interactions` come from that one registry file.

---

## 5. 🚩 Risk register — the four "two representations of one thing" classes

| Class | Where it lives | Symptom when wrong | Status |
|---|---|---|---|
| **base-dir vs final-dir** | `gmail_discover_dir()` appends `discover/gmail/<slug>` onto `--output-dir`; caller must pass the **base** | `…/discover/gmail/raw/<acct>/discover/gmail/<acct>/` double-nest | ✅ fixed; permanent fix = `gmail_account_dir(email)` owns the final path |
| **mount-view vs key-view** | modal writes `operators/<id>/…`, reads `/data/operators/<id>/…` | upload lands at `data/operators/…`, sandbox reads stale file | ✅ fixed `:552`; ⚠️ `:421` still hand-builds `op_prefix` → promote to `OPERATOR_VOLUME_PREFIX` + `volume_key()` |
| **discovery-CSV vs people-CSV** | `contacts.csv` (`GMAIL_DISCOVERY_COLUMNS`) vs `people.csv` (`PEOPLE_SCHEMA_COLUMNS`) | counts column present but all 0 | ✅ guard added; permanent fix = `require_schema()` at the seam |
| **missing vs blank** | `normalize_people_row` defaults every column to `""` (`people_schema.py:114`) | wrong-shape input laundered into valid-but-empty output | ⚠️ assert-at-boundary (`require_schema`) instead of coerce |

---

## 6. 🎯 The fix: one `pipeline_files.py` registry (location ⊗ schema)

A single module binds every artifact in §0 to its location **and** schema, so a stage references a registry entry instead of a string, and asserts the schema on read.

```python
# packs/ingestion/schemas/pipeline_files.py
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class FileSpec:
    location: Path | str             # canonical path OR volume key (one owner)
    schema: tuple[str, ...] | None   # expected CSV header; None for non-CSV
    produced_by: str
    consumed_by: tuple[str, ...]

# every §0 row becomes one entry, reusing the EXISTING schema constants:
GMAIL_DISCOVER_CONTACTS = FileSpec(DISCOVER_DIR_GMAIL / "contacts.csv",
    GMAIL_DISCOVERY_COLUMNS, "discover/gmail.py", ("import/gmail.py",))
GMAIL_IMPORT_PEOPLE     = FileSpec(DEFAULT_IMPORT_DIR / "gmail" / "people.csv",
    PEOPLE_SCHEMA_COLUMNS, "import/gmail.py", ("merge_network_sources.py",))
MERGED_PEOPLE           = FileSpec(DEFAULT_OUTPUT_DIR / "people.csv",
    MERGED_COLUMNS, "merge_network_sources.py", ("index", "modal"))
def gmail_account_dir(email):  ...            # single owner of the per-account dir
def volume_key(path: str) -> str:             # mount-view  -> key-view, the ONLY converter
    return str(path).removeprefix("/data/").lstrip("/")

class PipelineSchemaError(Exception): ...
def require_schema(spec: FileSpec, csv: Path) -> None:
    header = read_csv_header(csv)
    missing = [c for c in (spec.schema or ()) if c not in header]
    if missing:
        raise PipelineSchemaError(f"{csv} missing {missing} for {spec.produced_by}")
```

Determinism contract:
- **Locations:** every 🆕/⚠️ row in §0 gets exactly one constant here; primitives import it and pass **nothing** (CLI `--path` args become test-only overrides).
- **Schemas:** each entry reuses the existing column constant (`PEOPLE_SCHEMA_COLUMNS`, `MERGED_COLUMNS`, `GMAIL_DISCOVERY_COLUMNS`, …) — no new schema definitions, just bindings.
- **Boundaries:** every stage calls `require_schema(spec, csv)` on read → wrong-shape input fails **loud** instead of being coerced to blank.
- **Modal:** `volume_key()` is the single mount→key converter; no hand-built `/data/...` or `operators/...` strings anywhere (retires `op_prefix` at `:421`).

> Current state: the three fixes (gmail seam, path nest, modal upload key) are
> committed on `fix/pipeline-path-dag` (PR #83). This registry module is the
> follow-up that makes the pattern enforced, not just patched.
