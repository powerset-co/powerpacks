# sales_nav_artifacts

Local file store for Sales Navigator MCP results.

The Sales Nav MCP returns one page of leads or artifact rows at a time. This
primitive normalizes those page responses into durable local handoff files so
agents can pass file paths between steps instead of re-reading or re-pasting lead
payloads.

Default files under the run directory:

- `leads.jsonl` / `leads.csv` — one row per lead/member
- `mutuals.jsonl` / `mutuals.csv` — lead ↔ mutual edges with operator/source metadata
- `member_urls.json` — member_id → LinkedIn URL resolutions
- `manifest.json` — paths/counts/artifact ids

Typical workflow:

```bash
python packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py init \
  --query "VP engineering at Stripe" --set-id "$SET_ID" --conversation-id "$CONV_ID"

# After an MCP sales_nav_search or get_artifact response has been saved to page.json:
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
