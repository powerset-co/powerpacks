# Apollo build-outbound primitive

Stdlib-only CLI for turning a reviewed Sales Nav run into an inactive Apollo sequence.

Commands:

- `resolve-sales-nav [--query-hint TEXT] [--sales-nav-manifest PATH] [--state PATH] [--run-dir PATH] [--limit N]`
- `preview --instructions TEXT [...] [--sequence-json PATH] [--out-dir PATH]`
- `build --instructions TEXT --sequence-json PATH [...] [--dry-run]`
- `activate --manifest PATH --confirm-activation CAMPAIGN_ID`

Safety notes:

- `build --dry-run` does not call Apollo mutation endpoints; enrichment is skipped unless `--allow-enrichment-in-dry-run` is passed.
- Non-dry-run `build` creates an inactive Apollo campaign and enrolls contacts, but never activates it.
- `activate` requires the exact `campaign_id` from `manifest.json` and writes `activation_status.json`.
- Console JSON masks emails and never prints `APOLLO_API_KEY`; raw Apollo responses are kept locally under `.powerpacks/`.

## Manual live smoke

This primitive has a manual-only live Apollo smoke path for now; do not wire it into CI or a weekly schedule until we add a dedicated cleanup wrapper. Use a one-lead fake Sales Nav run shaped like real exports (`state.json` with `files.final_leads_csv: "exports/leads.csv"`, or `files.leads_jsonl`) and target only a synthetic test contact (`operator@example.com`, LinkedIn `https://www.linkedin.com/in/example-test-contact/`).

Expected live-smoke flow:

1. Run `preview`, then non-dry-run `build` with reviewed one-step smoke copy.
2. Confirm Apollo created the sequence, step/template/touch, and enrolled exactly one contact.
3. If activation behavior must be checked, run `activate` with the exact manifest `campaign_id`; immediately stop/remove the contact and archive the sequence through Apollo.
4. Verify the archived campaign is inactive and no messages are scheduled/sent before reporting success.
