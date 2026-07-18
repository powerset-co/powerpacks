---
name: import-messages
description: Add iMessage/WhatsApp contacts to your local network. Use for $import-messages. Sets up access (Full Disk Access + WhatsApp QR), syncs message contacts down, matches them against already-imported LinkedIn/Gmail people (free), and imports matched people plus a research-candidates pool. No LLM calls, no paid research, no index build — identity resolution and indexing happen later in $deep-context. Never uploads to a Powerset set.
---

<!--
Created: 2026-06-20
Changelog:
- 2026-07-17: Added smart-default incremental WhatsApp sync. First import full-
  backfills; later runs auto-detect the populated wacli store and pull only the
  delta. New verbs: `$import-messages sync` (explicit incremental) and
  `$import-messages full` (force a full re-backfill), forwarded as
  `--wacli-sync-mode` to discovery. iMessage is unchanged (local chat.db read).
- 2026-07-14: Refocused on contact sync only. Dropped the in-skill LLM triage,
  Parallel deep-research, LLM-scored review UI, and the Modal index/validate
  steps — identity research + index building move to the centralized $deep-context
  processing layer. Import is now contacts-direct: matched contacts land in
  people.csv, floor-passing unmatched contacts land in candidates.csv. Ends by
  suggesting missing sources and offering to process contacts.
- 2026-07-13: Added the product architecture guide; documented both OpenRouter
  calls, Parallel and Modal payloads, Contacts.app inclusion, provider gates, and
  the current explicit-exclusion review semantics.
- 2026-06-20: New skill, split out of $setup (replaces $import-contacts for the
  local-index use case).
-->

# import-messages

`$import-messages` adds **iMessage / WhatsApp** contacts to your local network:
set up access, sync message contacts down, match them against your
already-imported LinkedIn/Gmail people (free), then import — matched contacts
attach their message activity to people you already have; unmatched contacts
worth researching go to a **candidates pool** for the `$deep-context` processing
layer, which builds cross-channel context and resolves identities once. This
skill itself makes **no LLM calls, no paid research, and no index build**.

Run `$setup` (LinkedIn) and `$import-gmail` first for the best matching —
Messages merges on top of whatever is already imported.

For the pipeline walkthrough and privacy map, see
[`message-import-pipeline.md`](../../docs/message-import-pipeline.md).

It runs a **fixed checklist and always reruns it end to end**, idempotent against
fixed paths.

## How to run this skill

**FIRST, create a literal, visible checklist with all seven steps below and step
through it, marking each complete as you go.** Mandatory (TaskCreate / update_plan
/ your harness's todo tool). Seed it with these exact titles:

```
0. Check Powerset runtime credentials
1. Choose Messages sources (iMessage / WhatsApp)
2. Link & discover message contacts (Full Disk Access + WhatsApp QR)
3. Match contacts against LinkedIn & Gmail
4. Import matched people + research candidates
5. Merge all sources
6. Suggest next sources & processing
```

Then: **work the checklist 0 → 6, one item `in_progress` at a time**; run from the
canonical repo root (resolve once, see *Repo root*); overwrite fixed derived paths
and rely on the primitives.

### Guardrails (hard rules)

- **No context pass. Do not go exploring.** Self-contained and authoritative.
  Build the checklist and execute it directly.
- **Do not edit code.** Only invoke the primitives below. Plain shell for
  `cp`/`test`/`wc`/`cat` is fine; no glue scripts.
- **No LLM, no paid providers, no index.** This skill never calls OpenRouter,
  OpenAI, Parallel.ai, RapidAPI, or Modal. Identity research for unresolved
  contacts and the index rebuild belong to `$deep-context`. Never call
  `sync_powerset_candidates`; nothing here uploads to a Powerset set.
- **Metadata only.** Powerpacks never selects or sends message bodies; wacli
  owns its local provider store. Only contact metadata (phone, name, channels,
  message counts, last-message timestamps) is read.
- **Consent gates (pause for the user):** macOS Full Disk Access (Step 2);
  Homebrew installs requested by the WhatsApp child and the WhatsApp QR scan
  (Step 2); and the import confirmation when Step 4 would add new rows.

### Repo root

```bash
resolve_powerpacks_root() {
  for candidate in "${POWERPACKS_REPO_ROOT:-}" "$PWD" "$HOME/powerpacks" "$HOME/workspace/powerpacks"; do
    [[ -n "$candidate" ]] || continue
    [[ "$candidate" != *"/.codex/"* ]] || continue
    if [[ -x "$candidate/bin/update-codex" && -d "$candidate/packs" ]]; then
      printf '%s\n' "$candidate"; return 0
    fi
  done
  return 1
}
REPO="$(resolve_powerpacks_root)" || { echo "Install Powerpacks to ~/powerpacks first." >&2; exit 1; }
cd "$REPO"
```

---

## The checklist

### Step 0 — Check Powerset runtime credentials

```bash
cd "$REPO" && uv run --project . python packs/powerset/primitives/auth/auth.py whoami
cd "$REPO" && uv run --project . python packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py check --env-file .env
```

If `whoami` fails or keys are missing, tell the user to run **`$powerset setup`**
and stop here. That command only establishes login, runtime keys, and MCP access;
it does not import a data source. LinkedIn (`$setup`) and Gmail (`$import-gmail`)
remain optional, separate source workflows that improve Step 3 matching when
they have already been run.

### Step 1 — Choose Messages sources (iMessage / WhatsApp)

Ask the user which channels to add: **iMessage**, **WhatsApp**, **both**, or
**skip**. Record the choice. If the user skips, stop. Source extraction is local
and the flow never uploads to a Powerset set.

There is *no* sync-window question (unlike Gmail). WhatsApp scope follows the
**sync mode**, which you pick from how the user invoked the skill:

- **first import / plain `$import-messages`** → `auto`. The **first** run
  full-backfills all WhatsApp history (~15–20 min) to build the local archive;
  **every later run auto-detects the populated store and pulls only the delta**
  (fast). You do not need to ask.
- **`$import-messages sync`** (or "sync/update/refresh my messages") → the
  explicit fast incremental path.
- **`$import-messages full`** (or "re-import everything / full resync") → forces
  a full re-backfill.

iMessage always does a cheap local `chat.db` read regardless of mode.

### Step 2 — Link & discover message contacts

Discover message contacts with the split Messages discovery handler. It extracts,
normalizes, and merges the selected sources, then stops without materializing
people or touching Powerset. The current iMessage default also includes every
Contacts.app row with a phone number, even when no message exists. Pass only the
channels chosen in Step 1:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/messages.py discover \
  --include-imessage --include-whatsapp
```

Default is smart `auto`. For an explicit verb from Step 1, add the mode flag:
`--wacli-sync-mode incremental` for **sync**, or `--wacli-sync-mode full` for
**full**. (Drop `--include-imessage` or `--include-whatsapp` if that channel
wasn't picked.)
It writes `.powerpacks/messages/contacts.csv` and stages the discovery artifact
at `.powerpacks/network-import/discover/messages/contacts.csv`, with status and
counts in that fixed stage directory's `manifest.json`. It does not create a run
directory or a step ledger. It pauses at consent gates; resolve each, then re-run
the same `discover` command to advance:

- **iMessage Full Disk Access** (`status: blocked_user_action`, step `check_imessage`):
  open the macOS pane, ask the user to enable Full Disk Access for this terminal,
  wait for confirmation, then re-run discovery:

  ```bash
  cd "$REPO" && uv run --project . python packs/ingestion/primitives/extract_imessage_contacts/extract_imessage_contacts.py open-privacy-settings
  ```

- **Missing WhatsApp helper** (`status: blocked_user_action` with an
  `install_command`): show the exact command and ask before running it. After the
  user approves and the install completes, re-run discovery. The child uses
  `--no-install`, so the canonical flow never starts a Homebrew install silently.
- **WhatsApp QR / expired session** (`status: blocked_user_action`, step
  `authenticate_whatsapp`): surface the QR page, have the user scan it in
  WhatsApp, then re-run discovery. (Default provider is wacli.)

**Consent gates: Full Disk Access, WhatsApp helper install, WhatsApp QR.** The
run completes with `selected_steps_completed` once contacts are merged.

### Step 3 — Match contacts against LinkedIn & Gmail

Resolve contacts you already have **for free** by matching against the enriched
gmail + linkedin people from `$setup`/`$import-gmail`. Combine the per-source
people.csv (same schema, one header) and match. The optional `--candidates`
catalog is deliberately omitted, so only those local Gmail/LinkedIn rows
participate:

```bash
cd "$REPO"
GM=".powerpacks/network-import/import/gmail/people.csv"
LI=".powerpacks/network-import/import/linkedin/people.csv"
LOCAL=".powerpacks/messages/_local_people.csv"
{ [ -f "$GM" ] && cat "$GM" || cat "$LI"; } > "$LOCAL" 2>/dev/null
[ -f "$GM" ] && [ -f "$LI" ] && tail -n +2 "$LI" >> "$LOCAL"
uv run --project . python packs/ingestion/primitives/match_local_candidates/match_local_candidates.py match \
  --contacts .powerpacks/messages/contacts.csv --local-people "$LOCAL"
```

(If neither gmail nor linkedin has been imported yet, there's nothing to match
against — pass `--allow-unmatched` in Step 4 and every eligible contact becomes
a research candidate.)

### Step 4 — Import matched people + research candidates

Materialize the matched contacts into this source's canonical
`.powerpacks/network-import/import/messages/people.csv` (attaching message
`interaction_counts` to people you already have) and the unmatched contacts that
pass the deterministic "worth researching" floor into
`.powerpacks/network-import/import/messages/candidates.csv` for `$deep-context`.
The floor is pre-LLM and free: a plausibly-real saved contact name, a real
10–15 digit phone, and at least one DM message; group-only low-signal contacts
are excluded by default. `suggested` matches are never auto-attached — they go
to candidates with the suggestion recorded.

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/messages.py run
```

If it blocks with an import-confirmation (exit 20), show the user the counts
from the diff (matched people + new candidates), get their OK, then re-run with
`--confirm-import`. Useful flags: `--min-message-count N` (raise the DM floor),
`--include-group-only` (keep group-only contacts), `--allow-unmatched` (no match
manifest — first run with no other sources).

No review stop happens here: candidates are a research pool, not searchable
people. Spam screening and identity decisions happen in `$deep-context`'s judged,
user-reviewable flow before anything becomes searchable.

### Step 5 — Merge all sources

Fan-in merges the per-source `import/<source>/people.csv` files into one network
(Messages here, plus LinkedIn/Gmail if already imported):

```bash
cd "$REPO" && uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py fan-in \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Writes `.powerpacks/network-import/merged/people.csv` (default
`--include-existing-artifacts` picks up every imported source).

### Step 6 — Suggest next sources & processing

Check which sources are imported and suggest the missing ones (skip the ones
already present):

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/status.py status
```

- `gmail.import.imported: false` → suggest **`$import-gmail`** (email contacts
  sharpen matching and give `$deep-context` cross-channel context).
- `linkedin.import.imported: false` → suggest **`$setup`** (LinkedIn is the
  identity backbone).
- Report candidate counts (`import.candidates` per source) so the user knows how
  many contacts are waiting for research.

Then ask, **in plain product words grounded in what the status check just
found — name the imported sources, never the skill**. Pattern:

> "I see Gmail and LinkedIn are imported alongside iMessage/WhatsApp — do you
> want to enrich your contacts?"

(Adapt the source list to what's actually imported; `$deep-context` is the
internal route — do not say its name or describe its machinery in the ask.)
If yes → run the `$deep-context` flow. If no → say their new contacts become
searchable after the next enrichment run; nothing is lost, the candidates
stay staged.

---

## Done

Report a terse summary: channels linked, N contacts discovered, K matched people
attached, C research candidates staged, merged network of M people, and whether
the user chose to process now. Remind the user that rerunning `$import-messages`
reruns the whole checklist, and that LinkedIn (`$setup`) and Gmail
(`$import-gmail`) are separate skills.
