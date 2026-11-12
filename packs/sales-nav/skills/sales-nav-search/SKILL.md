---
name: sales-nav-search
description: Find people in the extended network with LinkedIn Sales Navigator. Through the Powerset API when the chat has a set (every teammate's seat, merged), through the user's own LinkedIn session when it does not. Resolve ids, run a plan of a few structured queries, read each query's summary, add queries when the pool is wrong or thin, score with strict criteria.
---

# Sales Nav Search

Created: 2026-11-11 (rewritten; the MCP relay version dates from 2026-03)

Change log:
- 2026-11-11: No MCP. The pipeline calls Sales Navigator itself, cloud or local by set. The cloud
  agent's query playbooks (network-search-api react_graph.py) move here, since the agent reading
  this file is now the one deciding what to search next.

Use this skill when the query asks for people the user does not know yet: `/sales-nav-search
<query>`, "find LinkedIn leads at <company>", "CTOs at <company> outside my network", "who else",
"2nd degree", or when the search skill hands off after the network came up short. "Who do I
know at <company>" is a network search, not this.

For mutual connections with one named person, see "One person" at the end.

## How it runs

One primitive does the work, from the Powerpacks checkout:

```bash
PIPE="uv run --project . python packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py"
```

Which LinkedIn it talks to is decided by the set, nothing else:

| | the chat has a set (`POWERPACKS_DEFAULT_SET_ID`, from the Search-in picker, or `--set-id`) | no set (local-only chat) |
| --- | --- | --- |
| who searches | the Powerset API, every Sales Nav seat in the set, merged | the user's own LinkedIn session on this Mac |
| 2nd degree of | the whole team | the user |
| needs | Powerset sign-in (`$powerset login`); the token refreshes itself | the LinkedIn Chrome profile the connections import signed in to |
| enrichment, mutual URLs, team attribution | yes | no; the step says so in its summary |
| cost | Powerset's | 100 LinkedIn requests a day: one per run plus one per query, two per `resolve` name |

Never ask the user which. A `failed` answer naming `budget` means the local day is spent: say so
and stop. A `signed_out` answer means the user signs in again through the LinkedIn connections
import. A cloud answer with `reconnect_required` means a teammate's Sales Nav seat needs
reconnecting in the web app: say which, and go on with the rest.

Leads are sensitive. Keep them in the run folder (`.powerpacks/sales-nav/runs/<run>/`), show
compact summaries in chat, and never paste lead payloads into commands.

## The loop

1. **Resolve names to ids.** Companies and titles are filtered by LinkedIn id, not text:

   ```bash
   $PIPE resolve --company "Stripe" --title "Chief Technology Officer"
   ```

   Take the first match (`recommended: true`) and go; do not ask the user to pick between a
   parent and its subsidiaries. Mention alternatives in one clause at the end ("searched Stripe;
   also found Stripe Partners"). If nothing resolves, retry once in the same turn with two or
   three variants (strip possessives, try the acronym or spaced form, for a university add "<school>
   Investment Office"), then use the parent institution. Never put an unresolved company name
   in `title` or `keywords` as a substitute.

2. **Write a plan** of one to four structured queries (shape below), the playbook for the ask
   deciding which. Structured filters first; `keywords` last and rarely.

3. **Run it.**

   ```bash
   $PIPE run --query "<the user's ask>" --run-id <slug> --search-plan-json .powerpacks/sales-nav/runs/<slug>/plan.json
   ```

   The output is `status: completed` with `searches`: per query, `filters_description` (what it
   asked for, in words), `total_count`, `new_leads`, `lead_count_after`, `has_more`, the first ten
   `lead_summaries` (name, title, company, mutual count), `refinement_hint` from the cloud, and
   `unsupported_filters` from the local session.

4. **Read the summaries like a reviewer.** Do the titles and companies match the ask? Is the pool
   thin (under five) or wrong (right company, wrong level)? If so, write a second plan file with
   new query ids and continue; the ids already run are kept, the new ones run, everything dedupes
   into one `leads.jsonl`:

   ```bash
   $PIPE continue --ledger .powerpacks/sales-nav/runs/<slug>/pipeline.json --search-plan-json .../plan2.json
   ```

   Stop at about five queries per ask. After that, answer from what loaded or say the search did
   not find enough good matches.

5. **Score when the ask is qualitative** (C-suite, "investment side", "fintech background",
   "most senior"). Title filters are brittle; the scorer reads whole profiles. It costs money and
   asks once:

   ```bash
   $PIPE continue --ledger <ledger> --criteria "<strict criteria>"       # answers blocked_approval
   $PIPE approve llm --ledger <ledger> --approval-id <id> --confirm
   $PIPE continue --ledger <ledger> --criteria "<strict criteria>"
   ```

   Write criteria that exclude, not prefer: "current C-level operators at Stripe (CEO, CFO, CTO,
   COO, Chief X Officer, President as top officer); score 0 for VPs, directors, heads, advisors,
   board-only, past-only, and similar-name companies". The scorer drops at 0 and cuts at 0.3;
   "deprioritize" leaves borderline people in.

6. **Present.** Lead with how many were found and how many have mutual connections, then the top
   leads: name, title, company, location, mutual count, LinkedIn URL when there is one (local
   leads have none; say "N mutuals" and the name). Point at `exports/leads.csv` and
   `exports/mutuals.csv`. If a query has `has_more`, say so and ask before paging; paging is the
   same query with `start_offset` set to its `next_start_offset`, as a new plan id.

## Plan shape

```json
{
  "score_criteria": "optional: strict criteria, scored after the searches (asks for approval)",
  "queries": [
    {"id": "pool", "label": "company only", "args": {"company_ids": [2135371], "company_names": {"2135371": "Stripe"}}},
    {"id": "cxo", "label": "company + executives", "args": {"company_ids": [2135371], "company_names": {"2135371": "Stripe"}, "seniority_ids": ["310", "320"]}},
    {"id": "cto", "label": "exact title", "args": {"company_ids": [2135371], "company_names": {"2135371": "Stripe"}, "title_ids": [{"id": 153, "text": "Chief Technology Officer"}]}}
  ]
}
```

`args`: `company_ids` + `company_names` (always pass the names), `past_company_ids` +
`past_company_names`, `seniority_ids`, `function_ids`, `geography_ids` + `geography_names`,
`headcount_ids`, `industry_id`, `title_ids` (from resolve; `title` text also works and is resolved
for you), `keywords`, `count` (25 max), `start_offset`. The local session cannot do
`past_company_ids`, `headcount_ids` or `industry_id` and reports them in `unsupported_filters`.

Seniority: 100 In Training, 110 Entry, 120 Senior, 130 Strategic, 200 Manager, 210 Senior Manager,
220 Director, 300 VP, 310 CXO, 320 Owner/Partner. Leaders = 220,300,310,320; executives = 310,320.
Function: 1 Accounting, 4 Business Dev, 6 Consulting, 8 Engineering, 9 Entrepreneurship, 10 Finance,
12 HR, 13 IT, 14 Legal, 15 Marketing, 18 Operations, 19 Product, 24 Research, 25 Sales, 26 Support.
Headcount: B 1-10, C 11-50, D 51-200, E 201-500, F 501-1000, G 1001-5000, H 5001-10000, I 10000+.
Geography: 90000084 SF Bay Area, 90000070 NYC, 90000049 LA, 90000091 Seattle, 90000012 Boston,
90000013 Chicago, 90000003 Austin, 90009496 London, 103644278 US, 102257491 UK, 100506914 Europe.

## Playbooks

LinkedIn's filters are literal and its title facets miss compound titles ("Co-Founder & CTO"), so
the pool comes from structure and the precision from scoring.

- **People at a company** ("who works at Brookfield"): company only. If 0, the same org id as
  `past_company_ids` (cloud). If still 0, `keywords` = the resolved company name, once.
- **Exact C-suite at a company**: company only first. Read `max_total_count`: under about 200,
  page the same query until `has_more` is false or about 200 leads loaded, then score once with the
  strict C-level criteria above. At or above 200, do not sweep: company + `seniority_ids`
  `["310","320"]`, then company + `keywords` for the acronyms the ask names (CEO, CTO, CFO, COO),
  then exact `title_ids` only for a named role, accumulate, score. Founder, Owner or Partner counts
  only with an explicit current C-level operating role at that company.
- **Broad executives / leadership**: company + `["310","320"]`. If that loads five or more, stop
  searching and score for current executive leadership; score 0 for non-executive directors,
  managers, advisors, past-only. Company only when fewer than five.
- **Exact title** (CEO, "Head of Sales", "VP Product"): company + `title_ids` first. If it misses
  or looks too literal, the nearest structure in the same turn: company + the matching
  `seniority_ids`, company + `function_ids`, or both. Score the pool against the title before
  saying none.
- **Role or department** (engineering, sales, finance, recruiting): company + `function_ids`.
  A named role inside it adds company + `title_ids`; a named level adds company + function +
  seniority as a third query. Score against the original ask. Company only is the last query,
  not the first.
- **Investment side of an institution** ("Dartmouth endowment", "the investment team at X"): use an
  explicit investment office or company if one resolved. If only the parent did, the parent's
  generic staff are context, not success, however many load: (a) parent + `function_ids` `["10"]`,
  (b) if those are admin or accounting rather than investing, one or two parent + `keywords`
  searches from the user's own words (investment, investor, portfolio, endowment), (c) past
  company + the keyword only as a last resort (it finds alumni). Then score with criteria that give
  0 to trustees, board-only, grants, accounting, faculty, real estate operations and generic
  advisors.
- **A specific person by name**: `keywords` = the name (the one everyday use of keywords), company
  if known.

`keywords` is never the discovery mechanism: it misses everyone whose profile lacks the exact
word. Criteria you would be tempted to put there ("in the Payments division", "working on
agents") go to the scorer instead.

Ask the user only when a filter is genuinely ambiguous (several unrelated "Cisco" companies with
no obvious match) or before paging far past the first results. Do not ask between the steps
above, and do not reuse or resume an older run to answer a new ask: each ask is its own run.

## One person

Mutual connections with one named person, local, through the user's own session, up to 25 URLs
a call (one request per person, cached 30 days, the person's profile is never viewed):

```bash
uv run --project . python packs/ingestion/primitives/discover/linkedin/linkedin_session.py mutuals https://www.linkedin.com/in/<slug>
```

The answer is each person's shared-connection count and the first ten shared connections by
name and URL. Counts against the same 100 a day.

## What this skill does not do

- It does not write leads into contacts. Lead to contact promotion is a separate, future skill.
- It does not promote leads into a Powerset set; that is the web app's job.
- It does not page on its own. More pages are the user's call, one query id each.
