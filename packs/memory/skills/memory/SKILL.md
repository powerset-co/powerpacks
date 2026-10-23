---
name: memory
description: Retrieve saved local knowledge about people, relationships, topics, personal labels and preferences. Use for questions about what the user knows or remembers, and follow-ups on remembered groups. Includes named-person dossier lookup. Not recruiting/JD search, shared-network search, or memory generation.
---

# Memory

You choose the retrieval primitive from the meaning of the question and the
conversation. Resolve references such as “those people” from the prior selection;
never silently expand its scope. No classifier or extra model call is needed.

Run from the installed checkout using `.venv/bin/python -m packs.memory`.
For memory queries use the explicitly identified local corpus and its separate
derived SQLite DB. Original dossier lookup does not require a memory DB.
Never guess among owners' stores. If the corpus is known but memory is absent,
read `packs/memory/skills/memory/memory-directory.md` for the free import contract.
For memory queries, import current source changes before a new request. Do not generate, discover,
enrich, upload or rebuild embeddings as a side effect of answering.

## Choose and retrieve

| Intent, including conversational context | Primitive sequence |
| --- | --- |
| Any “who” question: “who is family”, “my Roblox coworkers”, “who helped me buy a house”, “my friends in LA” | `search` with the user's words. It is the same call the Memory page makes, so the user can replay it there. People come back warmest first with the claims that matched. A layer word lists its holders when surrounded only by stop words, or filters when it follows “my”/“our” with up to two words between. Elsewhere it is a plain search word. `--layer` names a label directly. An empty answer means nothing saved matches: say so. |
| “All my SoF people”; a label the user named exactly; an action on that group | Follow `packs/memory/skills/memory/memory-directory.md`: `resolve` → inspect meanings → `enumerate` every page. Retain exact IDs and corpus for actions. |
| “What do I know about this topic?”; evidence behind a search answer | `query` with the user's meaning and hard filters → `open` the useful claims. This is a ranked shortlist of claims, not people. |
| “Tell me everything about Jordan”; “open Jordan’s original dossier” | Follow `packs/memory/skills/memory/person-lookup.md`: use the free native dossier lookup; no memory generation or ranked search. |
| “What do we remember about Jordan on this topic?” | `lookup` exact name or known identifier → disambiguate → `query --person` canonical parent → `open`. For the complete original dossier use `packs/memory/skills/memory/person-lookup.md`. |
| “Who overlapped with Jordan at Acme?” | `lookup` → `overlap` with the canonical parent and organization. Overlap is a possible connection, not proof of friendship or willingness to introduce. |
| “My LPs in Cedar Fund I” | Use the explicit `lp` kind and fund object; use directory resolution/enumeration for a complete saved group. Never infer membership from job titles. |
| Recruiting, a JD, a named shared network, or cold prospects | Use search or GTM with the original scope; do not substitute local memory. |

Replace the example database path and identifiers with observed values:

```sh
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite search 'my roblox coworkers' --embedding-model KNOWN_MODEL --approve-spend
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite search 'who is family'
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite search 'los angeles' --layer 'close friend' --embedding-model KNOWN_MODEL --approve-spend
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite lookup 'Jordan Bravo'
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite query 'permission architecture' --kind work
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite query --person CANONICAL_PARENT_ID
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite query --kind lp --object 'Cedar Fund I'
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite overlap CANONICAL_PARENT_ID --organization Acme
.venv/bin/python -m packs.memory --db /path/to/memory.sqlite open CLAIM_ID
```

For hybrid retrieval, use the corpus's known embedding model via
`--embedding-model`; never guess a model or mix vector spaces. Cached query
embeddings are reused. On `needs_approval`, obtain permission for the estimated
query embedding cost before adding `--approve-spend`. Without a known model or
approved spend, lexical/typed retrieval remains available; describe it honestly.
If the first query misses, try a bounded reformulation using synonyms or an
observed alias while preserving hard filters. Report insufficient saved evidence
rather than “nobody exists.” Check `truncated` and `missing_embeddings`; do not
claim complete recall from a ranked or partially embedded result.

## Questions that require combining evidence

For natural-language questions about people or relationships, use `search`
with the corpus's embedding model under the existing spend rules. Use `--limit 5`.
Keep `resolve` → `enumerate` for literal saved groups and exhaustive tag actions;
keyword overlap or semantic similarity alone does not establish membership.

Check whether the retrieved evidence actually supports the question before answering.
If the shortlist contains only weak associations, try up to two meaning-preserving
reformulations for supporting activities or events and compare the combined people
by canonical ID. Do not accept the top vector hit as an answer or interpret a poor
shortlist as absence. Use lexical reformulations if fresh embeddings lack approval.

Compare each person's `evidence` AND `context`, not just their relationship summary.
These are candidate evidence bundles, not final answers or confidence scores.
Read relevant events, owner scope, dates, human corrections and contradictory
claims together. A dated past relationship does not establish a current one;
an unspecified relationship summary does not negate specific event evidence.
Do not infer relationships from co-ownership, names or topic similarity alone.
When multiple independent facts support an inference, state it with citations.

If `context_truncated` is true or the relationship remains ambiguous, `open` supporting
claim IDs and read their saved dossier; use person-filtered `query` for additional
relevant evidence. Preserve canonical identities and do not write an inferred
relationship back into the source. Return the best-supported person first, explain
why, and distinguish historical or uncertain alternatives. This applies equally
to personal relationships, professional expertise and other remembered connections.

## Answer with evidence

Open the relevant claim IDs before making precise assertions. Inspect saved
content, source hash, active state and read errors. Do not present a retracted
claim as current, a topic mention as expertise, a naming association as residence,
or retrieval rank as relationship strength. Human corrections take precedence.
Recent indexing does not make old source evidence recent.

Lead with the answer. Put a concise source link immediately after the supported
sentence; for a block quote, put the source directly underneath. Quote only
verbatim text verified in `saved_content`, labelled as a dossier/profile excerpt
when that is the source—not as an original message.

Citations currently identify saved artifacts, not a memory-viewer URL. Use an
actual returned HTTP URL or an existing local artifact path when available.
SQLite `file:...#facts/...` references are row locators, not working webpage
links: show a short reference such as “saved dossier, claim 42” and retain its
exact database locator, hash and claim ID for `open` follow-ups. Never invent a
clickable route or original-message link. `original_message_citation_verified`
is false in the current reader. If the user asks for original messages, use the
existing authorized dossier/logbook workflow; memory alone cannot verify them.

For groups, show canonical people count, names and the reason for membership.
Separate literal tags, inferred labels and factual locations when they differ.
For inferred connections, state the inference and its supporting evidence.
