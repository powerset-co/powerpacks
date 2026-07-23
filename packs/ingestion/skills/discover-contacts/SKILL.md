---
name: discover-contacts
description: Discover local LinkedIn CSV, msgvault email, and Twitter source artifacts. Use for $discover-contacts. Route iMessage and WhatsApp to $import-messages instead.
---

# discover-contacts

Use this skill for `$discover-contacts` source discovery.

## User-facing tone

Be clear and calm. The user should hear what is happening in product terms, not
pipeline jargon. Do not normally say fan-out, fan-in, ledgers, artifacts,
sub-agents, or DuckDB. Use those terms only in internal execution notes or if the
user asks for technical details.

Use this style instead:

```text
I found these connected sources:
- Gmail: 2 accounts
- LinkedIn: Connections.csv

I’m going to discover each source in parallel where possible. Import/enrichment
and indexing run as separate stages.

I won’t upload anything automatically. I’ll only stop if I need a browser login,
a QR/device link, an overwrite approval, or approval for a paid provider step.
```

For progress updates, report user-visible progress and counts:

```text
Gmail discovery is running for 2 accounts. LinkedIn discovery is also running.
```

```text
Discovery finished. Next run import/enrichment, then indexing.
```

If a provider/spend step blocks, explain the choice plainly:

```text
I found contacts that need paid LinkedIn/profile enrichment before I can improve
their profiles. I won’t run that automatically. Do you want me to approve this
step, skip it for now, or continue with only the local data?
```

## After Onboarding

If `$onboard` has linked sources, propose the concrete import command and ask
for one confirmation before long sync/import work:

```text
Your sources are connected. I can now discover local source contacts. Large
mailboxes or large networks can take a while. I won’t upload anything
automatically. Continue?
```

After confirmation, run discovery until it completes or reaches a real approval
confirmation. Do not ask again for routine local metadata work.

## Inputs

- `--linkedin-csv <Connections.csv>` and `--linkedin-source-user <label>` for LinkedIn.
- `--gmail-account-email <email>` / repeated `--gmail-account-emails <email>` and optional `--msgvault-db <path>` for email/msgvault.
  The Gmail import worker owns `msgvault sync-full` for each selected account
  before reading the local msgvault DB. Do not run this sync from onboarding.
  Use `--skip-msgvault-sync` only for tests or known pre-synced local DBs.
- `--from-accounts .powerpacks/ingestion/accounts.json` or `--from-setup .powerpacks/setup/setup-run.json` to consume link-only state from `$setup` / `$onboard`.
- `--include-existing-artifacts` is legacy and should not be used for merge.

iMessage and WhatsApp are intentionally outside this generic runner. Route either
source to `$import-messages`; do not consume an existing Messages artifact here.

## Command

```bash
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run \
  --from-accounts .powerpacks/ingestion/accounts.json
```

`run --dry-run --from-accounts ...` reports source discovery jobs with
`parallelizable: true/false`. LinkedIn, Gmail/msgvault accounts, approved
Twitter imports have no cross-source dependency, so this skill may dispatch them
in parallel sub-agents. `$setup` remains LinkedIn-only and `$import-messages`
exclusively owns iMessage/WhatsApp. Merge and indexing are owned by
`index_contacts_pipeline.py`, not this skill.

That paragraph is for execution planning. Do not repeat it to the user. Say:

```text
These sources can be discovered at the same time, so I’ll run them in parallel.
```

For manual source discovery, run workers with `--only-source`; each source
writes to its fixed `.powerpacks/network-import/discover/<source>/` folder. Do
not approve RapidAPI/Parallel/OpenAI spend confirmations inside workers; return
those confirmations to the main thread.

## Restore Prior Checkpoints

When existing operator export/checkpoint CSVs are available, first generate a
local restore bundle:

```bash
uv run --project . python packs/ingestion/primitives/setup/bootstrap_network_from_exports.py generate \
  --operator-mapping <operator_mapping.json> \
  --source-dir <existing-export-csv-dir> \
  --operators <operator-slug> \
  --linkedin-csv <Connections.csv> \
  --gmail-account-email <email> \
  --seed-profile-cache \
  --force
```

Then run the command printed in
`.powerpacks/network-bootstrap/operators/<operator-slug>/outputs/commands.txt`.

Resume after a child approval confirmation:

```bash
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py approve
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py continue
```

## Long Runs

Run the command in a visible shell and keep `[discover-contacts]` /
`[enrich-people]` progress lines visible. For large mailboxes or profile sets,
use the status command every few minutes instead of treating silence as failure:

```bash
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py status \
  --ledger .powerpacks/network-import/discover/ledger.json
```

RapidAPI LinkedIn hydration now runs directly when a RapidAPI key is configured.
If the pipeline blocks on another spend-bearing step, show the count and ask for
approval before running `approve`.

## Outputs

Discovery writes stable per-source artifacts only:

```text
.powerpacks/network-import/discover/gmail/contacts.csv
.powerpacks/network-import/discover/gmail/linkedin_resolution_queue.csv
.powerpacks/network-import/discover/linkedin/contacts.csv
```

Import/enrichment owns `directory.csv` and source `people.csv` outputs. Indexing
owns merged `people.csv` and the `local-search.duckdb` search artifact.

Do not upload automatically. Report artifact paths and counts only.
