---
name: gtm
description: "Find GTM prospects across an authorized Powerset network and cold public profiles, or find introduction paths to an exact target. Use for $gtm and explicit combined or warm+cold prospecting. Honor local, offline, and network-only scope."
---

# GTM discovery

Plan the user's query, then send the typed plan to the Powerset GTM API. The API
owns retrieval, matching, connection evidence, and the selected result set.
This skill returns prospects and introduction evidence. Drafting, sending,
Apollo activation, and Ask the Set are separate requests.

## Scope and plan

Explicit local/offline requests use `packs/search/skills/search/SKILL.md` with the
local backend. Existing network-only searches use that skill or the requested
Sales Navigator surface. Do not add cold retrieval to a network-only request.
Explicit combined/warm+cold GTM requests and exact-target introductions in an
extended/combined network use this workflow. `$gtm` without an
explicit narrower scope uses the authorized network plus cold profiles; state
that scope in the preview.

Translate the request into actual API predicates; a query string alone is not
a filter plan. Preserve AND/OR alternatives, exclusions, current/past role
meaning, geography, seniority, company properties, set, and requested caps.
Resolve company IDs through the existing company resolver when needed. An alias
must identify the same company. Empty current-company results do not authorize a
past-company search or a broader seniority search. Broaden only when requested.

For an exact-person introduction request, preserve the supplied LinkedIn URL
as the target. Do not turn it into an employer search. Known introductions need
both operator-to-introducer and introducer-to-target edges with provenance.
Company overlap with dated evidence may support an explicitly inferred path;
company affiliation alone cannot establish a known introduction. Keep best-fit
cold prospects visible even when their path is unknown.

Write the request to `.powerpacks/gtm/discover/request.json`. The typed request
uses `query`, `set_id`, `target`, `filters`, `sources`, `page_size`,
`max_pages`, `offset`, `allow_provider_calls`, `max_provider_calls`,
`max_semantic_calls`, `semantic_model`, `selection`, and `cursor`. Query wording is context; `filters` contains the predicates.

```json
{
  "query": "current marketing leaders at ExampleCo",
  "set_id": "<authorized set>",
  "filters": {
    "company_ids": ["<resolved company ID>"],
    "role_function": "marketing",
    "seniority_bands": ["director", "vice-president", "c-suite"],
    "is_current": true
  },
  "sources": ["network", "company"],
  "page_size": 25,
  "max_pages": 1,
  "offset": 0,
  "allow_provider_calls": false,
  "max_provider_calls": 0,
  "max_semantic_calls": 0,
  "semantic_model": null
}
```

`network` reads authorized indexed network people. `company` reads stored
company rosters; those reads always remain free and do not fetch a cold company
provider. Stored profile hydration also remains free. `sales_nav` retrieves
authorized extended LinkedIn leads from cache by default; a cache miss needs
`allow_provider_calls` and a positive `max_provider_calls` budget before a
Sales Navigator provider request. Authorized missing-profile hydration may use
RapidAPI or Unipile within that same provider-call cap, with at most four profile
lookups in flight. Sales Navigator pages remain sequential. Include `sales_nav` when
extended-network retrieval is requested. Cached and authorized provider phases use the same
predicates in separate bounded requests. Within each source request,
`max_pages` is shared across its accounts or companies. A cursor resumes the
chosen account/company; it does not reset the criteria or selected candidates.

Semantic scoring has a separate `max_semantic_calls` budget, defaulting to zero,
with at most two matching roles per call. A person with multiple eligible roles
is checked against each role; the winning role supplies the displayed employer
and headcount. A positive budget reviews at most `2 × max_semantic_calls`
uncached roles in their current order. Unreviewed roles stay unknown with partial
semantic coverage. A later refinement can reuse decisions and review the remainder.
Select `semantic_model` explicitly or use the configured `GTM_SCORING_MODEL`;
there is no implicit model. Cached semantic decisions can be reused. Report the
returned semantic coverage, calls, cache hits, model, input/output tokens,
output-token cap, and errors.
An unknown semantic requirement remains unknown when there is no supporting
cached decision or authorized scoring budget.

Seniority bands use canonical index values (`director`, `vice-president`,
`c-suite`); preserve strict C-suite when requested.

Supported filters: `company_ids`, `company_names`, `company_urls`,
`company_queries`, `titles`, `title_ids` (`id` and `name`), `role_function`,
`seniority_bands`, `cities`, `countries`, `is_current`, `excluded_titles`,
`excluded_company_terms`, `company_entity_types`, `headcount_min`,
`headcount_max`, `semantic_traits`. If the user's required predicate cannot be
expressed or verified, say so; do not silently drop it. Read CLI help before
constructing the request. Preview the actual predicates,
authorized sources, per-source caps, and any paid work. Obtain spend authorization
before a paid request; already-approved scope and budget remain approved.

For "partners who explicitly practice litigation", plan both role and specialty:

```json
{"titles": ["Partner"], "is_current": true,
 "semantic_traits": ["Substantive current litigation, trial, arbitration or disputes practice in the matching role, supported by the person's own practice description. Transactional advising, incidental litigation mentions or past-only litigation do not establish this practice; missing current evidence is unknown."]}
```

`Partner` alone establishes the requested role. Litigation requires supporting
person/role evidence; the firm's litigation practice does not establish theirs.

For company-wide "Head of Operations", `role_function: "operations"` alone is
too broad. Add a semantic requirement for current company-wide operations or
business-operations leadership (COO or Head/VP/Director of Ops/BizOps). Exclude
specialized revenue, sales, GTM, clinical, product, engineering, IT, people,
facilities and workplace operations unless the user requests that department.

Industry exclusions may need `semantic_traits` as well as literal
`excluded_company_terms`: `finance` does not match `financial`. For "exclude
accounting and finance firms", require evidence that the matching employer is
neither an accounting nor a financial-services firm; missing industry evidence
stays unknown. Do not infer the employer's industry from the person's job title.

For "all employees at a company", require current staff employment; pure
investor, adviser/advisor and board associations do not establish employment.
Express that eligibility in the predicates, using title exclusions where they
preserve legitimate employee roles and semantic evidence when needed. An
explicit investor/advisor/board query retains those requested roles.

## Run and inspect

```bash
uv run --project . python packs/gtm/primitives/discover/discover.py --help
uv run --project . python packs/gtm/primitives/discover/discover.py \
  --request .powerpacks/gtm/discover/request.json
```

Read `response.json`, `candidates.jsonl`, and `manifest.json` in the same fixed
directory. Report loaded, qualified, and source counts using the returned fields;
show actual applied filters, source errors, and caps. For Sales Navigator, show
`coverage.queries`: actual RestLi filter strings, keywords, start, and count.
Partial coverage is partial
coverage. A capped or exhausted source does not prove no other prospects or paths
exist. Do not claim latency or live-provider success from fixture tests.

Present the best-fit results with matched current role/company evidence and
separate known, inferred, and unknown introduction evidence. An interaction
count supports the operator's relationship to the introducer; it does not prove
the introducer's relationship to the target.

## Follow-ups

For refinement, use `refine --request <file>` with the complete preserved
filters and `selection` containing the full selected `fit.candidate` records
from the prior response. The caller must copy every preserved filter; the API
evaluates the supplied filters and does not recover earlier predicates for you. For sort, use `sort --request <file>` with
`{"candidates": [<full selected ranked records>], "sort": "company_headcount",
"descending": true}`. Sort options are `fit`, `company_headcount`, and `name`.
Default descending `fit` sort groups qualified people before unknown people and
uses semantic scores when available. Deterministic-only ties retain input order;
there is no fabricated relevance or warmth score.
Each operation writes its own fixed `.powerpacks/gtm/<operation>/` artifacts;
keep the full response evidence when constructing follow-up requests.

A refinement adds predicates to the existing plan and operates on the selected
candidates. Preserve every earlier predicate unless the user explicitly removes
it. Unknown required evidence does not pass. A sort is a permutation of the exact
selected person IDs: no additions or removals, and both sort key and displayed
employer refer to the matching role/company. Unknown sort values go last.

For another page or a provider phase after a cached phase, reuse the exact
query, set, target, and filters. Send previous selected full `fit.candidate`
records as `selection`. Build `cursor` from the chosen coverage row:
`source`, `offset` from `next_offset`, `account_id`, and `company_id`. Preserve
`sources`; the cursor retrieves only its owning source/account/company. The API
validates that account and company against the authorized scope and resolved
identities. Keep the authorized provider cap explicit.
`discover` appends and deduplicates those candidates with the requested page;
`refine` evaluates only the supplied selection. Read the existing response before
writing the next request. Paging results retain earlier candidates; source
coverage in each response describes that request, so report earlier source errors
from the earlier response rather than claiming the latest page recovered them.

A request to broaden retrieval changes the requested provider predicates, rather
than merely rescoring the same candidates. Pagination retains the resolved
company, strict role/seniority predicates, set, and source coverage. Do not silently
expand C-suite to VPs. Inspect the current artifact before reusing it; never
present a stale artifact as a new search.
