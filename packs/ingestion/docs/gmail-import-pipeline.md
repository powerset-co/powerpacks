<!--
Changelog:
- 2026-07-23: Gmail discovery account selection is `--account-email` (repeatable)
  only — the `--accounts`/accounts-file path and the `discover()` wrapper were
  dropped from the primitive (callers construct `GmailDiscovery(...).run()`).
  Corrected the Bounded-sync stage row path `gmail.py discover` →
  `gmail/discover.py discover`.
- 2026-07-23 (audit): gmail/msgvault_store.py split into the gmail/msgvault/
  package (store.py = MsgvaultStore + SQL, util.py = pure helpers) and
  gmail/sync.py moved to gmail/msgvault/sync.py; the component table now links
  the new paths.
- 2026-07-23 (audit batch 17): gmail/network_import.py was split into
  gmail/msgvault_store.py (msgvault reader/aggregation) and
  gmail/discover_engine.py (per-account artifact-emission CLI).
- 2026-07-23: The powerpacks-console app and its setup_gmail.py engine were
  deleted; the harness skill is now the only Gmail import surface.
- 2026-07-23: Removed the --resolve-legacy / --approve-parallel-spend flags.
  The import is directory-only, period; stored legacy resolutions migrate into
  overrides/review.csv via `bin/deep-context migrate-legacy` (the central SOT),
  and all new resolution/enrichment runs through $deep-context's judged stages.
- 2026-07-16: Refocused on contact sync only. The import stage is now
  directory-reuse only (free, local): unresolved contacts land in
  import/gmail/candidates.csv, Parallel.ai resolution + RapidAPI hydration
  move to the $deep-context processing layer, and Modal indexing is no longer part of $import-gmail (it
  stays in $setup and in $deep-context's finale).
-->

# Gmail import pipeline

`$import-gmail` adds Gmail relationship metadata to the local Powerpacks
network. Gmail is synced into msgvault, Powerpacks reads metadata from the
local archive, identities already known to the local directory are reused, and
every still-unresolved contact worth researching is staged in a candidates
pool for the `$deep-context` processing layer. The import stage is free and
local: no Parallel.ai, no RapidAPI, and no Modal index build.

This guide describes the product behavior and trust boundaries. The executable
agent contract is [`import-gmail/SKILL.md`](../skills/import-gmail/SKILL.md).

## At a glance

- **Gmail content boundary:** msgvault downloads messages into its local archive
  for the selected window and may also download attachments. Powerpacks then
  queries only participant and interaction metadata; it does not select bodies,
  subjects, snippets, raw MIME, or attachment content.
- **Identity strategy:** local directory only. People resolved by prior imports
  attach immediately; unresolved and cached-negative contacts are written to
  `import/gmail/candidates.csv` for `$deep-context`, which owns Parallel.ai
  resolution and RapidAPI hydration. Stored legacy resolutions are adopted
  into `overrides/review.csv` by `bin/deep-context migrate-legacy`.
- **Output:** `.powerpacks/network-import/import/gmail/people.csv` plus
  `import/gmail/candidates.csv`, merged into the shared network by fan-in.
- **Indexing:** no longer part of `$import-gmail`. The Modal index build stays
  in `$setup` and in `$deep-context`'s finale; new Gmail contacts become
  searchable after one of those runs.
- **Cloud boundary:** none in the import, ever. Provider calls and the Modal
  upload happen only in `$deep-context`.

## Architecture

```mermaid
flowchart TD
    A["Choose Gmail accounts<br/>and history window"] --> B["Set up local msgvault<br/>and OAuth desktop app"]
    B --> C["Authorize every selected account"]
    C --> D["msgvault syncs selected window<br/>into its local message archive"]
    D --> E["Powerpacks reads metadata only<br/>names, emails, roles, IDs, dates, labels"]
    E --> F["Filter automation and one-way contacts<br/>plus categories when label tables exist"]
    F --> G["Write per-account artifacts<br/>and stable discovery manifest"]

    G --> H["Reuse local directory mappings<br/>exact email/phone or unambiguous name"]
    H -->|Resolved| R["Canonical Gmail people.csv"]
    H -->|Unresolved or cached-negative| Q["import/gmail/candidates.csv<br/>research pool for $deep-context"]

    R --> S["Local fan-in across sources<br/>merged people.csv + provenance CSVs"]
    S --> T["Suggest missing sources<br/>offer $deep-context processing"]

    Q -. "identity research, review, indexing" .-> X["$deep-context processing layer<br/>(Parallel.ai + RapidAPI + Modal)"]
        Y -.-> R

    classDef local fill:#eaf5ff,stroke:#2878a8,color:#14364a;
    classDef cloud fill:#fff0ee,stroke:#b54c3d,color:#4a1f19;
    classDef output fill:#eef8ed,stroke:#4f8a49,color:#233f20;
    class A,B,C,D,E,F,G,H,Q,R,S local;
    class X,Y cloud;
    class T output;
```

## Stage walkthrough

| Stage | What happens | Product consequence |
| --- | --- | --- |
| Account choice | The user selects every Gmail address and a history window. Default is three years; a wider window needs confirmation. | Selection is explicit rather than inferred. |
| OAuth and authorization | msgvault's desktop OAuth app is created if missing. Every selected address absent from `status.accounts` is authorized, including the primary account. | Existing OAuth configuration does not imply a new account is authorized. |
| Bounded sync | All selected accounts are passed to one `gmail/discover.py discover` invocation with repeated `--account-email` flags and one `--sync-after`. | Separate per-account calls can rewrite the stable manifest and lose earlier accounts from the following import. |
| Metadata extraction | msgvault first synchronizes messages into its local full-message archive. Powerpacks opens that SQLite database read-only and selects participants, direction, message/conversation IDs, timestamps, labels, counts, and display names. | Powerpacks does not select body, subject, MIME, or attachment content, although msgvault's local store contains message bodies and may contain attachments. |
| Filtering | Automated/service addresses and contacts without bidirectional interaction are removed. Default category labels are also removed when both msgvault label tables exist. | The queue favors actual person-to-person relationships; missing label tables weaken category filtering rather than failing closed. |
| Directory lookup | Gmail observations update the reusable local directory. Exact email, phone, or unambiguous unique-name mappings at confidence `>= 0.75` are reused; cached negative outcomes are not retried. | Known people attach immediately with no provider call. |
| Candidates staging | Post-directory unresolved queues and cached-negative queues are unioned by email into `import/gmail/candidates.csv` (cached negatives flagged in `evidence`). | Every contact worth researching waits for `$deep-context`; nothing is looked up in-import and nothing is silently dropped. |
| Source fan-in | Duplicate LinkedIn IDs across Gmail accounts and other sources merge; email aliases and interaction fields are unioned. | One canonical person can carry evidence from several imports. |
| Suggest & process tail | A read-only status check reports which sources are imported and how many candidates wait per source, then offers `$deep-context`. | Indexing is not part of this skill; the Modal build stays in `$setup` and in `$deep-context`'s finale. |

## Identity lookup details

The canonical import (contract `gmail-directory-only-v2`) is directory-reuse
only:

1. Commit the latest Gmail observations to
   `.powerpacks/network-import/directory.csv`.
2. Reuse a positive directory mapping by exact email/phone or unambiguous name.
3. Keep cached-negative identities out of repeated provider calls.
4. Filter generic/non-person addresses.
5. Write every still-unresolved contact — including the cached negatives,
   flagged with `cached_negative` evidence — to `import/gmail/candidates.csv`
   (`candidate_key` is `email:<addr>`). `$deep-context` researches each
   candidate once, with cross-channel context, in a judged and user-reviewable
   flow.

### Legacy resolutions: migrated, never replayed via flags

The old in-import Parallel behavior (per-email lookup, results accepted at
`>= 0.75` with no identity judge or human review) is REMOVED — there is no
flag that restores it. Its stored outputs still exist and are handled in
`$deep-context`: `bin/deep-context migrate-legacy` adopts every
still-unverified stored link as a pending `retarget` proposal in
`overrides/review.csv` (the central source of truth the fan-in and the review
flow already read), where the retarget judge, auto-stand rules, and the
Check-LinkedIn queue finally audit them.

## Privacy and provider boundaries

| System | Data it receives or stores | Boundary |
| --- | --- | --- |
| msgvault | Gmail OAuth tokens and a local full-message archive under `~/.msgvault`; the current skill does not request attachment suppression, so supported msgvault builds may download attachments. | Owned by msgvault on the user's machine. Powerpacks does not copy secrets into tracked files or send archive content to identity providers. |
| Powerpacks metadata reader | Emails, names, sender/recipient roles, IDs, dates, labels, and aggregate counts. | Opens msgvault read-only; excludes bodies, subjects, snippets, raw MIME, and attachments. |
| Local directory | Contact observations, identity mappings, confidence, and cached negative outcomes. | Local `.powerpacks` artifact reused across imports. |
| Parallel.ai | Full name, email, an email-domain-derived company guess, and optional context. | Not called by the canonical import — `$deep-context` owns this boundary. No Gmail body or subject content. |
| RapidAPI | Accepted LinkedIn URL/public identifier. | Not called by the canonical import — `$deep-context` owns this boundary. No Gmail content. |
| Modal | Full merged `people.csv`, including Gmail addresses and interaction metadata. | Not part of `$import-gmail`; the index build happens in `$setup` and in `$deep-context`'s finale. |

After OAuth, the canonical `$import-gmail` run stays on-device: msgvault talks
to Gmail, and everything else is local file processing.

Before any mailbox sync, the workflow runs one zero-download OAuth health probe
for every selected account (`msgvault_setup.py auth-check`). Stored account
presence is not treated as proof that Google still accepts the refresh token.
The probe aggregates every missing/expired account, the agent asks once before
opening those browser grants sequentially, and the full selected set is checked
again before the bounded sync starts. Network/DNS/Google 5xx failures remain
transient errors and never trigger forced reauthorization.

## Artifacts and resume

```text
.powerpacks/network-import/
|-- discover/gmail/<account>/
|   |-- accounts.csv
|   |-- gmail_threads.csv
|   |-- gmail_contacts_aggregated.csv
|   |-- targeted_emails.csv
|   |-- linkedin_resolution_queue.csv
|   |-- people.csv
|   `-- manifest.json
|-- discover/gmail/
|   |-- contacts.csv
|   |-- linkedin_resolution_queue.csv
|   `-- manifest.json
|-- directory.csv
|-- import/gmail/
|   |-- people.csv
|   |-- candidates.csv
|   `-- manifest.json
`-- merged/people.csv
```

`~/.msgvault/msgvault.db` is durable and must not be deleted. With an explicit
history window, discovery passes `--noresume`, rescans that window, and relies on
msgvault deduplication for already stored messages. Without an explicit window,
the primitive may infer `--after` from the most recent local message. The
import manifest records the `gmail-directory-only-v2` contract; an unchanged
input is a fingerprinted no-op (`--force` reruns anyway). Stored Parallel resolver
output CSV rows are applied as raw material; their audit lives in
`overrides/review.csv` after `bin/deep-context migrate-legacy`.

## Current product gaps

- The `$deep-context` processing layer (candidate research, review, indexing)
  lands in a companion PR; until then candidates wait in
  `import/gmail/candidates.csv`, and directory-resolved contacts become
  searchable only after the next index rebuild.
- Stored legacy resolutions were accepted without an identity judge or human
  review — run `bin/deep-context migrate-legacy` so they enter the judged
  review loop.
- The harness skill (`$import-gmail`) is the single Gmail import surface; the
  former console app endpoints and their `setup_gmail.py` engine were removed
  on 2026-07-23.

## Implementation map

| Concern | Authority |
| --- | --- |
| Agent workflow | [`import-gmail/SKILL.md`](../skills/import-gmail/SKILL.md) |
| OAuth and account status | [`msgvault_setup.py`](../primitives/setup/msgvault_setup.py) |
| Sync and stable discovery | [`gmail/msgvault/sync.py`](../primitives/discover/gmail/msgvault/sync.py) |
| Metadata aggregation | [`gmail/msgvault/store.py`](../primitives/discover/gmail/msgvault/store.py) (SQL + `MsgvaultStore`) and [`gmail/msgvault/util.py`](../primitives/discover/gmail/msgvault/util.py) (pure helpers) |
| Per-account artifact emission | [`gmail/extract_gmail.py`](../primitives/discover/gmail/extract_gmail.py) |
| Import orchestration | [`imports/gmail/importer.py`](../primitives/imports/gmail/importer.py) |
| Directory reuse | [`imports/directory.py`](../primitives/imports/directory.py) |
| Candidates schema | [`candidates_schema.py`](../schemas/candidates_schema.py) |
| Per-source status | [`status.py`](../primitives/imports/status.py) |
| Profile hydration (legacy era; not callable from the import) | [`enrich_people.py`](../primitives/enrich/enrich_people.py) |
| Fan-in | [`index_contacts_pipeline.py`](../../indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py) |
