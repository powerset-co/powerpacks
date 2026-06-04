---
name: build-outbound
description: Build an outbound handoff from network-search or Sales Nav leads through the Apollo.io MCP. Use for `$build-outbound setup`, `$build-outbound status`, `$build-outbound prepare-leads`, outbound campaign handoff, or adding Powerpacks/Sales Nav/network-search leads to Apollo sequences.
---

# Build Outbound

Use this skill when the user asks to build outbound from leads, configure Apollo.io MCP, prepare leads for Apollo outbound, or move `$search-network` / `$sales-nav-search` results into an Apollo sequence.

Powerpacks uses the public stdio MCP package `apollo-mcp@0.2.0`. There is no
first-party Apollo.io MCP server in this repo. The MCP exposes Apollo API tools;
Powerpacks only installs/configures the server and prepares local lead handoff
files.

## What is possible

Supported flow:

1. Find leads with `$search-network` or `$sales-nav-search` and export CSV/JSON.
2. Prepare Apollo handoff files from the export, especially LinkedIn URLs.
3. Use Apollo MCP read-only tools to inspect sequences and connected email
   accounts.
4. With explicit user confirmation, enrich LinkedIn-only leads, create/update
   Apollo contacts, then enroll selected contact IDs into an existing Apollo
   sequence from a chosen connected email account.

Do **not** promise Apollo sequence/campaign creation through MCP. The selected
MCP package supports finding existing sequences and adding contacts to them; the
operator should create/configure copy, steps, schedules, sending limits, and
mailbox warmup in Apollo first.

## Setup/configuration

Apollo prerequisites:

- Apollo API key: create it in Apollo at **Settings → Integrations → Apollo API**.
- Use a **Master API key** for sequence/campaign, email-account, and admin-style
  tools. A non-master key may work for narrower search/enrichment endpoints but
  often fails for sequences.
- Connected sending mailbox in Apollo: Settings / Mailboxes or Email Accounts.
  The MCP `get_email_accounts` tool lists the usable `send_email_from_email_account_id`.
- Existing Apollo sequence/campaign: create and review copy/steps/schedule in
  Apollo. The MCP `search_sequences` tool returns the `emailer_campaign_id`.

Store the key locally without pasting it into chat:

```bash
# In the Powerpacks repo or installed bundle cwd:
printf '\nAPOLLO_API_KEY=your_apollo_api_key\n' >> .env
```

Then install/register the MCP:

```bash
uv run --project . python packs/apollo/primitives/apollo_mcp/apollo_mcp.py install --host codex
# or add --host claude / --host all for Claude Code or both hosts
```

From an installed skill bundle, replace `packs/...` with `powerpacks/packs/...`.
Restart Codex/Claude Code after install.

## Commands

```bash
uv run --project . python packs/apollo/primitives/apollo_mcp/apollo_mcp.py status --host codex
uv run --project . python packs/apollo/primitives/apollo_mcp/apollo_mcp.py install --host codex
# or add --host claude / --host all for Claude Code or both hosts
uv run --project . python packs/apollo/primitives/apollo_mcp/apollo_mcp.py prepare-leads --input <leads.csv>
```

## Routing

- `$build-outbound`, `$build-outbound help`: summarize setup and safe outbound flow.
- `$build-outbound status`: run `apollo_mcp.py status` (defaults to Codex) unless the
  user asks for Claude or all hosts. Say only whether `APOLLO_API_KEY`, Node/npx,
  and MCP registration are present. Never print the API key.
- `$build-outbound setup` / `install outbound mcp` / `install apollo mcp`: if `APOLLO_API_KEY` is present, run
  `apollo_mcp.py install` for Codex, or add `--host claude` / `--host all` when
  requested. If missing, tell the user to create a Master API key in Apollo and
  add `APOLLO_API_KEY` to `.env` or the shell.
- `prepare these leads for Apollo`: run `prepare-leads --input <export.csv>` and
  report counts plus output paths.
- `add these leads to campaign/sequence`: first prepare leads if needed, then
  use MCP read-only tools to list/resolve the target sequence and email account.
  Ask for explicit confirmation before any Apollo enrichment/contact creation or
  sequence enrollment.

## MCP tools to use

Typical read-only checks after setup:

- `get_email_accounts` — list connected sending accounts; pick one for
  `send_email_from_email_account_id`.
- `search_sequences` — find existing Apollo sequences/campaigns and their
  `emailer_campaign_id`.
- `search_contacts` — dedupe/check already-saved Apollo contacts.
- `get_api_usage` — optional credit/rate-limit visibility.

Spend-bearing or mutating tools; require explicit confirmation:

- `bulk_enrich_people` / `enrich_person` — can consume Apollo enrichment credits
  and reveal email addresses from LinkedIn URLs.
- `bulk_create_contacts`, `create_contact`, `update_contact` — creates/updates
  saved Apollo contacts.
- `add_contacts_to_sequence` — enrolls contacts into an Apollo email sequence;
  this can trigger outbound email according to the sequence settings.
- `update_contact_sequence_status` — changes a contact's sequence membership
  state (`active`, `paused`, `finished`, etc.).
- `activate_sequence` / `deactivate_sequence` — changes sending state; never run
  unless the user explicitly asks.

Treat every Apollo MCP tool that is not explicitly read-only above as requiring
confirmation, including account/deal/task/contact mutations exposed by the MCP.

## Lead handoff playbook

Given a CSV from network search or Sales Nav:

```bash
uv run --project . python packs/apollo/primitives/apollo_mcp/apollo_mcp.py prepare-leads \
  --input .powerpacks/sales-nav/runs/<run>/exports/leads.csv
```

The primitive writes `.powerpacks/apollo/<run>/` with:

- `contacts.json` — normalized Apollo contact payloads with names, titles,
  company, email when present, and LinkedIn URLs.
- `enrich_requests.json` — LinkedIn/name/company payloads for leads missing
  email.
- `create_ready_contacts.json` — contacts with `email`, `first_name`, and
  `last_name`, safe to batch into Apollo create-contact calls.
- `manual_review_contacts.json` — identifiable rows that need Apollo enrichment
  or manual edits before contact creation, including LinkedIn-only leads.
- `contact_batches.json` — create-ready chunks for `bulk_create_contacts`;
  Apollo requires `first_name` and `last_name`, and Powerpacks only includes
  rows that already have email.
- `enrich_batches.json` — chunks of up to 10 for `bulk_enrich_people`.
- `manifest.json` — counts and paths.

Keep these files local. Do not paste full lead lists, emails, or API responses
into chat; summarize counts and selected rows only when needed.

## Confirmation wording

Before enrichment/contact creation/sequence enrollment, ask a concrete approval,
for example:

> I found 42 prepared leads: 30 already have email and 12 need Apollo enrichment.
> Enriching can spend Apollo credits; creating contacts modifies Apollo; enrolling
> contacts may send email through sequence `<name>` from `<email>`. Should I
> enrich the 12 missing emails, create/update contacts, and enroll them?

Proceed only after explicit yes/confirm.
