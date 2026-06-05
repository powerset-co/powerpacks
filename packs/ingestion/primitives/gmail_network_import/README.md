# gmail_network_import

Resumable local Gmail network-import orchestrator.

V1 supports both a one-person local seed and a local msgvault metadata import.
It ports the legacy Gmail contact CSV contracts and header parsing/normalization
logic into Powerpacks, with no runtime dependency on `../aleph-mvp`.

It writes `.powerpacks/` artifacts and exits complete. The msgvault import reads
only local SQLite metadata (`sources`, `participants`, `messages`,
`message_recipients`) and never reads message bodies, subjects, snippets, raw
MIME, or attachments. No Gmail API, DVC, paid APIs, uploads, Harmonic
enrichment, or production source seeding run locally.

## msgvault metadata import

After syncing Gmail with [msgvault](https://github.com/wesm/msgvault), import
email/name interaction metadata from its local SQLite archive:

```bash
uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py msgvault-accounts \
  --db ~/.msgvault/msgvault.db

uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py msgvault \
  --db ~/.msgvault/msgvault.db \
  --account-email me@gmail.com
```

Outputs are written under `.powerpacks/network-import/discover/gmail/<account>/` and
include both legacy Gmail CSV artifacts, `linkedin_resolution_queue.csv`, and
canonical `people.csv` with `primary_email`, `all_emails`, `full_name`, and
`source_channels=gmail_msgvault`. Automated/noreply addresses are filtered by
default; pass `--include-automated` to keep them.

To recover the previous email → LinkedIn → profile-enrichment shape with msgvault
as the sync source, prefer `discover_contacts_pipeline.py run`. The orchestrator
first applies the shared `.powerpacks/network-import/directory.csv` checkpoint,
then runs optional LinkedIn resolution only for unresolved Gmail rows, and
finally delegates resolved LinkedIn rows to `enrich_people.py`.

For manual primitive-level debugging, run LinkedIn resolution over the emitted
queue and apply the results before shared RapidAPI profile enrichment:

```bash
# no spend: prepare harness/manual prompts
uv run --project . python packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py run \
  --provider harness \
  --input .powerpacks/network-import/discover/gmail/<account>/linkedin_resolution_queue.csv \
  --output-dir .powerpacks/network-import/discover/gmail/<account>/linkedin-resolution

# spend-bearing: Parallel.ai, requires approval
uv run --project . python packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py run \
  --provider parallel \
  --input .powerpacks/network-import/discover/gmail/<account>/linkedin_resolution_queue.csv

# after linkedin_resolutions.csv exists, attach LinkedIn URLs to Gmail people
uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py apply-resolutions \
  --people-csv .powerpacks/network-import/discover/gmail/<account>/people.csv \
  --resolutions-csv .powerpacks/network-import/discover/gmail/<account>/linkedin-resolution/linkedin_resolutions.csv

# then hydrate resolved LinkedIn profiles through the shared RapidAPI cache/fetch flow
uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py run \
  --input .powerpacks/network-import/discover/gmail/<account>/resolved/people.csv
```

Powerpacks no longer exposes the old Powerset-hosted Gmail OAuth/sync commands
(`accounts`, `connect`, or backend gmail-sync). Use msgvault for Gmail sync and
then import from the local msgvault DB.

## Legacy one-person seed

The `run` / `continue` / `approve` command contract remains only as a local
one-person seed for deterministic tests/manual fixtures. Do not use it for real
Gmail sync.

## Artifacts

Run artifacts live under `.powerpacks/network-import/discover/gmail/<account>/`:

- `accounts.csv` — local account registry row, supports one run per Gmail account
- `source_contact.jsonl`
- `gmail_threads_<account>_<op>.csv`
- `gmail_contacts_aggregated_<account>_<op>.csv`
- `targeted_emails_<account>_<op>.csv`
- `linkedin_resolution_queue.csv` — email/name/company-guess queue for LinkedIn resolution
- `people.csv` — canonical Powerpacks people artifact for msgvault imports
- `domain_context.json` — local domain/company heuristic, not OpenAI
- `manifest.json`
- `workspace.json`
- `next-steps.json`

## Notes

- Multiple Gmail accounts are modeled as separate msgvault source accounts;
  use `msgvault-accounts` to list them, then pass `--account-email` once per
  selected account (or let the onboarding `step` loop do this with repeated
  `--gmail-account`).
- OpenAI domain parse is not needed; the local heuristic derives
  `example.com -> Example` only for the legacy one-person seed.
- msgvault import itself does not call EnrichLayer/RapidAPI/Parallel/Harmonic.
- Optional LinkedIn resolution can use `resolve_linkedin_queue.py` in harness or
  Parallel mode with approval, then shared `enrich_people.py` handles
  RapidAPI LinkedIn profile hydration for resolved rows.
- Future Gmail sync should remain msgvault-backed unless explicitly redesigned.
