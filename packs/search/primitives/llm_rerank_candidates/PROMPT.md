# llm_rerank_candidates prompt

This primitive intentionally carries a Powerpacks-local copy of the production
agent rerank prompt shape from `network-search-api/api_v2/search/agent_rerank.py`.
The production implementation uses `AgentReranker` with batch size 2 and high
async concurrency; this primitive uses stdlib HTTP fan-out and the same scoring
principles so local `search-network` runs can emulate app behavior.

The exact system prompt used at runtime is the `SYSTEM_PROMPT` constant in
`llm_rerank_candidates.py`. It asks the model to return:

```json
{
  "score": 0.0,
  "verdict": "include",
  "reason": "specific evidence from the candidate profile",
  "confidence": 0.0,
  "trait_scores": {
    "<trait>": 0.0
  }
}
```

Key copied/scoped app-side rules:

- differentiate candidates; do not give everyone high scores
- cite specific evidence from title/company/education/dates/descriptions
- score missing trait evidence low
- recency matters unless the query explicitly asks for past experience
- explicit exclusions are hard gates and should score 0.0
- output JSON only

In `--state` mode the primitive defaults to passing only current positions and
search-matched positions to the LLM (`--current-and-matched-only`, on by
default). This mirrors the app's `PersonContext.to_xml(current_and_matched_only=True)`
optimization: shrink tokens and avoid confusing the model with irrelevant old
positions. Use `--include-all-positions` only when the query is explicitly
about past/all-time experience.
