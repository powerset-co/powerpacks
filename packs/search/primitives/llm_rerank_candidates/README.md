# LLM rerank candidates

Bounded semantic reranking for the typed search engine.

`packs/search/pipeline/ranking.py` imports `RerankItem` and `rerank_all`
directly. The CLI is retained for the explicit-input behavior evaluation in
`packs/search/evals/run_llm_rerank_candidates_eval.py`; it accepts candidate
JSONL via `--in` and never reads or writes task state.

```bash
uv run --project . python packs/search/primitives/llm_rerank_candidates/llm_rerank_candidates.py \
  --in candidates.jsonl \
  --query "senior backend engineers" \
  --traits "hands-on distributed systems" \
  --out reranked.jsonl
```

Live/model execution requires explicit credentials and is not part of the
offline test suite.
