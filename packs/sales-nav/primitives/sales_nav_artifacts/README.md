# sales_nav_artifacts

Local file store for Sales Navigator MCP results.

The Sales Nav MCP returns one page of leads or artifact rows at a time. This
primitive normalizes those page responses into durable local handoff files so
agents can pass file paths between steps instead of re-reading or re-pasting lead
payloads.

Default files under the run directory:

- `leads.jsonl` — internal handoff, one row per lead/member, including enriched profile fields (`summary`, `experiences`, `education`, `enriched`), operator/source metadata, and `total_interactions` when available
- `mutuals.jsonl` — internal handoff, lead ↔ mutual edges with operator/source metadata and `total_interactions` when available
- `member_urls.json` — internal handoff, member_id → LinkedIn URL resolutions
- `manifest.json` — paths/counts/artifact ids
- `exports/leads.csv` and `exports/mutuals.csv` — final user-facing CSVs written only by `export`

Typical workflow:

```bash
python packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py init \
  --query "VP engineering at Stripe" --set-id "$SET_ID" --conversation-id "$CONV_ID"

# Normal fast handoff: after MCP sales_nav_search returns artifact_id, download the persisted artifact directly (uses powerset_auth/Auth0 credentials):
python packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py download-artifact \
  --artifact-id "$ARTIFACT_ID" --out page.json

# After a Sales Nav artifact response has been saved to page.json:
python packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py ingest-page \
  --state .powerpacks/sales-nav/runs/<id>/state.json --response page.json

# After MCP sales_nav_resolve_member_ids response has been saved to urls.json:
python packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py ingest-member-urls \
  --state .powerpacks/sales-nav/runs/<id>/state.json --response urls.json

python packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py export \
  --state .powerpacks/sales-nav/runs/<id>/state.json

python packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py lookup \
  --state .powerpacks/sales-nav/runs/<id>/state.json --query "ada"
```

`lookup` joins the leads and mutuals files and returns compact JSON for answering
follow-up questions without loading the full JSONL files into chat context.
