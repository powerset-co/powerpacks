# Powerpacks product documentation

This is the GitHub-rendered entry point for understanding Powerpacks. Start
with the product guide, then follow the operational or implementation links
only when you need that level of detail.

## Start here

| Topic | Document | Use it for |
| --- | --- | --- |
| People search | [`$search` architecture](../packs/search/docs/search-architecture.md) | Product walkthrough of routing, standard (`depth: fast`) search, deep recruiter search, review points, data boundaries, outputs, and roadmap. |
| LinkedIn setup and indexing | [LinkedIn and Modal indexing pipeline](../packs/indexing/docs/linkedin-modal-pipeline.md) | How a LinkedIn `Connections.csv` becomes the local DuckDB queried by `$search local`. |
| Gmail import | [Gmail import pipeline](../packs/ingestion/docs/gmail-import-pipeline.md) | How bounded msgvault sync, metadata extraction, directory reuse, LinkedIn lookup, hydration, fan-in, and indexing work. |
| iMessage and WhatsApp import | [Message import pipeline](../packs/ingestion/docs/message-import-pipeline.md) | Source extraction, matching, provider payloads, human review, and the Modal boundary. |
| Post-import processing | [Deep-setup pipeline](../packs/ingestion/docs/deep-setup-pipeline.md) | The layer after imports: dossiers over people + candidates, yes/maybe/no network-worth triage, the single reverse lookup, review, and the index rebuild. |
| Deep relationship context | [Deep-context pipeline](../packs/ingestion/docs/deep-context-pipeline.md) | How message bodies become dossiers, duplicate clusters, LinkedIn self-heal decisions, and reviewed network overrides. |
| Running deep search | [Deep-mode runbook](../packs/search/skills/search/deep-mode.md) | Exact operator commands, artifacts, approval boundary, and resume rules. |
| Running setup | [`$setup` skill](../packs/ingestion/skills/setup/SKILL.md) | Exact LinkedIn-only setup checklist. |
| All skills | [Root skill index](../README.md#skills) | GitHub-native list of supported skill entry points. |
| Generated skills map | [`skills-map.html`](skills-map.html) | Interactive inventory to open locally. On GitHub this link shows the tracked HTML source until Pages is enabled. |

## How the documents fit together

```mermaid
flowchart TD
    HOME[Product documentation] --> SEARCH[Search product guide]
    HOME --> INDEX[LinkedIn and Modal indexing guide]
    HOME --> GMAIL[Gmail import guide]
    HOME --> MESSAGES[iMessage and WhatsApp guide]
    HOME --> DEEPSETUP[Deep-setup guide]
    HOME --> CONTEXT[Deep-context guide]
    SEARCH --> SEARCHSKILL[Search execution contract]
    SEARCH --> DEEP[Deep-mode runbook]
    SEARCH --> CONTRACTS[Search schemas and data contracts]
    INDEX --> SETUP[Setup execution contract]
    INDEX --> CODE[Indexing driver and pipeline code]
    GMAIL --> GMAILSKILL[Import-gmail execution contract]
    GMAIL --> DEEPSETUP
    MESSAGES --> MESSAGESKILL[Import-messages execution contract]
    MESSAGES --> DEEPSETUP
    DEEPSETUP --> DEEPSETUPSKILL[Deep-setup execution contract]
    DEEPSETUP --> INDEX
    CONTEXT --> CONTEXTSKILL[Deep-context execution contract]
    CONTEXT --> INDEX

    classDef home fill:#0f3d3e,color:#ffffff,stroke:#0f3d3e,stroke-width:2px;
    classDef guide fill:#e8f3f1,color:#102a2a,stroke:#2f6f6d,stroke-width:2px;
    classDef detail fill:#fff7e6,color:#3d2b0f,stroke:#b7791f;
    class HOME home;
    class SEARCH,INDEX,GMAIL,MESSAGES,DEEPSETUP,CONTEXT guide;
    class SEARCHSKILL,DEEP,CONTRACTS,SETUP,CODE,GMAILSKILL,MESSAGESKILL,DEEPSETUPSKILL,CONTEXTSKILL detail;
```

The product guides explain what the system does and why. `SKILL.md` files are
the executable agent instructions. Schemas, contracts, and CLI behavior are
the final authority when implementation details matter. Retired implementation
plans remain available in Git history instead of appearing beside maintained
product documentation.

## GitHub, Wiki, and Pages

The Markdown in this repository is canonical by design:

- GitHub renders these pages and their Mermaid diagrams directly.
- Every documentation change is versioned with the code and reviewed in the
  same pull request.
- Relative links work on branches, in pull requests, and at a historical
  commit.

The repository Wiki feature is enabled but currently uninitialized and empty.
A Wiki is a separate Git repository with a separate publishing path. It cannot
participate naturally in the same pull-request review as the code, so a
manually maintained Wiki would create a second source of truth. If the Wiki tab
is desired later, initialize it once and generate a mirror from selected files
after merges rather than editing it by hand.

GitHub Pages is currently disabled for this repository. It is possible as a
future presentation layer, but this repo would need the Pages setting plus a
build that collects canonical files from `packs/` and renders Mermaid. Pages
should be a generated view, not the source of truth.
