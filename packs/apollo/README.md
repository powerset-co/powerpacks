# Apollo pack

The Apollo pack turns reviewed Sales Navigator leads into Apollo outbound
artifacts. It is intentionally conservative: preview and resolution are local,
build creates an inactive Apollo campaign, and activation requires a separate
exact campaign-id confirmation.

## Surfaces

- `skills/build-outbound/SKILL.md` is the agent-facing workflow.
- `primitives/build_outbound/build_outbound.py` resolves Sales Nav runs,
  previews sequence copy, builds inactive campaigns, enrolls contacts, and
  activates only after explicit confirmation.
- `primitives/apollo_mcp/apollo_mcp.py` installs or checks the Apollo MCP server
  for Codex or Claude Code and provides a legacy lead-prep helper.

## Normal flow

1. Resolve a Sales Nav run from `.powerpacks/sales-nav/runs/` by query hint,
   manifest, state path, or run directory.
2. Generate or review sequence copy and write local preview artifacts under
   `.powerpacks/apollo/build-outbound/<run>/`.
3. After user confirmation, enrich the Sales Nav LinkedIn URLs through Apollo
   people enrichment.
4. Convert enriched people into Apollo Contacts:
   - search Contacts by enriched email;
   - reuse only an exact email match;
   - create a Contact when no exact email match exists.
5. Add the resulting unique Contact IDs to a newly created inactive Apollo
   campaign.
6. Activate only through the `activate` command with the exact campaign id.

## Apollo person vs contact model

Apollo people enrichment returns enriched person records. The `matches[].id`
value in that response is a person/prospect id, not a sequence-enrollable
Contact id. Apollo sequences require Contact IDs, so the build step must find or
create Contacts before calling `add_contact_ids`.

The build primitive preserves dedupe while avoiding broad search mistakes by
requiring exact email equality on Contact search results. If Apollo search
returns an unrelated Contact, the primitive ignores it and creates a new Contact
from the enriched payload.

## Safety rules

- Do not print `APOLLO_API_KEY`, raw enrichment responses, or unmasked email
  addresses in chat.
- `resolve-sales-nav` and `preview` are local/read-only.
- `build --dry-run` does not call mutation endpoints and skips enrichment unless
  `--allow-enrichment-in-dry-run` is set.
- Non-dry-run `build` mutates Apollo: it creates an inactive campaign, creates
  or reuses Contacts, and enrolls them. It never activates the campaign.
- `activate` is the only command that can activate a campaign, and it requires
  `--confirm-activation <campaign_id>`.

## Useful commands

```bash
uv run --project . python packs/apollo/primitives/apollo_mcp/apollo_mcp.py status --host codex
uv run --project . python packs/apollo/primitives/apollo_mcp/apollo_mcp.py install --host codex

uv run --project . python packs/apollo/primitives/build_outbound/build_outbound.py resolve-sales-nav \
  --query-hint "product managers at spot2.mx"

uv run --project . python packs/apollo/primitives/build_outbound/build_outbound.py preview \
  --instructions "<reviewed instructions>" \
  --state .powerpacks/sales-nav/runs/<run>/state.json

uv run --env-file .env --project . python packs/apollo/primitives/build_outbound/build_outbound.py build \
  --instructions "<reviewed instructions>" \
  --state .powerpacks/sales-nav/runs/<run>/state.json \
  --sequence-json .powerpacks/apollo/build-outbound/<preview-run>/sequence_input.json
```

Run focused tests with:

```bash
uv run --project . python -m unittest tests.test_apollo_mcp tests.test_build_outbound
```
