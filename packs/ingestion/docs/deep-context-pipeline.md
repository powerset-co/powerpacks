# Deep-context pipeline

`$deep-context` turns a person's local conversation history into a searchable
Markdown dossier, finds likely duplicate identities, and checks whether the
LinkedIn profile attached during ingestion is actually the same person.

This guide explains the product and trust boundaries. The executable contract is
the [`deep-context` skill](../skills/deep-context/SKILL.md); the primitives remain
the authority for schemas and CLI behavior.

## At a glance

- **Input:** `.powerpacks/network-import/merged/people.csv`, local msgvault Gmail,
  macOS Messages, and an optional local wacli store.
- **Core output:** one synthesized Markdown dossier per person, plus lookup indexes
  for name, phone, and email.
- **Reasoning:** OpenAI extracts facts, judges duplicate pairs, and checks attached
  LinkedIn profiles. Optional Parallel research looks for a replacement identity.
- **Human control:** core OpenAI and Parallel stages have measured previews and
  current-run approval. RapidAPI cache misses and Modal require explicit disclosure
  and approval but do not yet have equivalent measured child gates. Browser review
  is a hard stop before decisions are realized.
- **Privacy exception:** this skill intentionally reads message bodies. Direct
  messages are the default; small iMessage group bodies require explicit opt-in.

## Architecture

```mermaid
flowchart TD
    A["$deep-context request"] --> B{"Requested path"}
    B -->|lookup, check, validate| C["Free local command<br/>use existing artifacts"]
    B -->|review| D["Open local review UI<br/>actions update review.csv"]
    B -->|full build| E{"Owner profile cached?"}
    E -->|Yes| E2["Build owner context<br/>from local cache"]
    E -->|No| E0["Approve RapidAPI<br/>owner lookup"]
    E0 --> E1["Hydrate owner profile<br/>through RapidAPI"]
    E1 --> E2

    E2 --> F["Check Gmail, iMessage,<br/>WhatsApp, and keys"]
    F --> G{"Approve collection scope"}
    G -->|default| H["Collect Gmail + DM bodies<br/>and iMessage group names"]
    G -->|explicit group opt-in| I["Also collect bodies from<br/>small iMessage groups"]
    H --> J["Ephemeral raw bundles"]
    I --> J

    J --> K["Preview and approve<br/>OpenAI synthesis"]
    K --> L["Adaptive fact extraction<br/>one checkpoint per person"]
    L --> M["Compose Markdown dossiers<br/>and lookup index locally"]
    M --> N["Validate completeness"]

    N --> O["Preview and approve<br/>duplicate judging"]
    O --> P["OpenAI judges plausible pairs<br/>with facts + message samples"]
    P --> P0["Inspect accepted-edge audit<br/>before creating parents"]
    P0 --> Q["Build canonical parent dossiers<br/>from connected components"]

    Q --> R["Preview and approve<br/>LinkedIn reconciliation"]
    R --> S["OpenAI compares facts + message samples<br/>with attached LinkedIn"]
    S --> T["Write durable proposed actions<br/>review.csv"]
    T --> U{"Optional identity recovery"}
    U -->|approved| V["Parallel public-web research"]
    U -->|skip| D
    V --> W["Propose a retarget or explicitly<br/>requested synthetic profile"]
    W --> D

    D --> X{"User finishes review"}
    X --> X1{"Approved retarget needs<br/>RapidAPI cache miss?"}
    X1 -->|Yes| X2["Approve RapidAPI<br/>retarget hydration"]
    X2 --> X3["Hydrate approved retarget"]
    X3 --> Y["Fan in reviewed overrides locally"]
    X1 -->|No| Y
    Y --> Z0["Approve Modal upload<br/>and provider processing"]
    Z0 --> Z["Upload merged people.csv<br/>to workspace-shared Modal volume"]
    Z --> AA["Build and download<br/>local-search.duckdb"]

    classDef gate fill:#fff4d6,stroke:#a66b00,color:#3d2a00,stroke-width:2px;
    classDef local fill:#eaf5ff,stroke:#2878a8,color:#14364a;
    classDef cloud fill:#fff0ee,stroke:#b54c3d,color:#4a1f19;
    classDef output fill:#eef8ed,stroke:#4f8a49,color:#233f20;
    class E,E0,G,K,O,R,U,X,X1,X2,Z0 gate;
    class C,D,E2,F,H,I,J,M,N,P0,Q,T,Y local;
    class E1,L,P,S,V,X3,Z cloud;
    class AA output;
```

Approval nodes are wait points on the normal path, not separate failure states.
If a provider call is not approved, execution simply does not continue past that
node; the repeated stop branches are omitted to keep the product flow readable.

The all-in-one `bin/deep-context run` shortcut is intentionally disabled. A
single process cannot pause, show each core model stage's measured estimate, and
obtain independent current-run approvals safely. Operators use the staged
commands from `SKILL.md` instead.

## Stage walkthrough

| Stage | What it does | Where it runs | Main result |
| --- | --- | --- | --- |
| Owner context | Loads the operator's school, work, and location history so shared context can disambiguate contacts. A cache miss sends the LinkedIn URL to RapidAPI after approval. | Local cache or RapidAPI. | `owner.json` |
| Readiness | Confirms the merged network, available message sources, Full Disk Access, and OpenAI key. Partial-source runs are allowed. | Local. | Readiness JSON |
| Collection | Streams one person at a time from msgvault, Messages, and wacli. `--deep-cap` applies independently to Gmail, iMessage DM, WhatsApp DM, and optional group pools; a character safety cap bounds the combined bundle. | Local, read-only source access. | `raw/<person_id>.json` |
| Synthesis | Sends bounded message samples plus owner context to OpenAI. It deepens until confidence, saturation, exhaustion, or the batch limit. | OpenAI. | `facts/<person_id>.jsonl` |
| Composition | Deterministically renders facts into dossiers and indexes names, phones, and emails. | Local. | `dossiers/*.md`, `index.json`, `index.md` |
| Duplicate merge | Generates plausible pairs locally, then asks OpenAI using structured facts and short verbatim message samples. Accepted edges at `--confidence` (default `0.70`) form transitive clusters; inspect the edge audit before parent construction because the later UI cannot split them. | Local plus OpenAI. | `merge-candidates.csv`, `merge-verdicts.csv`, `parents/*.md` |
| LinkedIn self-heal | Compares facts and short verbatim message samples with each attached profile, then proposes verify, detach, retarget, exclude, or review actions. It does not edit `people.csv`. | Local plus OpenAI. | `reconcile/*`, `overrides/review.csv` |
| Identity recovery | Researches explicitly approved, model-recommended unresolved identities. The default finds retargets. Researching plausibly absent LinkedIns and assembling synthetic profiles requires explicit `--include-plausibly-absent` opt-in. User-touched rows are excluded because the current override schema cannot hold a sticky decision and a pending retarget simultaneously. | Parallel.ai, then local cache/RapidAPI for approved retargets. | `reconcile/deep-research/*`, override CSVs |
| Review | Joins dossiers, verdicts, profiles, and durable decisions. Keep, detach, fix, exclude, reset, and synthetic decisions autosave. | Local browser. | Updated override CSVs |
| Realization | Fan-in reapplies approved overrides and consolidates contact fields. A separate Modal build creates the downloadable local search index in a workspace-shared volume with operator-prefixed paths and shared caches. | Local, then Modal. | Merged `people.csv`, local DuckDB |

## What leaves the machine

| Boundary | Data sent | Not sent |
| --- | --- | --- |
| OpenAI synthesis | Sampled message text, message metadata needed for context, and owner context. With explicit `--include-groups`, this may include small iMessage group bodies. | Unselected messages and raw source databases. |
| OpenAI duplicate judge | Structured facts, identity evidence, and short verbatim message samples for each plausible pair. | Unrelated people and full source databases. |
| OpenAI LinkedIn judge | Parent facts, owner context, short verbatim message samples, and cached LinkedIn profile evidence. | Unrelated people and full source databases. |
| Parallel.ai | Display name, primary email, phone, source channel, dossier-derived relationship/work/school/location/topics, and the rejected LinkedIn URL plus reason for approved unresolved people. | Raw message bodies. |
| RapidAPI | A LinkedIn URL needing profile hydration. | Gmail or chat content. |
| Modal | The canonical merged people CSV, including contact and interaction fields. The volume is workspace-shared; inputs/runs are operator-prefixed, caches are shared, and a missing operator ID uses the all-zero path. | Raw msgvault, Messages, wacli, and deep-context raw bundles. |

Raw bundles are gitignored but not self-deleting. Duplicate judging, parent
construction, and LinkedIn reconciliation still read them. Run
`bin/deep-context purge-raw` only after those stages and any debugging are finished;
purging earlier removes the later judges' verbatim samples.
Dossiers contain synthesized facts rather than verbatim messages.

## Decisions and review

`reconcile` writes suggestions rather than mutating the network. The durable
table is `.powerpacks/network-import/overrides/review.csv`.

- `approved=auto` is a high-confidence machine decision applied by fan-in.
- `approved=yes` or `approved=no` is a sticky human decision.
- Blank approval is pending.
- A retarget is not materialized until it is approved and `apply-retargets` has
  hydrated the replacement profile.
- Synthetic profiles at completeness `>= 0.6` are `approved=auto` and will merge
  unless rejected during review; lower-completeness synthetic rows are pending.
- Review page loading is free of provider calls, but the page is not read-only:
  decisions save locally and some merged-record resolution is applied by the UI.

The user must explicitly finish review before retarget application, fan-in, or
index rebuild continues, including review of auto-approved synthetic rows.

## Artifacts and resume

```text
.powerpacks/deep-context/
|-- owner.json
|-- raw/<person_id>.json
|-- facts/<person_id>.jsonl
|-- dossiers/<slug>.md
|-- index.json
|-- index.md
|-- merge-candidates.csv
|-- merge-verdicts.csv
|-- parents/<slug>.md
`-- reconcile/
    |-- verdicts.jsonl
    |-- verdicts.csv
    |-- summary.md
    |-- applied.csv
    `-- deep-research/

.powerpacks/network-import/overrides/
|-- review.csv
|-- consolidate-people.csv
|-- retarget-people.csv
`-- synthetic-people.csv
```

Collection and synthesis resume at person granularity. Collection reuses a raw
bundle only when its stored group/cap policy matches the request. When a default
collection follows a group-enabled or legacy manifest, it deletes the old raw
bundle files by filename without deserializing their text, then rebuilds DM-only
bundles. A narrowed `--person`/`--limit` run refuses that transition because it
cannot safely rebuild the whole prior scope.
Use `--force` on both stages to include newly arrived messages for existing
people. `bin/deep-context dry` only estimates synthesis from existing bundles; it
never re-collects or changes their privacy scope. Duplicate judging and LinkedIn
reconciliation do not checkpoint individual model calls, so an interrupted stage
may repeat work. Lookup, check,
validate, and opening an existing review are independent free paths.

## Current product gaps

- Group-body access and retained group-message counts are reported in the
  collection manifest, but raw bundles still require a manual purge.
- Parent construction intentionally includes every member connected by a
  duplicate edge at the configured threshold. There is no separate cluster-level
  human gate before LinkedIn review.
- A user-detached LinkedIn cannot currently receive an automatic pending retarget;
  the one-row override schema cannot represent both decisions safely.
- A synthesis failure can leave an empty fact checkpoint that requires
  `--force` to retry.
- `messages_available` is exact for Gmail and iMessage DMs but post-cap for
  WhatsApp and opted-in group bodies, so `people_capped` can under-report them.
- Owner and retarget RapidAPI misses and Modal indexing lack measured child-level
  previews; the skill therefore adds explicit disclosure/approval boundaries.
- The final `index-people` path uploads contact metadata to Modal and currently
  uses uncapped internal provider mode when `--max-usd 0` is left unchanged. See
  the [LinkedIn and Modal indexing guide](../../indexing/docs/linkedin-modal-pipeline.md).
- Modal storage is workspace-shared and falls back to an all-zero operator path
  unless `POWERPACKS_OPERATOR_ID` is set; automatic per-user isolation is not shipped.

## Implementation map

| Concern | Authority |
| --- | --- |
| Agent workflow and approvals | [`deep-context/SKILL.md`](../skills/deep-context/SKILL.md) |
| Command dispatcher | [`bin/deep-context`](../../../bin/deep-context) |
| Collection and provenance | [`collect_person_context.py`](../primitives/deep_context/collect_person_context.py) |
| Per-source body readers | [`sources.py`](../primitives/deep_context/sources.py) |
| Synthesis | [`synthesize_person_context.py`](../primitives/deep_context/synthesize_person_context.py) |
| Duplicate judge | [`cluster_merge_candidates.py`](../primitives/deep_context/cluster_merge_candidates.py) |
| LinkedIn self-heal | [`reconcile_linkedin.py`](../primitives/deep_context/reconcile_linkedin.py) |
| Review UI | [`reconcile_review_web.py`](../primitives/deep_context/reconcile_review_web.py) |
| Optional recovery | [`reconcile_deep_research.py`](../primitives/deep_context/reconcile_deep_research.py) |
| Fan-in realization | [`index_contacts_pipeline.py`](../../indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py) |
