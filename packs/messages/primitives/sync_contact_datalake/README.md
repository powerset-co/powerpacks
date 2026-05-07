# sync_contact_datalake

Build and sync reviewed messages research rows to the server-side contact
datalake endpoint (`POST /v2/contact-datalake/import`).

This is separate from `upload_research_review`: artifact upload stores the
review ZIP; contact datalake sync writes usable contact/linkedin/profile rows
for downstream ingestion.

```bash
python packs/messages/primitives/sync_contact_datalake/sync_contact_datalake.py build \
  --csv .powerpacks/messages/research_review.csv \
  --research-dir .powerpacks/messages/research \
  --output .powerpacks/messages/contact_datalake.payload.json

python packs/messages/primitives/sync_contact_datalake/sync_contact_datalake.py sync \
  --csv .powerpacks/messages/research_review.csv \
  --research-dir .powerpacks/messages/research \
  --confirm-sync
```

The payload includes phone/name/message metadata from the review CSV and the
joined `01_research_parallel.json` profile, including LinkedIn URL when found.
