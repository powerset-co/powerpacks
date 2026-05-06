# sync_messages_research_cache

Download the operator-scoped messages deep-research cache from the processing
GCS bucket into local `.powerpacks` artifacts before spending new Parallel.ai
credits.

This mirrors `../network-search-api/scripts/sync_messages_research_cache.sh`,
but writes directly to the Powerpacks layout used by
`deep_research_contacts`:

```text
.powerpacks/messages/research/<handle>/01_research_parallel.json
.powerpacks/messages/research_cache/output/<operator_id>/phone_contacts_to_enrich.csv
```

Remote layout:

```text
gs://powerset-search-processing-artifacts/data/messages_research_profiles/<operator_id>/
gs://powerset-search-processing-artifacts/pipeline_output/messages_research/<operator_id>/
```

## Usage

```bash
# Resolve current operator via Powerset auth and show server/local counts.
python packs/messages/primitives/sync_messages_research_cache/sync_messages_research_cache.py status

# Preview GCS commands.
python packs/messages/primitives/sync_messages_research_cache/sync_messages_research_cache.py download --dry-run

# Download cache into .powerpacks/messages/research.
python packs/messages/primitives/sync_messages_research_cache/sync_messages_research_cache.py download
```

The primitive uses cached `$powerset login` credentials to resolve the current
operator via `/v2/messages-research/skip-status`, then shells out to
`gcloud storage rsync --recursive` by default. Use `--operator-id` for an admin
operator override, `--bucket` for a non-default processing bucket, or
`--profiles-dir` if you want a different local `deep_research_contacts
--output-dir`.

## Why this exists

`deep_research_contacts estimate|submit|run` skips rows where
`<output-dir>/<handle>/01_research_parallel.json` already exists. Syncing the
server cache first avoids re-running paid Parallel research for handles already
researched under the same operator.
