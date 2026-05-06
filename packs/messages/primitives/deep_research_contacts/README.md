# deep_research_contacts

Deep-research the unmatched-but-named contacts via Parallel.ai. Stdlib-only.

Native port of
`aleph-mvp/data_pipeline_v2/pipelines/synthetic/research_parallel.py`,
re-implemented against the Parallel HTTP API directly so Powerpacks does not
depend on the `parallel` SDK or `pydantic`.

Reads the input CSV produced by `prepare_research_queue` and writes
per-handle JSON artifacts in the same shape `research_parallel.py` produces,
so any downstream code that already consumes `01_research_parallel.json`
keeps working.

This is the native Powerpacks primitive for the old `network-search-api`
synthetic deep-research path. The queue builder applies the same
`looks_like_real_name` rule and default min-message prune before this primitive
spends Parallel credits.

## Privacy contract

The primitive only sends the explicit input shape:

- `handle`, `display_name`, `bio` (empty for phone)
- `known_info` (built from email/domain/website/follower/phone/area-code/
  message-source/last-message-timestamp/group flags)
- `source_channel`, `phone_number`, `area_code`

It does not read or send message content. Inputs are already filtered by
`llm_review_contacts` and `prepare_research_queue`.

## Usage

```bash
# 0. Download existing operator-scoped research cache first (no Parallel spend).
python packs/messages/primitives/sync_messages_research_cache/sync_messages_research_cache.py download

# 1. Cost estimate, no Parallel network/spend; skips already-downloaded handles.
python packs/messages/primitives/deep_research_contacts/deep_research_contacts.py estimate \
  --input .powerpacks/messages/research_queue.p1p2.csv \
  --processor core2x

# 2. Run the whole pipeline (submit + poll) — needs PARALLEL_API_KEY.
python packs/messages/primitives/deep_research_contacts/deep_research_contacts.py run \
  --input .powerpacks/messages/research_queue.p1p2.csv \
  --processor core2x \
  --output-dir .powerpacks/messages/research

# 3. Or split into submit + poll for long batches:
python ... deep_research_contacts.py submit --input ... --processor core2x
python ... deep_research_contacts.py status --output-dir .powerpacks/messages/research
python ... deep_research_contacts.py poll --output-dir .powerpacks/messages/research
```

## Subcommands

| Command | What it does | Network? |
| --- | --- | --- |
| `estimate` | Print queue size + per-processor cost estimate | No |
| `submit` | Create Parallel task group, submit all eligible runs, persist state | Yes |
| `status` | One-shot status check on the persisted/explicit task group | Yes |
| `poll` | Wait for task group to finish, fetch each run's result, write artifacts | Yes |
| `run` | `submit` + `poll` (the common case) | Yes |

## Idempotency

Rows where `<output-dir>/<handle>/01_research_parallel.json` already exists
are skipped on `estimate` / `submit` / `run`. Run
`sync_messages_research_cache download` first to hydrate this directory from the
server-side operator cache and avoid paying Parallel again for prior work. That
makes it safe to re-run after partial failures or to incrementally add new
candidates.

## Artifacts

```
<output-dir>/
├── <handle>/
│   ├── 00_parallel_raw.json        Raw Parallel `output.content`
│   └── 01_research_parallel.json   Transformed shape compatible with aleph-mvp
├── _taskgroup.json                 Persisted state (taskgroup_id, run_ids, rows)
└── _manifest.json                  Final summary (counts, errors, group status)
```

## Pricing (per task)

| Processor | $ / task |
| --- | --- |
| `core2x` | $0.05 |
| `pro` | $0.10 |
| `ultra8x` | $2.40 |

`estimate` reports the cost for the current queue at the chosen processor.

## Environment overrides

| Variable | Default |
| --- | --- |
| `PARALLEL_API_KEY` | _required_ for `submit` / `poll` / `run` / `status` |
| `POWERPACKS_PARALLEL_BASE_URL` | `https://api.parallel.ai` |
| `POWERPACKS_PARALLEL_BETA` | `search-extract-2025-10-10` |
| `POWERPACKS_PARALLEL_PROCESSOR` | `core2x` |
