# llm_rerank_candidates

Async fan-out LLM rerank over a JSONL of candidates or a Powerpacks task-state. Stdlib only.

Same shape as the production `SEARCH_V2_RERANK_MAX_CONCURRENT=400` path
in network-search-api, but Powerpacks-local. Useful for:

- default LLM reranking inside a `search-network` task after hydration/filtering
- standalone reranking outside a `search-network` task
- load testing your `OPENAI_API_KEY` rate limit
- quickly verifying a query + traits produce sensible verdicts on a
  small synthetic candidate set
- driving message-pack contact reviews, deal scoring, or any other
  per-item LLM-judgment workload

Does **not** require `set_id`. It can either read JSONL (`--in`) or read a
Powerpacks task-state (`--state`) after `hydrate_people` has run.

## Inputs

- `--in PATH | -` — JSONL of candidates. Each line is a JSON object.
  Object shape is freeform; if it has an `id` / `person_id` /
  `member_id` / `candidate_id` field, that becomes the result id.
- `--state PATH` — Powerpacks task state. Reads hydrated profiles from
  `hydrate_people.output.llm_profiles_path` / `profiles_path`, uses
  `llm_filter_candidates.output.passed_candidate_ids` when present, and writes
  `query_results.csv` under the run's artifact directory.
- `--query STRING` — the search query (prompt context; defaults to `state.query`
  in `--state` mode)
- `--traits TRAIT` — expected traits (repeatable; pass once per trait)
- `--concurrency N` — `asyncio.Semaphore` size (default 50; env
  `LLM_RERANK_CONCURRENCY`)
- `--model NAME` — chat completion model (default `gpt-4o-mini`)
- `--api-base URL` — base URL (default `https://api.openai.com`; useful
  for testing against a localhost mock or a different OpenAI-compatible
  provider)
- `--api-key KEY` — API key (default `$OPENAI_API_KEY`)
- `--timeout SEC` — per-call timeout (default 120)
- `--max-retries N` — retry on `429 / 502 / 503 / 504` (default 3,
  exponential backoff)
- `--out PATH | -` — output JSONL path (default stdout)
- `--dry-run` — build prompts, do not call the API. Prompts written to
  stderr so you can inspect them before spending money.
- `--include-prompt` — echo the per-item user prompt back into each
  output row (useful for debugging eval drift)
- `--current-and-matched-only` — in `--state` mode, only pass current and
  search-matched positions to the LLM. This is on by default and mirrors the
  app-side token-saving/currentness optimization.
- `--include-all-positions` — disable that pruning when the query explicitly
  asks about past/all-time experience.
- `--write-state` — in `--state` mode, append a `llm_rerank_candidates` step.

## Outputs

JSONL, one line per input. Order matches input order.

```jsonc
{
  "id": "p123",
  "score": 0.85,
  "verdict": "include",
  "reason": "AI engineer at OpenAI; matches both traits.",
  "confidence": 0.88,
  "trait_scores": {"ai or software engineer": 0.92, "at openai": 1.0},
  "model": "gpt-4o-mini",
  "elapsed_ms": 412,
  "error": null,
  "input": { ... original object ... }
}
```

A summary is printed to stderr at the end:

```
rerank: items=N concurrency=M ok=X failed=Y elapsed=Ts
```

Exit code is 0 if `failed == 0`, else 1.

## Examples

### Dry-run (no API spend)

```bash
echo '{"id":"p1","name":"Ada Lovelace","headline":"AI engineer at OpenAI"}' \
  | python packs/search/primitives/llm_rerank_candidates/llm_rerank_candidates.py \
      --in - --query "ai or software engineer at open ai" \
      --traits "ai or software engineer" --traits "at openai" \
      --dry-run
```

### Search-network state run

```bash
python packs/search/primitives/llm_rerank_candidates/llm_rerank_candidates.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --concurrency 200 \
  --write-state
```

Writes:

- `artifacts/<task>/llm_rerank_candidates/query_results.csv`

The CSV uses the app query-results row schema from `network-search-api`:

```jsonc
{
  "conversation_id": "...",
  "query": "...",
  "person_id": "...",
  "result_index": 0,
  "matched_position_indexes": [],
  "final_score": 0.87,
  "trait_scores": {
    "trait": {"score": 0.87, "reason": "...", "confidence": 0.9}
  },
  "overall_reasoning": "...",
  "pre_rerank_score": 0.42,
  "tags": null,
  "vertical_sources": null,
  "created_at": "..."
}
```

### Real OpenAI JSONL run

```bash
cat candidates.jsonl \
  | python packs/search/primitives/llm_rerank_candidates/llm_rerank_candidates.py \
      --in - --query "ai or software engineer at open ai" \
      --traits "ai or software engineer" --traits "at openai" \
      --concurrency 200 \
      --out reranked.jsonl
```

### Load test against a mock server

```bash
# Start a stdlib mock OpenAI server in another terminal — see
# tests/test_llm_rerank_candidates.py for a working example.
python packs/search/primitives/llm_rerank_candidates/llm_rerank_candidates.py \
  --in candidates.jsonl --query "..." --traits "..." \
  --api-base http://127.0.0.1:8123 --api-key fake --concurrency 200
```

## Concurrency model

- A single `asyncio.Semaphore(N)` gates how many calls are in flight.
- Each in-flight call runs `urllib.request.urlopen` on a thread from a
  `ThreadPoolExecutor(max_workers=N)` so blocking I/O doesn't stall the
  event loop.
- Retries are async-aware (`asyncio.sleep` for backoff).
- `concurrency` of 200 is safe in practice on macOS / Linux but watch
  the file-descriptor limit (`ulimit -n`) and your OpenAI tier rate
  limits.

## Prompt

The prompt lives in the primitive (`SYSTEM_PROMPT` in `llm_rerank_candidates.py`).
`PROMPT.md` explains its provenance and relationship to the production app.

## What this primitive does NOT do

- It does not de-duplicate candidates before scoring. In `--state` mode it uses
  the candidate IDs already recorded by retrieval/filtering.
- It does not hide all-time history unless you let the default
  `--current-and-matched-only` pruning run. Use `--include-all-positions` for
  explicit past/all-time queries.
