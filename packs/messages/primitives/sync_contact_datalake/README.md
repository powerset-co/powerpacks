# sync_contact_datalake

Build and sync reviewed messages research rows to the server-side contact
datalake endpoint (`POST /v2/contact-datalake/import`).

This is separate from `upload_research_review`: artifact upload stores the
review ZIP; contact datalake sync stages contact/linkedin/profile rows for
future downstream ingestion/materialization.

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

The payload includes:

- operator context from API auth/request body
- phone/name/message metadata from the review CSV
- optional canonical `linkedin_url`
- `public_identifier` using Aleph's synthetic rules (`linkedin slug`, then
  `synth-x-*`, `synth-phone-*`, etc.)
- raw `research_profile` from `01_research_parallel.json`
- draft `synthetic_profile` in Aleph `04_final_profile.json` shape
- `processing_status: staged`

The draft `synthetic_profile` is intentionally not materialized directly. The
server-side processing stage can resolve company URNs, run Harmonic for LinkedIn
URLs, and transform it into the final Harmonic-compatible synthetic shape.
