---
name: import-whatsapp
description: Import local WhatsApp relationship signals via a user-controlled WAHA Docker container. No contact-exporter dependency.
---

# Import WhatsApp

Use this skill to extract WhatsApp contact metadata locally through a WAHA
container the user runs on their own machine.

The pack is privacy-first:

- never request or store message content
- only collect: phone, name, source, group flags/names, message counts, and
  the most recent message timestamp
- every Docker / WAHA / extraction action is gated on explicit user approval
- the WAHA container is yours to start and stop; nothing runs on a remote service

When invoked by the top-level `import-contacts` workflow, the user's initial
workflow consent covers starting/reusing WAHA and extracting WhatsApp contact
metadata. Still stop for Docker installation, starting a stopped Docker daemon,
and the QR scan.

## Prereqs

- Python 3.9+ (stdlib only)
- Docker Engine reachable as the current user
  - macOS GUI: `brew install --cask docker && open -a Docker`
  - macOS lightweight: `brew install colima docker && colima start --memory 2 --vm-type vz --vz-rosetta`
  - Linux: `curl -fsSL https://get.docker.com | sh && sudo systemctl start docker`
- WhatsApp on the user's phone (for the QR scan in step 3)

The `waha_runtime check` primitive surfaces these alternatives in its JSON
manifest if Docker is missing. Always show that manifest to the user and
**ask explicit permission** before running any `install_cmd`. Do not
auto-install.

## Architecture

The skill is composed of three small primitives:

1. `waha_runtime` — checks Docker, pulls/runs/stops the WAHA NOWEB container
2. `waha_session` — creates the WAHA session, fetches the QR code, polls for auth
3. `extract_whatsapp_contacts` — reads contacts from the authenticated session
   into the canonical CSV/JSONL shape

`normalize_message_contacts` then folds the result into the unified
`message-contact.schema.json` format shared with iMessage.

## Workflow

### 0. Inspect the harness contract

Read `packs/messages/docs/harness.md` and surface the privacy contract above
to the user before doing anything.

### 1. Check Docker availability

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/waha_runtime/waha_runtime.py check
```

Inspect the JSON manifest:

- `docker.installed == true` and `docker.daemon_ok == true`: continue.
- `docker.installed == false`: stop. Show the user the
  `docker.alternatives` array (Docker Desktop, Colima, Linux Docker Engine)
  and **ask explicit permission** before running any of the suggested
  `install_cmd` values. Do not auto-install.
- `docker.installed == true` but `docker.daemon_ok == false`: ask the user
  whether they want to start Docker Desktop / Colima now (the corresponding
  `start_cmd` is in the manifest).

### 2. Start the WAHA container

After explicit user approval, or when `import-contacts` has already collected
workflow consent:

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/waha_runtime/waha_runtime.py up
```

Reuse-on-rerun is the default. To force a clean container, pass `--recreate`.
The first run pulls `devlikeapro/waha:noweb-2026.3.4` (a few hundred MB), which
the user should be told about up front.

### 3. Authenticate the session via QR code

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/waha_session/waha_session.py start --open --wait
```

This:

- creates (or reuses) the `default` WAHA session with NOWEB store enabled
- writes `qr.png` and `qr.txt` under `.powerpacks/messages/whatsapp/`
- opens the PNG in the system image viewer (`--open`)
- polls until status reaches `WORKING` or times out (`--wait`)

Tell the user to open WhatsApp > Settings > Linked Devices > Link a Device,
then scan the displayed QR. If the previous session is still good (credentials
persist under `~/.powerpacks/waha-sessions/`), no scan is needed and the
command returns immediately with `state.status == "WORKING"`.

If `start --wait` times out, run `wait` again to keep polling without
recreating the session:

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/waha_session/waha_session.py wait
```

### 4. Extract contacts

After explicit user approval, or when `import-contacts` has already collected
workflow consent:

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/extract_whatsapp_contacts/extract_whatsapp_contacts.py extract \
  --output-csv .powerpacks/messages/whatsapp.contacts.csv \
  --output-jsonl .powerpacks/messages/whatsapp.contacts.jsonl
```

The primitive writes a manifest with diagnostics next to the CSV. Per-chat
message-count pagination is on by default; pass `--skip-message-counts` for a
fast, less complete run.

### 5. Normalize into the canonical schema

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/normalize_message_contacts/normalize_message_contacts.py normalize \
  --input .powerpacks/messages/whatsapp.contacts.csv \
  --out-jsonl .powerpacks/messages/whatsapp.contacts.normalized.jsonl
```

### 5b. Merge with iMessage (if iMessage was already imported)

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/merge_message_contacts/merge_message_contacts.py merge \
  --input .powerpacks/messages/imessage.contacts.csv \
  --input .powerpacks/messages/whatsapp.contacts.csv \
  --output .powerpacks/messages/contacts.csv
```

This dedupes by phone, unions sources/groups, and keeps the higher
`message_count` / more recent `last_message` per contact. The unified
`contacts.csv` is what `import-contacts-review` reads as input.

### 6. Tear down (optional)

```bash
uv run --project powerpacks python powerpacks/packs/messages/primitives/waha_runtime/waha_runtime.py down
```

Add `--purge-session` if the user wants to wipe the persisted WhatsApp
credentials so the next `up` requires a fresh QR scan.

## What this skill does NOT do (yet)

- LLM review, candidate matching, and upload to Powerset are out of scope here.
  Those are intentionally separate steps that require explicit user approval
  and currently still live in `powerset_contacts_harness` for non-WhatsApp
  channels. Native primitives for review/match/upload can be added the same
  way the extraction primitives were split.
