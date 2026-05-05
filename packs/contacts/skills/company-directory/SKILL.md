---
name: company-directory
description: Browse the Powerset company directory by company name or id. Uses the existing deterministic company-directory API to show people who work at a company with pagination.
---

# Company Directory

Use this skill when the user asks for `/company-directory <company>`, "show me
people at OpenAI", or "who works at Stripe?".

This skill is **not** semantic people search. It does not use `search`, `count`,
`query_results`, `sales_nav_search`, or LLM reranking. It is a deterministic
company-directory lookup backed by the existing app endpoint:

- `POST /v2/companies/search`
- with `include_people: true`
- with exact `company_id` when known
- with app company-name lookup only for initial disambiguation

## Tool

Call the remote `powerset-search` MCP tool:

- `list_company_people`

If the MCP is missing, route the user through `powerset-login` / `mcp_install`.

## Workflow

1. Extract the company name or company id from the user request.
2. If the user supplied a company id, call `list_company_people(company_id=...)`.
3. If the user supplied a name, call `list_company_people(company_name=..., company_limit=5)`.
   This uses the app's company-name lookup, not semantic search.
4. If multiple plausible companies are returned, ask the user to choose. Prefer
   an exact case-insensitive name match when one is obvious.
5. Page through people using `page` and `page_size`. Do not auto-page unless the
   user explicitly asks.

## Supported parameters

- `company_id` — exact company id / URN when known
- `company_name` — company name for lookup/disambiguation
- `set_id` — optional app-supported set filter; use only if the user explicitly
  asks for the company directory scoped to a set
- `page` — zero-indexed people page
- `page_size` — people per company page
- `position_type` — `current` or `all`
- `people_sort` — `current`, `status`, `name`, or `tenure`
- `people_dir` — `asc` or `desc`

Do not invent additional filters. If the user asks for something unsupported
(e.g. "AI engineers at OpenAI"), explain that this skill lists the company
directory and can sort/page the app-supported directory results. Suggest
`search-network` if they actually want semantic/role search in a set.

## Output

Render a compact table:

- name
- headline/title
- LinkedIn/public identifier when present
- current/all position info returned by the API
- network badges/operators when present

If the response has `people_has_more` or tool-level `next_page`, tell the user
how to ask for the next page.

## Examples

### Company name

User:

```text
/company-directory openai
```

Tool:

```text
list_company_people(company_name="openai", page=0, page_size=50, company_limit=5)
```

If the returned company is clearly OpenAI, show its `people`. If several matches
are plausible, ask the user to pick the company id/name.

### Exact company id

User:

```text
/company-directory company_abc123 page 2
```

Tool:

```text
list_company_people(company_id="company_abc123", page=1, page_size=50)
```
