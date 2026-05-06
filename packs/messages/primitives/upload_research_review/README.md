# upload_research_review

Uploads a reviewed messages research CSV to Powerset through:

`POST /v2/messages-research/artifacts`

The review UI writes yes/no decisions to `exclude`. The backend artifact
endpoint currently splits by `bucket`, so this primitive prepares an upload CSV
where:

- `exclude=yes` becomes `bucket=no`
- `exclude=no` becomes `bucket=yes`
- blank `exclude` keeps the original bucket default

The server artifact stores yes/maybe/no splits. The yes split is the
include/enrich set; maybe/no are preserved as reviewed context.

It reuses the cached Powerset login from `~/.powerpacks/credentials.json`.

```bash
python packs/messages/primitives/upload_research_review/upload_research_review.py summarize \
  --csv .powerpacks/messages/research_review.csv

python packs/messages/primitives/upload_research_review/upload_research_review.py upload \
  --csv .powerpacks/messages/research_review.csv \
  --confirm-upload
```

Never run `upload --confirm-upload` until the user explicitly approves uploading
the reviewed artifact.
