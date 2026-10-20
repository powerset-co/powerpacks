# Look up a person

Find the person, read their saved parent and child dossiers, and answer the user's question.
This is a free lookup, not a ranked search or a request to build deep context.
Run from the installed checkout identified by the skill; its `.powerpacks/`
holds the user's data. Do not look in the chat's unrelated working directory.
A lookup reads this local store only.

## Read the saved facts

For "tell me everything about Jordan Bravo", "find Jordan's dossier", or a
name/email/phone lookup, start with the existing primitive:

```bash
bin/deep-context-v2 lookup --name "Jordan Bravo" --json
# Or use --email "jordan@example.com" / --phone "+14155550100".
```

It resolves written names and identifiers to families (one per person the user has talked
to) and prints each family's saved facts from
`.powerpacks/deep-context/deep-context-v2.sqlite`: `relationship_to_owner`, employers, school,
location, topics, notable events, the identifiers, and the one LinkedIn the store confirmed.
No Markdown export or search index is required.

- **One person:** summarize the facts. Lead with how the user knows them
  (`relationship_to_owner`), then work and shared history.
- **Several people:** show a numbered list with each person's name and a one-line
  summary of their relationship to the user from `relationship_to_owner`.
  Show how the user knows them (partner, friend, former teammate, shared history),
  rather than a LinkedIn title. Honor relationship corrections already given in chat.
  Add an identifying contact detail when needed and ask which person the user means.
  Keep each person's facts separate.
- **No match:** try useful name parts, known aliases, or supplied identifiers
  before asking for help. If a plausible match appears, show its identifying
  details and ask "Did you mean this person?" Similar spelling is not identity.
- **No local database:** this installation has no local facts. Say so; do not
  start setup or import data.
- **Unreadable database:** report the access/setup error; do not treat it as an
  empty database or replace it. Use `powerpacks-doctor` for an unclear setup error.

Exact names take priority; otherwise every supplied name part must match a name
on the person or parent. Nicknames work when saved as a child's name; arbitrary
typos are not automatically corrected.

For another identifier or a name pattern, query the same SQLite store read-only
with parameterized SQL. `candidates` holds `candidate_id` and `display_name`;
`current_parent` maps each `candidate_id` to its `parent_id`; `candidate_identifiers`
holds `kind` and `normalized_value`; `facts.facts_json` has the saved facts. Use `LIKE`
or `GLOB` to find plausible matches, then read the chosen family with `lookup`.
Inspect the stored fields rather than inventing columns. Similar text alone does
not prove identity. Keep dossier reads in this store, without `index.json`,
Markdown-folder scans, or the separate search index.

## When nobody is found

Use only the SQLite lookup above. Never run semantic retrieval for a person's name
or require an index build. If the store finds nobody, say where you looked and
offer another identifier.

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
