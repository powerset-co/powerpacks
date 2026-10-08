---
name: search
description: "Find people from a natural-language request, a job description or posting URL, or a company's open roles. Use for \"find engineers\", \"who do I know for this job\", \"who in my network can help Cloaked\", person/dossier lookups, and search refinements. Search Powerset, Personal Network, or the local network. Load this skill before concluding that people search is unavailable: the packaged CLI searches people even when MCP only exposes account/network tools."
---

# Search

Turn the request into useful people and finish the search. Choose the tools yourself;
users do not need to know skills, sets, or modes. Ask only for a missing choice that
changes the result or an unavoidable human action. Do not ask routine spend or
execution questions. For a named person or dossier, follow [person-lookup.md](person-lookup.md) first;
this read-only lookup skips search preparation and `decision.json`. Requests for
people like someone or who worked with them continue below. For searches, start
with **Choose the network**, including its visible scope statement, before company intake. A request to preview or review step by
step still means pause.

For ordinary and deep searches, this chat advises and one native sub-agent runs
the commands in tmux: follow [tmux-worker.md](tmux-worker.md). This chat owns the
tasks, scope, reviews, recovery decisions and user messages until useful results
are verified. Quick person/dossier lookups stay in this chat.

## Locate the tools

Run commands from the configured canonical checkout, not the conversation folder
or installed skill bundle. Prefer `POWERPACKS_REPO_ROOT`, then a current Powerpacks
checkout, `~/powerpacks`, or `~/workspace/powerpacks`; see [Powerset](../powerset/SKILL.md#canonical-repo-setup).
Sibling skill links use the installed layout. When reading this repository copy,
load that named skill from the harness catalog. Read only relevant instructions
on the happy path, not source, old runs or transcripts.

## Choose the network

Explicit scope and corrections persist through refinements. Never silently switch
accounts or networks. Remove scope directives from the query text.

- “Local”, “offline”, or “my imported network” uses DuckDB without remote network
  resolution. If only a local index is configured, use it without requiring login.
- For hosted search, call Powerset MCP `list_sets`; use `person_count`, not member
  or operator counts. No stated preference → largest accessible network by count.
  Use its returned name; never hardcode Powerset or a population size.
- “My network” / “people I know” → the signed-in account's Personal Network.
  Get the actual email with [Powerset whoami](../powerset/SKILL.md#powerset-whoami).
  The current API names this “Personal Connections” with `is_personal: true` and
  `role: owner`; other people's personal networks can also be visible. Do not pick
  the first personal row. If ownership remains ambiguous, ask one account question.
- A named network wins. A company/location in “find people in XYZ” is a query
  constraint unless context identifies XYZ as a network.
- Pass the selected ID as `--set-id` to prepare or deep initialization. Keep IDs
  internal; choosing a network for a search does not change the saved default.

State scope once: “Searching <name>: <count> people,” or “Searching the Personal
Network for <actual email>: <count> people.” Local: “Searching your imported network.”
For personal size 0, check [account/network health](../powerset/SKILL.md#network-health)
and ask about another account/network if still empty. At 1–9, suggest another may
have more people and continue. Never replace an explicit personal scope automatically.
Unknown counts, a failed listing, and zero search matches are not empty networks.

## Understand the request

- Company-help/hiring request (including “who can help Cloaked”) → discover
  openings and get the role choice below; only then prepare a people search.
- Job posting URL or pasted JD → [deep mode](deep-mode.md).
- Description of people to find → ordinary search.

Preserve role, location, seniority, hard requirements and JD alternatives. Review
extractor output for correctness, without making a broad query more specific.
Defaults rank; they do not become extra hard requirements. Derive seniority from
stated levels, never years of experience. With no stated level use junior/mid/senior/
staff ICs; explicit leadership wins. “Product managers” does not imply management
seniority. User corrections bind every subsequent pond and refinement. For “people like X,”
anchor to X’s known current role/level; ask IC versus leadership only if ambiguous.

## Company → openings → people

For “who can help Cloaked”, a company name in hiring context, or “they're hiring,”
find the company's official site and careers page, then its linked job board.
Use web search/browser tools to find the official board. For Ashby, use the existing
fetcher so a JS page or web-tool API failure does not turn into snippet-based jobs:

```bash
uv run --project . python packs/search/primitives/deep_search/fetch_jd.py \
  --list-openings --url '<official Ashby board URL>' --out <run>/openings.json
```

It returns actual published titles, locations and posting URLs without full JDs.
For other boards read the official current list or its public API. Failed access is
not “no openings.” Never present snippets as verified current jobs. Preserve each
posting URL; read its JD when the user selects the role.

Show the returned count and a **Role | Location | Posting** table: one row per
published opening, with its actual title, all locations and a direct posting link.
Group rows Engineering → Product → Design → GTM → Finance → Other; user priorities
override this order. Classify by the work (legal counsel belongs in Other).
Ask which roles to search **after showing the table**, unless already selected.
Then pass each chosen posting URL to [deep mode](deep-mode.md), preserving network
and corrections and keeping results per role. No openings → say so and ask which
role they need. Never search the literal phrase “help <company>” or invent a job.

<!-- decision-rules:start -->
Record `surface`, `backend`, `depth`, `mode`, and a one-sentence `reason`:

- **surface:** `people` by default; `company` when asking for companies/IDs/funding
  rather than people; `sql` for cross-row aggregates, role ordering, or person joins;
  `contacts` for contact-field browsing. A name alone is a people lookup. “Worked
  with Python” is not SQL; “overlapped with Jane at Stripe” is.
- **backend:** explicit `local`/offline/imported network → `local`; explicit Powerset,
  named/shared network → `powerset`. Otherwise retain the current search's scope.
  For a new search use `powerset` if remote credentials exist, else `local` when a
  local index exists. `POWERPACKS_LOCAL_SEARCH_DB` explicitly selects local.
  Both configured without that override → `powerset`. SQL is always `local`;
  company and contacts are `powerset`. Never silently move an explicit local request
  to a hosted specialist; explain a capability gap instead.
- **depth:** `deep` for a JD/posting URL, detailed role brief, deep/thorough/judged
  request, recruiting/shortlist/source-candidates deliverable, or best/top-tier/
  strongest/cracked candidates with role context. `fast` for ordinary descriptions
  and lookups. Bare “find candidates” needs role context; a profile URL alone is a
  lookup, not a role brief. An explicit fast request overrides automatic deep intake.
  Company hiring intake discovers/selects a posting first.
- **mode:** `auto` for deep by default, continuing until five unique people
  rate at least 4 overall or no supported new pond remains; `interactive` only for explicit step-by-step review.
  Fast searches and other surfaces use `interactive` (no extra execution question).
- Uncertain route → `people`, the applicable backend, `fast`, `interactive`; record
  the uncertainty without blocking. Ask only when missing role/company identity
  prevents a meaningful search.
<!-- decision-rules:end -->

Write the existing `decision.json` in `.powerpacks/search/<slug>` for fast or
`.powerpacks/deep-search/<slug>` for deep before running. Use a fresh run for a new
query/refinement, carrying forward scope and corrections; deep ponds share one run.
For an interrupted command, resume the same run from its saved state; do not
create a new run or repeat completed paid work just because the worker stopped.

## Run the search


- JD/posting/shortlist → [deep-mode.md](deep-mode.md).
- Named person, dossier, or identifier lookup → [person-lookup.md](person-lookup.md).
- Relational/aggregate question → [search-sql](../search-sql/SKILL.md).
- Company lookup/funding/IDs → [search-company](../search-company/SKILL.md).
- Contact fields → [search-contacts](../search-contacts/SKILL.md).
- Explicit offline → use the read-only local SQL tool in [search-sql](../search-sql/SKILL.md)
  for the supported query. No remote fetch, extraction, embeddings, scoring or upload.
  Say when the local data cannot answer; `--search-only` alone is not fully offline.

For ordinary people search:

```bash
uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py prepare \
  --query '<request without scope directives>' \
  --set-id '<selected network ID>' --output-dir '.powerpacks/search/<slug>'
```

For local retrieval replace `--set-id` with `--backend local --db '<db>'`.
The DB is `POWERPACKS_LOCAL_SEARCH_DB` or `.powerpacks/search-index/local-search.duckdb`.
Omit `--env-file .env` when no env file exists. Missing index: follow the existing
[build-local-search-index](../build-local-search-index/SKILL.md) workflow and resume
when ready; surface only necessary user action.

Review the returned query/filters yourself; correct extraction errors, preserve
constraints and OR alternatives. A broad/zero-count preview merits diagnosis, not
an automatic relaxation. For `company_directory_fast_path`, follow its returned
tool request. Otherwise run the returned `execute_command`, including its existing
`--execute-approved` flag; do not ask for another approval. Keep a user-specified
limit; do not invent one. Preparation is not completion.

Local retrieval stays on DuckDB; hosted model extraction/ranking can still run.
Missing scoring credentials can use `--search-only` when preparation succeeded;
explain unranked results. Never describe this as an offline guarantee.

## Results and recovery

Present count, up to five useful candidates with concise evidence and profile links,
and the viewer link when available. Ordinary results come from the returned CSV;
deep mode documents its bounded preview. Do not fabricate fit or expose internal
ledgers/paths as the answer. Zero matches means zero matches in the selected network.

Use the actual error and documented recovery. Retry transient reads once. For thin
job pages, try the official posting API or browser before asking for pasted text.
Disconnected MCP: follow [Powerset recovery](../powerset/SKILL.md#connection-recovery);
login only if authentication requires it. Retain the pending request and resume.
Never bypass membership checks, write replacement retrieval code, or silently use
Sales Navigator. If a requested “extended search” has no defined surface, clarify it.

Keep the search unfinished until retrieval and the requested ranking complete,
or a supported stopping reason is verified. Follow [tmux-worker.md](tmux-worker.md)
for bounded recovery and [feedback.md](feedback.md) for sanitized technical reports.

For local zero-match diagnosis read [search-sql](../search-sql/SKILL.md#integration-with-a-parent-search).
Apply user corrections to the next execution and log them via [feedback.md](feedback.md).
Load references yourself; do not ask the user to invoke skills. Packaged primitives
own extraction, retrieval, filtering, ranking and persistence.
