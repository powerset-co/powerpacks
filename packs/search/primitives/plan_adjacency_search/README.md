# plan_adjacency_search

Decide whether a search should include adjacent candidates beyond literal role
matches.

Use this after query decomposition and before slice generation when the request
mentions domains, company categories, or asks for "adjacent" people.

Modes:

- `off`: strict role/title search only
- `ask_user`: adjacency might help, but the user did not request it clearly
- `company_domain_union`: include people at companies matching the domain, even
  if their title is not an exact role match
- `company_domain_intersection`: require both role match and company-domain
  match
- `title_adjacent`: widen titles only, without company-domain expansion

Rules:

- If the user explicitly asks for adjacent, adjacent-ish, or domain-adjacent
  people, choose an adjacency mode without asking.
- If the query is ambiguous, record `ask_user` and ask whether to include
  company-domain adjacency.
- For "infra engineers", strict mode means people with infrastructure-like
  engineering titles. Company-domain adjacency means engineers or technical
  operators at infrastructure companies, even if their title is generic.
- Keep the adjacency decision in task state before executing widened slices.
- Do not use summary search, company-signal search, expensive scoring, or LLM
  candidate enrichment in V1.

Expected output validates against `schemas/adjacency-plan.schema.json`.
