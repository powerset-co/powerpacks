---
name: search-contacts
description: Browse Powerset contacts through the Powerset Search MCP. Natural-language routing for my contacts vs set contacts, app-native search fields, app-native sorting, pagination, and field projection.
---

# Search Contacts

Use this skill when the user asks for `/search-contacts <request>`, "show my
contacts", "show the most messaged contacts in my Powerset set", or "show my
contacts with emails, LinkedIn, and Twitter".

This is a pure MCP representation of the existing Powerset app contacts views.
Do **not** convert this into semantic people search. Do **not** call `search`,
`count`, `query_results`, Sales Nav tools, or company-directory tools from this
skill.

## Tools

Call the remote `powerset-search` MCP tools directly:

- `list_contacts` — app-equivalent `GET /v2/contacts` for the authenticated
  user's own contacts.
- `list_sets` — enumerate accessible sets when the user names a set.
- `list_set_contacts` — app-equivalent `GET /v2/set-contacts/{set_id}` for
  contacts across a set.

If the MCP is missing, route the user through `$powerset setup` / `mcp_install`
rather than pretending to fetch contacts.

## Routing

### 1. Decide own contacts vs set contacts

Use `list_contacts` unless the user explicitly names a set.

Set language examples:

- "in my Powerset set"
- "in the Powerset set"
- "for the Powerset set"
- "from the Founders set"

If the user names a set:

1. Call `list_sets`.
2. Resolve the set name using this exact tie-breaker:
   1. exact case-insensitive name match
   2. prefer non-personal sets
   3. prefer highest `member_count`
   4. then personal sets
   5. if still ambiguous, ask the user to choose
3. Call `list_set_contacts` with that `set_id`.

Do not invent set ids. Do not call local `resolve_set_operators` from this
skill; the MCP set list is the source of truth for this UX.

### 2. Parse app-native sort intent

Only use sort fields the contacts APIs expose.

For `list_contacts`:

- "most messaged", "most messages", "most interactions" →
  `sort_field: "total_messages"`, `sort_dir: "desc"`
  (`list_contacts` accepts `total_messages` as an alias for the app's
  `total_interactions` sort field.)
- "least messaged" → `sort_field: "total_messages"`, `sort_dir: "asc"`
- "by first name", "alphabetical" → `sort_field: "first_name"`, `sort_dir: "asc"`
- "by last name" → `sort_field: "last_name"`, `sort_dir: "asc"`
- "by headline/title" → `sort_field: "headline"`, `sort_dir: "asc"`
- "by location" → `sort_field: "location_raw"`, `sort_dir: "asc"`

For `list_set_contacts`:

- allowed sort fields: `first_name`, `last_name`, `headline`, `total_messages`
- default for set contacts: `sort_field: "total_messages"`, `sort_dir: "desc"`

If the user gives no sort preference:

- own contacts: use tool defaults (`first_name asc`)
- set contacts: use `total_messages desc`

### 3. Parse app-native search filters

Only use the search syntax supported by the app contacts routes:

- plain text → name search, e.g. `search: "arthur chen"`
- `email:<text>` for email/domain filtering, e.g. `email:@powerset.co`
- `phone:<digits>` for phone filtering, or `phone:` for contacts with phones
- `headline:<text>` for title/headline filtering
- `company:<text>` only when the user explicitly asks to filter contacts by a
  company field in the contacts view

No Twitter search exists in the app search syntax today. If the user asks to
"show twitter", treat Twitter as an output field, not a search filter.

Do not interpret phrases like "AI engineers" semantically. If the user clearly
asks for a headline/title filter, use `headline:engineer`; otherwise ask a
clarifying question.

### 4. Parse output fields

If the user does not specify fields, show the default useful columns:

- name/display_name
- headline/title
- location
- total_messages or interaction_level
- LinkedIn when available

If the user asks for fields, pass `include_fields` only to `list_contacts`.
`list_set_contacts` returns the app's set-contact lead shape; project fields in
the final answer rather than passing `include_fields`.

Field aliases for own contacts:

- "name" → `display_name`, `first_name`, `last_name`
- "email" / "emails" → `emails`
- "linkedin" → `confirmed_linkedin_url`, `public_profile_url`, `public_identifier`
- "twitter" / "x" → `x_twitter_handle`
- "phone" → `phone_number`
- "messages" / "interactions" → `total_messages`
- "headline" / "title" → `headline`
- "location" → `location_raw`

Always include enough identity fields to make the output readable even when the
user requests only contact fields. For example, "show emails and LinkedIn" still
needs `display_name` or `first_name`/`last_name`.

### 5. Pagination

Default to page 0 and page size 50 unless the user asks otherwise. Present the
first 20-50 rows as a compact markdown table. If `has_more` / `hasMore` is true,
tell the user how to ask for the next page.

Do not auto-page through all contacts unless the user explicitly asks to export
or retrieve all pages.

## Examples

### My contacts, most messaged, selected fields

User:

```text
/search-contacts show me my contacts ordered by most messages and show emails, twitter, and linkedin
```

Tool:

```text
list_contacts(
  sort_field="total_messages",
  sort_dir="desc",
  page=0,
  page_size=50,
  include_fields=[
    "display_name", "first_name", "last_name", "emails",
    "confirmed_linkedin_url", "public_profile_url", "public_identifier",
    "x_twitter_handle", "total_messages"
  ]
)
```

### Set contacts, most messaged

User:

```text
/search-contacts show me the most messaged contacts in my Powerset set
```

Tools:

```text
list_sets()
# resolve "Powerset" with exact → non-personal → most members → personal → ask
list_set_contacts(set_id="...", sort_field="total_messages", sort_dir="desc", page=0, page_size=50)
```

### Email domain filter

User:

```text
/search-contacts show my contacts with @powerset.co emails
```

Tool:

```text
list_contacts(search="email:@powerset.co", page=0, page_size=50)
```
