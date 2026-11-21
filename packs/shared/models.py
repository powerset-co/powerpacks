"""Every model search calls and the reasoning effort it is asked for, by job.

This is the one place a model name is written. Code takes its default from here; a command's
`--model` / `--reasoning-effort` flag still changes it for one run. Nothing here is read from the
environment or `.env`.

Standard library only: script-style primitives import it before the project's packages.

Created: 2026-11-21
"""
from __future__ import annotations

# ── A query becomes a search (search_network_pipeline prepare) ─────────────────────────────────
# One model for every extractor, title selection and the place review.
SEARCH_EXPAND = "gpt-5.6-luna"
SEARCH_EXPAND_EFFORT = "medium"
# Each extractor's own model, used only when expand_search_request runs with no --model.
SEARCH_EXTRACTORS = {
    "temporal": "gpt-4.1",
    "company": "gpt-5.4",
    "location": "gpt-4.1",
    "education": "gpt-4.1",
    "seniority": "gpt-4.1",
    "social": "gpt-4.1",
    "role": "gpt-4.1",
    "trait_generation": "gpt-4.1",
    "company_adjacency": "gpt-5.4",
    "interaction": "gpt-4.1-nano",
}

# ── The found people are screened and ranked (search_network_pipeline run, local and JD) ───────
SEARCH_FILTER = "gpt-5.6-luna"
SEARCH_FILTER_EFFORT = "none"
SEARCH_RERANK = "gpt-5.6-luna"
SEARCH_RERANK_EFFORT = "medium"
# Refining a finished search by a criterion (search_network_pipeline rerank).
SEARCH_REFINE = "gpt-5.1"

# ── Companies and embeddings ───────────────────────────────────────────────────────────────────
# Scores companies against an industry description on the local index (ce_rerank).
COMPANY_SCORE = "gpt-4.1-nano"
EMBEDDING = "text-embedding-3-small"

# ── Job-description (deep) search ──────────────────────────────────────────────────────────────
JD_CLEAN = "gpt-5.6-sol"                # clean_job_description
JD_CLEAN_EFFORT = "high"
JD_DECOMPOSE = "gpt-4o"                 # decompose_jd
JD_QUERY = "gpt-5.6-luna"               # the first pond's query (deep_search_loop)
JD_QUERY_EFFORT = "medium"
JD_PATTERN_DEFAULTS = "gpt-5.6-terra"   # proposed payload edits before a pond runs (search_harness)
JD_PATTERN_DEFAULTS_EFFORT = "medium"
JD_CAPABILITY = "gpt-5.6-luna"          # the capability judge (llm_rerank_candidates/terra.py)
JD_CAPABILITY_EFFORT = "low"
JD_DOMAIN = "gpt-5.6-terra"             # the domain judge (candidate_judges)
JD_DOMAIN_EFFORT = "medium"
JD_OPPORTUNITY = "gpt-5.6-luna"         # the opportunity judge (candidate_judges)
JD_OPPORTUNITY_EFFORT = "low"
JD_PIN = "gpt-6-sol"                    # introduce / review / not supported (pin_confidence)
JD_PIN_EFFORT = "low"
JD_DECIDE = "gpt-5.6-luna"              # stop or search another pond (search_harness decide)
JD_DECIDE_EFFORT = "medium"

# ── Sales Navigator ────────────────────────────────────────────────────────────────────────────
SALES_NAV_SCORE = "gpt-4o-mini"
