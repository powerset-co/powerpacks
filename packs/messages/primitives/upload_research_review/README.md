# upload_research_review

Uploads a reviewed messages research CSV to Powerset through:

`POST /v2/messages-research/artifacts`

The product-level upload concept is `approved`. The review UI still stores
legacy explicit decisions in `exclude` (`exclude=no` means approved,
`exclude=yes` means unapproved). This primitive prepares an upload CSV containing
only approved contacts. For backend artifact compatibility, approved rows are
sent with `bucket=yes` and an `approved=true` column.

It reuses the cached Powerset login from `~/.powerpacks/credentials.json`.

```bash
python packs/messages/primitives/upload_research_review/upload_research_review.py summarize \
  --csv .powerpacks/messages/research_review.csv

python packs/messages/primitives/upload_research_review/upload_research_review.py upload \
  --csv .powerpacks/messages/research_review.csv \
  --confirm-upload
```

Never run `upload --confirm-upload` until the user explicitly approves uploading
the approved contacts.
