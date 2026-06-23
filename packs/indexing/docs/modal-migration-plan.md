# 🚀 Modal migration plan: indexing/processing pipeline

**Created:** 2026-06-11

**Changelog:**
- 2026-06-11: Initial plan + PoC scope.
- 2026-06-11: PoC executed + benchmark results + streaming memory refactor (see Results).

## ✅ Results (PoC executed 2026-06-11)

All runs at Jake scale (6,500 people / 38,258 unique roles / 29,436 companies /
50,817 position records), 4 CPU Modal sandbox, **zero paid API calls** (no
OPENAI_API_KEY in the sandbox; every paid stage artifact-covered):

| variant | pipeline wall | pipeline peak RSS | duckdb wall / peak |
|---|---|---|---|
| baseline (main) | 667s | **17.7GB** (build_people_records) | 270s / 7.0GB |
| + trim unused lookup fields | 757s | 15.8GB | 212s / 7.1GB |
| + streaming refactor (16GB box) | 920s | **3.6GB** | 284s / 6.2GB |
| + streaming refactor (6GB box) | 677s | **3.6GB** | 355s / 6.0GB |

Pipeline wall-time delta vs baseline is within host noise (677s vs 667s on the
final run). DuckDB shim now sets `SET memory_limit` (default 2GB, override via
`POWERPACKS_DUCKDB_MEMORY_LIMIT`) and `SET preserve_insertion_order=false`;
the 6.0GB shim peak above was measured before `preserve_insertion_order` was
added — not yet re-measured at scale.

Per-step peaks (baseline) that motivated the refactor: build_people_records
17.7GB, embed_role_positions 9.5GB, duckdb shim 7.0GB, embed_companies 6.7GB,
build_vectors/validate 4.7GB, flatten 3.2GB.

Streaming refactor (verified bit-identical people/companies record hashes vs
baseline at 1x; full unittest suite shows only the pre-existing main failures):
- `step_people`: person-by-person streaming join + incremental record write;
  resident state is only `{title_hash → array('d') vector}` (~470MB @ 38k
  roles) and vector-stripped company metadata.
- `embed_records_checkpointed`: streams input rows, holds input embeddings as
  compact float64 arrays, finalize merges chunks without boxed-float blowup.
- `write_record_jsonl_stream_with_hashes`: per-row hash/diff + temp-file
  streaming write (no full record lists, no giant join-string writes).
- `validate_contracts` / `build_vectors` / role+company embedding shaping all
  iterate instead of materializing.
- DuckDB shim: `SET memory_limit` (default 2GB, env-overridable).

**Instance recommendation: 4 CPU / 16GB** (~$0.40 per full run at ~20 min).
The streamed pipeline peaks at 3.6GB and the DuckDB build at ~6GB, so 16GB is
comfortably safe headroom (8GB would work; the cost difference is ~$0.10/run).
M1 16GB laptops can also run the streamed pipeline again locally.

⚠️ Environment parity: the Modal image must pin Python deps to `uv.lock`
versions — `snowballstemmer` 3.1.1 vs the locked 3.0.1 changed stemming and
therefore `phrase_tokens` + record hashes on 19/500 summaries before pinning.

---

## Why

Operators on M1 MacBooks (16GB) are struggling to run the local search-index
processing pipeline at real network scale (Jake: 6,163 people, 39,361 unique
roles, 28,800 companies). The run on Jake's machine died mid-`build_people_records`
and left a stale mixed-state output dir. Not everyone can (or should) run
enrichment locally.

**Target mental model:** the local machine becomes a thin
dispatcher/aggregator — it uploads `people.csv` (plus any cached artifacts),
dispatches the entire processing/indexing run to a Modal sandbox, and downloads
the finished `.powerpacks/search-index/` artifacts (records JSONL +
`local-search.duckdb`). Per-vertical processing can move to additional
sandboxes later using the same pattern.

## What the pipeline actually is (investigation summary)

- Entry: `packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py {plan|run|continue|status} --input people.csv --output-dir .powerpacks/search-index`
  (wrapped by `index_contacts_pipeline.py` which handles operator fan-in).
- 15 sequential steps tracked in `ledger.json` (resumable, per-step
  checkpoints). Output contract = `records/*.records.jsonl` validated against
  `packs/search/contracts/turbopuffer/*.namespace.json`, then materialized to
  `local-search.duckdb` by `scripts/build-local-duckdb-shim.py`.
- **Paid steps** (OpenAI, network-bound): `build_roles` (1 chat call per unique
  `title_hash`), `embed_role_positions`, `embed_companies`, `embed_summaries`
  (text-embedding-3-small), `detect_ceo_founders`, `infer_ages`. All support
  precomputed-artifact bypass (`--role-input-classifications`,
  `--role-input-embeddings`, `--company-input-classifications`,
  `--company-input-embeddings`, `--summary-input-embeddings`) or
  checkpoint-resume keyed by stable ids, so a fully-cached run makes **0 paid
  calls**.
- **Pure-compute steps** (CPU/memory-bound, where M1s hurt): `flatten_people`,
  corpus builds, `build_people_records` (joins ~40k role embeddings × 1536
  floats in memory), `build_unified_profiles`, `build_summary_records`,
  `build_vectors`, `validate_contracts`, plus the DuckDB materialization.
- Everything is single-process Python + asyncio; no multiprocessing. Memory,
  not core count, is the binding resource.
- Python 3.12; deps needed in the sandbox: `duckdb`, `openai`,
  `python-dotenv`, `pyyaml`, `snowballstemmer`, `tiktoken`. (`turbopuffer`
  and `psycopg2` are prod-only, not needed.)
- Hardcoded `.powerpacks/` defaults exist but `--input`/`--output-dir` are
  explicit args, so the cloud run just passes absolute sandbox paths.

## Architecture (PoC → production)

```
local (aggregator)                      Modal (powerset-co workspace)
┌──────────────────────┐                ┌────────────────────────────────┐
│ people.csv (merged)  │── volume put ─▶│ Volume: powerpacks-indexing    │
│ cached artifacts     │                │   /data/input/...              │
│                      │                │                                │
│ driver script        │── Sandbox ────▶│ Sandbox (py3.12 image + repo)  │
│  (dispatch + poll)   │                │   build_processing_pipeline run│
│                      │                │   build-local-duckdb-shim      │
│ .powerpacks/         │◀─ volume get ──│   /data/output/search-index/   │
│   search-index/      │                └────────────────────────────────┘
└──────────────────────┘
```

- **Modal Volume** (`powerpacks-indexing-poc`) carries inputs and outputs —
  artifacts are ~200MB at single-operator scale and multi-GB at multi-operator scale, so no
  inline image baking for data.
- **Sandbox** runs the unmodified repo CLI (`exec`), so the ledger/checkpoint
  machinery keeps working; an interrupted cloud run resumes with `continue`
  exactly like a local one.
- **Secrets:** `OPENAI_API_KEY` becomes a Modal Secret for real paid runs.
  The PoC runs with no key at all (cache-covered).

## PoC scope (this branch)

1. **Plumbing proof at 1x (sample operator data, $0):** upload
   `people.csv` + the existing precomputed role/company/summary artifacts,
   pre-seed `founder_enrichment.jsonl` / `inferred_ages.jsonl` (their steps
   checkpoint on the output file and skip covered ids), run the full 15-step
   pipeline + DuckDB build in a sandbox, verify `validate_contracts` passes
   and `manifest.json` is written, download stats.
2. **Instance sizing at Jake scale (~6.2k people / 39k roles / 29k
   companies, $0):** an in-sandbox amplifier clones real rows into a
   synthetic `people.csv` at target scale and emits matching synthetic
   precomputed artifacts (keys recomputed via the repo's own
   `flatten_people`, vectors cloned from real embeddings). Run the full
   pipeline across instance configs (e.g. 2cpu/4GB, 4cpu/8GB, 8cpu/16GB),
   record wall time per step (from ledger timestamps) and peak RSS (RSS
   poller + `getrusage`), recommend the cheapest config with headroom.

### Measurement harness

- `bench_wrapper.py` runs inside the sandbox: spawns the pipeline as a child,
  polls `/proc/<pid>/status` VmRSS each second, records
  `ru_maxrss`, emits a JSON report; per-step durations come from
  `ledger.json` step timestamps.

### Explicitly out of scope for the PoC

- Paid OpenAI calls of any kind (everything is artifact/cache covered).
- Per-vertical sandboxes, scheduling, multi-operator dispatch.
- Changing any pipeline code paths — the PoC runs the repo CLI as-is.

## Production follow-ups (after PoC)

- `index_contacts_pipeline.py --remote modal` style dispatch wrapper (local
  fan-in stays local; processing goes remote; download merges back).
- Modal Secret for `OPENAI_API_KEY`; tier env (`POWERPACKS_OPENAI_USAGE_TIER`)
  set per workspace.
- Resume story: ledger lives on the Volume, so `continue` works across
  sandbox restarts — exactly what Jake needed when his local run died.
- Per-vertical sandboxes reusing the same image + volume layout.

## Cost note 💸

Modal compute for the benchmark: minutes of CPU-only containers
(~$0.000038/cpu-s + ~$0.00000667/GiB-s ≈ pennies per run; <$1 for the whole
matrix). No OpenAI spend.
