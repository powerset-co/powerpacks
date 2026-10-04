# Look up a person

Find the person, read their saved parent dossier, and answer the user's question.
This is a free lookup, not a ranked search or a request to build deep context.
Run from the installed checkout identified by the skill; its `.powerpacks/`
holds the user's data. Do not look in the chat's unrelated working directory.
For an explicit hosted-network request, start with the contacts section below.
Otherwise read the local dossier first. Local-only requests never use hosted data.

## Read the parent dossier

For "tell me everything about Jordan Bravo", "find Jordan's dossier", or a
name/email/phone lookup, start with the existing primitive:

```bash
bin/deep-context lookup --name "Jordan Bravo" --json
# Or use --email "jordan@example.com" / --phone "+14155550100".
```

It resolves child names and identifiers to distinct parents and reads the saved
parent dossier from SQLite. It does not require the Markdown export to exist.

- **One person:** summarize `dossier_body`. An empty body means the person was
  found but has no saved parent dossier; present the available identity/profile
  information and offer to check whether saved context is available to build one.
- **Several people:** give concrete choices, such as "Taylor, the designer at
  North, or Taylor, the engineer at South?" Use returned emails/headlines, not
  invented distinctions. Do not combine their facts. Once chosen, run
  `bin/deep-context lookup --parent-id "<returned parent_id>" --json`.
- **No match:** try useful name parts, known aliases, or supplied identifiers
  before asking for help. If a plausible match appears, show its identifying
  details and ask "Did you mean this person?" Similar spelling is not identity.
- **No local database:** this installation has no local dossiers. Continue to
  available profile/contact lookup below; do not start setup or import data.
- **Unreadable database:** report the access/setup error; do not treat it as an
  empty database or replace it. Use `powerpacks-doctor` for an unclear setup error.

Exact names take priority; otherwise every supplied name part must match a name
on the person or parent. Nicknames work when saved as a child's name; arbitrary
typos are not automatically corrected.

## When only a profile is available

For an explicit Powerset/network lookup, start with
`packs/contacts/skills/search-contacts/SKILL.md`. Also use it when local lookup
finds no person and the request permits hosted access. Preserve a named network;
otherwise search the signed-in user's own contacts. Report which account/network
was searched. The contacts API returns profile/contact fields, not a private
local dossier. Use a returned email or phone to find the same local parent when
the user also wants their dossier. Do not join people on a similar name alone.

If contacts tools are unavailable or login expired, follow the existing
`packs/powerset/skills/powerset/SKILL.md` connection/login recovery and resume the
lookup. An inaccessible source is not "person not found".

For explicit local-only requests, or a LinkedIn URL / Twitter handle, use the
existing local index if available (`POWERPACKS_LOCAL_SEARCH_DB`, otherwise
`.powerpacks/search-index/local-search.duckdb`):

```bash
uv run --project . python packs/search/primitives/local_duckdb_query/local_duckdb_query.py query \
  --sql "SELECT person_id, full_name, headline, current_title, current_company, city, linkedin_url FROM local_person_profiles WHERE full_name ILIKE '%Jordan Bravo%'"
```

Use `primary_email`/`all_emails`, `primary_phone`/`all_phones`,
`twitter_handle`/`x_twitter_handle`, or `linkedin_url`/`public_identifier` for
the supplied identifier (normalize LinkedIn URLs to the slug). Escape SQL string
literals. Read the matched profile, then use its identifiers for dossier lookup.
Never run semantic retrieval for a person's name or require an index build.
If the available sources find nobody, say where you looked and offer another
identifier. When hosted access is allowed, another available network/account
can be an option; name it and never invent access. Local-only stays local.

## Answer

Lead with who the person is, then the relevant background and relationship
context supported by the dossier. For "everything", cover the available
sections without dumping raw JSON or repeating child records. Link the saved
dossier/profile when available; preserve dates, sources, and uncertainty.
Distinguish recorded facts from inferences and missing information. A failed
lookup never establishes that the user does not know someone. Do not trigger
collection, synthesis, enrichment, or sharing just to answer a lookup.

When there is a gap, give the best available answer first, then one or two
useful next steps based on what you found. Do not make the user choose a backend
or skill, ask them to approve routine reads, or append a menu to a complete answer.
If they choose to build missing context, follow `deep-context` to check existing
sources and explain the actual scope and any cost before processing; don't
promise a one-person build or start processing the whole network implicitly.
