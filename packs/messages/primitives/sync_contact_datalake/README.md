# sync_contact_datalake

Build and sync reviewed messages research rows to the server-side contact
datalake endpoint (`POST /v2/contact-datalake/import`).

This is separate from `upload_research_review`: artifact upload stores the
review ZIP; contact datalake sync stages contact/linkedin/profile rows for the
backend to process later.

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
- phone/name/message metadata from the review CSV (`phone_e164`/`phone` and
  `full_name`/`name` are both sent explicitly; total, iMessage, and WhatsApp
  counts/timestamps are sent separately)
- stable `source_key` derived from the normalized phone number
- optional canonical `linkedin_url`
- `public_identifier` using Aleph's synthetic rules (`linkedin slug`, then
  `synth-x-*`, `synth-phone-*`, etc.)
- raw `research_profile` from `01_research_parallel.json`
- draft `synthetic_profile` in Aleph `04_final_profile.json` shape
- `processing_status: staged`

The draft `synthetic_profile` is intentionally staged as input for backend
processing. The backend can resolve companies, run enrichment for LinkedIn URLs,
and transform it into the final production-ready profile shape.
