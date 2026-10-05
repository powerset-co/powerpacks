# Look up a person

Find the person, read their saved parent and child dossiers, and answer the user's question.
This is a free lookup, not a ranked search or a request to build deep context.
Run from the installed checkout identified by the skill; its `.powerpacks/`
holds the user's data. Do not look in the chat's unrelated working directory.
For an explicit hosted-network request, start with the contacts section below.
Otherwise read the local dossier first. Local-only requests never use hosted data.

## Read the saved dossiers

For "tell me everything about Jordan Bravo", "find Jordan's dossier", or a
name/email/phone lookup, start with the existing primitive:

```bash
bin/deep-context lookup --name "Jordan Bravo" --json
# Or use --email "jordan@example.com" / --phone "+14155550100".
```

It resolves child names and identifiers to distinct parents. For one selected
parent, `dossier_body` includes its saved parent and child dossier bodies from
`.powerpacks/deep-context/deep-context.sqlite`, with identical bodies deduplicated.
Read that returned text; a parent Markdown export may only point to a child.
The current parent text comes first; saved context survives parent merges even
when its artifact key still contains an earlier parent ID.
No Markdown export or search index is required. Unresolved candidate dossiers
are not the selected person's confirmed context.

- **One person:** summarize `dossier_body`. An empty body means the person was
  found but has no saved parent or child dossier; present the available identity/profile
  information and offer to check whether saved context is available to build one.
- **Several people:** show a numbered list with each person's name and a one-line
  summary of their relationship to the user from `relationship_to_owner`.
  Show how the user knows them (partner, friend, former teammate, shared history),
  rather than a LinkedIn title. Honor relationship corrections already given in chat.
  If the returned relationship is vague or missing, read that parent's full saved
  dossier with `bin/deep-context lookup --parent-id "<returned parent_id>" --json`
  and use supported relationship/shared context. If it remains unknown, say so;
  don't substitute a job title or infer closeness from professional identity.
  Add an identifying contact detail when needed and ask which person the user means.
  Keep each person's facts separate; read the selected parent's full dossier to answer.
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

For another identifier or a name pattern, query the same SQLite store read-only
with parameterized SQL. `parents` holds `parent_id` and `display_name`; `people`
maps each `person_id` to its `parent_id`. `person_identifiers` holds `kind` and
`normalized_value`; `imported_people.row_json` has saved profile fields such as
`linkedin_url` and `twitter_handle`. Use `LIKE`, `GLOB`, or a registered regex
to find plausible matches, then read a chosen parent with `lookup --parent-id`.
Inspect the stored fields rather than inventing columns. Similar text alone does
not prove identity. Keep dossier reads in this store, without `index.json`,
Markdown-folder scans, or the separate search index.

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

For local-only requests, use only the SQLite lookup above. Never run semantic
retrieval for a person's name or require an index build. If the available sources
find nobody, say where you looked and offer another
identifier. When hosted access is allowed, another available network/account
can be an option; name it and never invent access. Local-only stays local.

## Answer

Lead with who the person is, then the relevant background and relationship
context supported by the dossier. For "everything", cover the available
sections without dumping raw JSON or repeating child records. Keep parent IDs
and internal paths out of the answer. Link profiles; only link a dossier file
after verifying it exists, since the text may live only in SQLite. Preserve
dates, sources, and uncertainty.
Summarize across the returned parent and children without repeating the same
facts. A child reference is not the child's content: if only a reference or sparse
summary is saved, explain that limit rather than claiming a complete dossier.
Distinguish recorded facts from inferences and missing information. A failed
lookup never establishes that the user does not know someone. Do not trigger
collection, synthesis, enrichment, or sharing just to answer a lookup.

When there is a gap, give the best available answer first, then one or two
useful next steps based on what you found. Do not make the user choose a backend
or skill, ask them to approve routine reads, or append a menu to a complete answer.
If they choose to build missing context, follow `deep-context` to check existing
sources and explain the actual scope and any cost before processing; don't
promise a one-person build or start processing the whole network implicitly.
