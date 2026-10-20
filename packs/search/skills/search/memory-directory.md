# Local memory directory

Use this guide for local contact labels and associations: “pull up SoF”,
“South of France”, “people tagged mentor”, and follow-up actions on those people.
Deep Context owns generation and identity. Ranked `query` finds evidence; it
never establishes an exhaustive selection. Ordinary recruiting/JD search stays
on the existing search route. An explicitly named Powerset/shared network stays
on that route: never substitute a local corpus or another default network.

Run from the installed checkout recorded by this search skill, using its
`.venv/bin/python`. Identify the user's requested local `.powerpacks` source and
an explicit, separate derived database outside that source directory. If no
local corpus was requested or identified, clarify scope before reading one.
Do not treat a copied skill bundle as a source root. The commands below use
explicit example paths; replace both with the actual requested paths.

```sh
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite import --v2-root /path/to/.powerpacks
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite resolve SoF
```

1. **Resolve.** Inspect every returned meaning, selector origin, distinct count
   and evidence. `source_name` means the literal phrase in original contact
   names; `human_tag` means an original human tag; `topic` means a saved topic
   or inferred label; `saved_value` means an exact saved factual value. SoF
   never implies SF. A literal suffix never proves South of France. Present
   geographic interpretations separately with their individual evidence and
   uncertainty; association does not establish residence. Ask which meaning
   the user wants when multiple meanings remain plausible.
2. **Enumerate.** Pass the chosen selector verbatim. Inspect `total`, collect
   every page using `next_after`, and preserve native `parent`, `members` and
   `corpus` from the returned people. Counts describe canonical people, not
   claim rows or source contacts. Pages use stable native IDs and have at most
   100 people. Present the useful names and provenance, state the full count,
   and retain the exact enumerated JSON for follow-ups. Never rewrite the user's
   label into an LLM search query to choose action targets.
3. **Act.** Use the exact people the user selected in the retained JSON, with
   the same `corpus` and `members`. For multiple pages, combine only their
   explicit people arrays under the same corpus. Do not expand the selection.
   Preview first. “Hide these” means the exact presented people; show their
   names/count and require user confirmation before `--confirm`. Unknown,
   foreign, hidden or changed families fail before any mutation; re-enumerate
   if stale. Human confirmation is required for destructive meaning even if
   a previous search or enumeration was approved.

```sh
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite enumerate source_name sof --limit 50
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite enumerate source_name sof --limit 50 --after NATIVE_PARENT_ID
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite hide --selection /path/to/selected.json --v2-root /path/to/.powerpacks
# Only after the user confirms the exact preview:
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite hide --selection /path/to/selected.json --v2-root /path/to/.powerpacks --confirm
```

An action file retains the enumerated values; this synthetic shape shows one
selected person (copy actual values rather than typing them):

```json
{"corpus":"file:///path/to/.powerpacks/deep-context/deep-context-v2.sqlite","people":[{"parent":"p001","name":"Jordan Synthetic","members":["c001"]}]}
```

“Remove SoF label” requires the origin: an inferred `topic` uses memory
corrections; a `human_tag` uses the existing human-tag writer. Both preserve
people. Preview with `remove-label ORIGIN VALUE --selection ... --v2-root ...`,
then use `--confirm` only after confirmation:

```sh
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite remove-label human_tag sof --selection /path/to/selected.json --v2-root /path/to/.powerpacks
```
 A literal `source_name` suffix
cannot be removed here: explain that the original contact's source must own
that edit. Never reinterpret label removal as person deletion. Hiding uses
Deep Context's native People soft-delete; no new deletion state exists.

Default resolution is free lexical retrieval after exact labels and confirmed
aliases. For hybrid suggestions, add `--embedding-model APPROVED_MODEL` to
`resolve`. Cached embeddings are reused; a missing query embedding returns
`needs_approval` before a provider call. Estimate and obtain permission before
rerunning with the existing `--approve-spend` flag. Semantic suggestions remain
suggestions: confirm a proposed existing selector before enumerating it; never
confirm aliases automatically or claim exhaustive semantic recall. Explicit
`confirm-alias ALIAS TOPIC` is a separate human decision. Import again before a
new request to reflect current identities, tags and hidden families. Import
requires current schema and never migrates the source. Do not run generation,
paid discovery, identity resolution or uploads merely to open this directory.
