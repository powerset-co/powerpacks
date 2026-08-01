# Search surface

`$search` is the public router for lookup, GTM, and recruiting. For a sufficiently
specific request it persists:

```json
{"target":"engine|sql|contacts","profile":"lookup|gtm|recruiting|null","backend":"local|powerset|null","reason":"..."}
```

Only `target=engine` produces a typed `SearchSpec` and executes
`packs/search/pipeline/search.py`.

| Request | Route |
| --- | --- |
| Person identifier lookup | `engine + lookup`; deterministic and corpus-scoped |
| People by role, function, level, or company archetype | `engine + gtm` |
| People at a named company | `engine + gtm` with company constraints |
| JD, job URL, role brief, or recruiting shortlist | `engine + recruiting` |
| Company-only local relational or directory question | `sql`, profile/backend `null` |
| Other relational/aggregate local question | `sql`, profile/backend `null` |
| Contact-field or set-contact question | `contacts`, profile/backend `null` |
| Ambiguous target, people, role/domain, corpus, or backend | `needs_input`; clarify once and perform no retrieval |

There is no public company-search command. Company resolution is an internal
backend stage for company-constrained people search. `$search-sql` and
`$search-contacts` remain explicit non-engine targets.

All engine profiles use one persisted `SearchSpec`, one selected concrete
runner, a person-grain `CandidateFrontier`, and typed `StageResult` outputs.
Profile and explicit bounds select the layers; legacy fast/deep modes, task
state, and alternate public search aliases are not current product surfaces.

Credentials and typed approval booleans are not paid quality-run authorization.
Paid validation requires separate explicit approval for named cases, model,
bounds, private output path, and maximum spend.
