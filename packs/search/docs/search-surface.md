# Search surface

`$search` is the live search router. For a sufficiently specific people request,
its persisted typed candidate router contract is:

```json
{"target":"engine|sql|contacts","profile":"lookup|gtm|recruiting|null","backend":"local|powerset|null","reason":"..."}
```

Bare-person lookup executes through `packs/search/pipeline/search.py`. Before
cutover, ordinary people search remains canonical on the legacy
`search_network_pipeline.py` prepare/Review/run flow, and recruiting remains
canonical on `deep_search_loop.py`. `packs/search/pipeline/search.py` is an
additive opt-in candidate path for GTM/recruiting deterministic tests and
approved read-only real-environment validation only.

| Request | Route |
| --- | --- |
| Person identifier lookup | `engine + lookup`; execute the typed deterministic path. Local fields are capability-derived; Powerset supports set-scoped `person_id`, `name`, `handle`, and `profile_url` only, while email/phone return `unsupported_capability` |
| People by role/company archetype | `engine + gtm`; legacy prepare/Review/run execution |
| People at a named company | `engine + gtm` with company constraints; legacy execution |
| JD, job URL, recruiting shortlist | `engine + recruiting`; canonical legacy deep execution |
| Relational/aggregate local question | `sql`, profile/backend `null` |
| Contact-field/set-contact question | `contacts`, profile/backend `null` |
| Company-only lookup/resolution | live `$search-company` surface |
| Ambiguous target, people, role/domain, or surface | `needs_input`; clarify once; no retrieval or guessed route |

`$search-company`, `$search-sql`, and `$search-contacts` remain distinct live
surfaces. `$search-network` remains a recognized alias, including NanoClaw's
live `/search-network` command and task/result UI.

Credentials and typed `plan_approved`/`judge_approved` booleans are not paid
quality-run authorization. Paid validation requires separate explicit approval
for the named cases, model, bounds, private output path, and maximum spend.
