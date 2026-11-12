# sales_nav_pipeline

Created: 2026-11-11 (rewritten; the first version dates from the MCP relay days)

Change log:
- 2026-11-11: The pipeline makes its own calls. No `blocked_tool_call`, no `get_artifact`, no
  MCP. Backend by set: with a set it calls the Powerset API's `/v2/sales-nav` routes (every
  seat in the set, merged); without one it searches through this Mac's LinkedIn session.
  Plan files append queries to a run; the output carries a summary per query.

Resumable Sales Navigator run: search plan → `leads.jsonl` → enrich → mutual URLs →
`leads.csv` / `mutuals.csv` → optional LLM scoring (approval-gated, the only stop).

## Which backend

| | set present (`--set-id` or `POWERPACKS_DEFAULT_SET_ID`) | no set |
| --- | --- | --- |
| search | Powerset API, every Sales Nav seat in the set, merged by member id | your own LinkedIn session, your seat only |
| resolve names → ids | Powerset API | Sales Nav typeahead through your session |
| profile enrichment | Powerset API (RapidAPI, about one lookup per lead) | not done, step records the reason |
| mutual member ids → URLs | Powerset API cache layers (free unless `--resolve-mutuals-external`) | not done |
| team attribution | yes | not done |
| cost | Powerset's | 100 LinkedIn requests a day; a run costs one plus one per query (plus one per text title) |

The chat's Search-in picker already puts the set into `POWERPACKS_DEFAULT_SET_ID`, so a chat
scoped to a set runs cloud and a local-only chat runs local without a flag.

## Run

```bash
# names → ids, through whichever backend the set says
python packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py resolve --company Stripe --title CTO

python packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py run \
  --query "CTOs at Stripe" \
  --search-plan-json .powerpacks/sales-nav/runs/<run>/search_plan.json
```

The output is `status: completed` with `searches`, one entry per query:

```json
{"id": "cxo", "label": "company + executives", "filters_description": "company=Stripe; seniority=CXO,Owner / Partner",
 "total_count": 61, "results_returned": 25, "new_leads": 19, "lead_count_after": 44, "has_more": true, "next_start_offset": 25,
 "lead_summaries": [{"name": "…", "title": "…", "company": "…", "mutual_count": 3, "location": "…", "linkedin_url": ""}],
 "refinement_hint": null, "unsupported_filters": []}
```

Read them. If the titles are wrong for the ask or the pool is thin, write a second plan file
with new query ids and run `continue --ledger <ledger> --search-plan-json <file>`: the ids not
seen before are added and run, the rest are kept. Everything dedupes into one `leads.jsonl`.

## Search plan shape

```json
{
  "score_criteria": "current C-level operators at Stripe; score 0 for VPs, directors, advisors, past-only",
  "queries": [
    {"id": "pool", "label": "company only", "args": {"company_ids": [2135371], "company_names": {"2135371": "Stripe"}}},
    {"id": "cxo", "label": "company + executives", "args": {"company_ids": [2135371], "company_names": {"2135371": "Stripe"}, "seniority_ids": ["310", "320"]}},
    {"id": "cto", "label": "exact title", "args": {"company_ids": [2135371], "company_names": {"2135371": "Stripe"}, "title_ids": [{"id": 8, "text": "Chief Technology Officer"}]}}
  ]
}
```

`args` take what `/v2/sales-nav/search` takes: `company_ids`, `company_names`, `past_company_ids`,
`past_company_names`, `seniority_ids`, `function_ids`, `geography_ids`, `geography_names`,
`headcount_ids`, `industry_id`, `title`, `title_ids`, `keywords`, `count`, `start_offset`. The local
session honours all but `past_company_ids`, `headcount_ids` and `industry_id`, and says so in
`unsupported_filters`. Cloud queries run four at a time; local queries run in one browser pass.

## Scoring

```bash
python ... continue --ledger <ledger> --criteria "real estate exposure"      # asks (blocked_approval)
python ... approve llm --ledger <ledger> --approval-id <id> --confirm
python ... continue --ledger <ledger> --criteria "real estate exposure"
```

## Flags

- `--skip-enrich`, `--enrich-limit N` (default 100, most mutuals first)
- `--skip-mutual-url-resolution`, `--mutual-url-limit N`, `--resolve-mutuals-external` (paid)
- `--discover-mutuals`, `--discover-stagger`, `--discover-max-leads` (team attribution, cloud only)
- `--count` per page (25 is LinkedIn's cap), `--force` to redo export and scoring
