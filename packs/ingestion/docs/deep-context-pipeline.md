# Deep-context pipeline

Created: 2026-07-13

Changelog:
- 2026-10-07: rewritten for v2. The design is the Deep Context Spec page; this file is the
  short map from the command the agent runs to the blocks and the store. The v1 narrative
  (stages, settlement, state and repeatability) is in git history before this date.

`$deep-context` is the one processing workflow after setup, `$import-gmail` or
`$import-messages`. It turns local conversation history into per-person facts, resolves
duplicate identities, decides who belongs in the network, finds and verifies their LinkedIns,
exports `people.csv` and builds the search index. The agent contract is the
[`deep-context` skill](../skills/deep-context/SKILL.md); the design, every decision and the
store's schema are on the Deep Context Spec page (claude.ai artifact `L2S3gBiu4kVtmzP87YoyvF`).

## One command

```text
bin/deep-context-v2 run      load -> collect -> synthesize -> dedupe -> worth -> enrich -> realize -> index (background) -> Check LinkedIn
bin/deep-context-v2 finish   realize -> index (cached) -> review server stopped
```

Nothing is approved: each paid stage prints its estimate and runs. The user sees the Check
LinkedIn page and nothing else. Every stage keys its work, so a rerun continues from what is
stored and spends nothing on what is done.

## Blocks

| Block | Module | Reads | Writes | Spends |
|---|---|---|---|---|
| 01 Import load | `import_load/load.py` | `network-import/import/<source>/people.csv`, `owner.json`, the Gmail archive's headers | `candidates`, `candidate_identifiers`, `candidate_sources`, `connections`, `owner` | — |
| 02 Collect | `collect/collect.py` | msgvault, `chat.db`, the WhatsApp store | `bundles` | — |
| 03 Synthesize | `synthesize/synthesize.py` | `bundles`, `owner` | `facts` | OpenAI (gpt-6-luna) |
| 05 Dedupe | `dedupe/dedupe.py` | `candidates`, `facts` | `pair_verdicts`, `candidate_parent` | OpenAI (gpt-6.1-sol) |
| 06 Worth | `worth/worth.py` | families, `facts`, `connections` | `worth` | JEV |
| 07 Enrich | `enrich/enrich.py` | families, `connections`, the profile cache | `research`, `candidate_linkedins`, `candidate_parent`, `worth` | Parallel, RapidAPI, JEV, OpenAI |
| 08 Review | `review/server.py` | the views | `candidate_linkedins`, `candidate_parent`, `worth` (human rows) | RapidAPI on a pasted URL |
| 09 Realize | `realize/realize.py` | the views, the profile cache | `network-import/merged/people.csv` | — |
| Index | `packs/indexing/.../index_contacts_pipeline.py` | `people.csv` | `search-index/` | OpenAI |
| Share | `packs/ingestion/primitives/share/share_list.py` | the views, `current_worth` labels, `facts`, `bundles` | `person_labels`, `share` (tags are the page's) | — |

Block 04 is empty by design: a candidate holds one identifier, so the pairs v1's slam dunk
caught are joined at import.

## The store

`.powerpacks/deep-context/deep-context-v2.sqlite`. Ledgers (`candidate_parent`, `worth`,
`candidate_linkedins`, `pair_verdicts`) are append-only; four views (`current_parent`,
`current_worth`, `current_linkedins`, `current_profile`) define the current state for every
reader, a human row above every machine row. Parent ids are `p:<16 hex>` until a LinkedIn is
confirmed, then `li:<member id>`. Stage manifests are under `deep-context/v2-manifests/`.

## What leaves the machine

Message bodies go to OpenAI for synthesis. Facts (never messages) go to JEV for worth and
identity, to Parallel for research, and to OpenAI for the LinkedIn judge. LinkedIn public
identifiers go to RapidAPI for profiles. Nothing is uploaded anywhere else by this pipeline.

## Re-research on the review page

Re-research from a description on the review page runs one Parallel research with the words beside the family's
facts (about $0.05, plus one profile fetch when a URL is found); the pending profile is rejected at once so the
queue advances; a profile found is applied as the Retarget, nothing found leaves the family worth yes without a LinkedIn.
