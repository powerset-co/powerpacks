# Server-side ask: operator-id endpoint (`GET /v2/me`) 🪪

Created: 2026-08-11
Changelog:
- 2026-08-11: initial ask, written alongside the client-side KEY_SOURCES
  mapping and the Modal driver shared-namespace guard.

## Problem

The Modal indexing volume (`powerset-indexing-v2`) is workspace-shared and
namespaced per operator: `operators/<POWERPACKS_OPERATOR_ID>/{input,runs}`.
Nothing provisions `POWERPACKS_OPERATOR_ID` today, so a fresh install falls
back to the all-zeros namespace `operators/00000000-0000-0000-0000-000000000000/`
and collides with every other unprovisioned install (this happened: a fresh
clone re-ran setup into the zeros bucket and overwrote its `input/people.csv`).
The correct operator id is the authenticated user's Powerset `public.users.id`
UUID. No API endpoint currently returns it — probed 2026-08: `/v2/me`,
`/v2/user`, `/v2/users/me`, `/v2/whoami` all 404; `/v2/account` 405.

## Proposal

```
GET /v2/me
Authorization: Bearer <Auth0 access token>   (same auth as /v2/integrations/*)

200 -> {"operator_id": "11111111-1111-4111-8111-111111111111"}
```

- `operator_id` is the caller's `public.users.id` UUID, verbatim.
- Read-only; never mints or provisions anything (matching the contract of the
  existing `/v2/integrations/*` key endpoints).
- 404/403 keeps the existing meaning: "not provisioned for this user".
- Room to grow: the response object can later carry email/workspace fields;
  the client only reads `operator_id`.

## Client already wired

`packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py` — the
`KEY_SOURCES` map already carries
`"POWERPACKS_OPERATOR_ID": ("/v2/me", "operator_id")` (line ~55). The mapping
self-activates: as soon as the endpoint returns 200, `$powerset env pull`
writes `POWERPACKS_OPERATOR_ID` into `.env` and the Modal driver's
shared-namespace guard stops firing. One follow-up on the client after the
endpoint ships: remove `POWERPACKS_OPERATOR_ID` from `PENDING_KEYS` in the
same file so `check`/doctor start *requiring* it instead of reporting it as
pending.

## Interim mitigation (already shipped client-side)

`packs/indexing/modal/linkedin_modal_pipeline.py` refuses volume-mutating
commands when `POWERPACKS_OPERATOR_ID` is unset and the volume contains other
operator prefixes; `--allow-default-operator` is the explicit solo/dev escape
hatch. The refusal message tells the user to set the id — this endpoint is
what makes that automatic.
