# sync_powerset_candidates

Download the operator's contact catalog from Powerset's search-api into a flat
local CSV for the matcher / LLM review primitives. Stdlib-only.

Reads the access token from the credentials file written by
`powerset_auth login` (default `~/.powerpacks/credentials.json`).

## Usage

```bash
# Refresh the catalog (auto-discovers credentials, paginates /v2/contacts).
python packs/messages/primitives/sync_powerset_candidates/sync_powerset_candidates.py sync \
  --output .powerpacks/messages/powerset_contacts.csv

# Admin: sync another operator's catalog (server enforces permissions).
python ... sync_powerset_candidates.py sync --operator-id op_123

# Use the local API endpoint for development.
python ... sync_powerset_candidates.py sync --local

# Don't call the API; just validate the local cache exists.
python ... sync_powerset_candidates.py sync --use-cached
```

## Behavior on failure

- **Auth missing/expired and cache present** — exit 0, status
  `cached_after_auth_error`. The downstream matcher can still run.
- **Network error and cache present** — exit 0, status
  `cached_after_network_error`.
- **Server returned a 5xx / unparseable body** — exit 1 (do not silently
  degrade match quality).
- **No cache and not logged in** — exit 1.

## Output schema

CSV columns:

```
id, name, linkedin_url, phone_number, emails, public_identifier
```

`emails` is `;`-joined. This is exactly the shape `match_local_candidates`
consumes.
