---
name: import-messages
description: Import iMessage/WhatsApp contact metadata locally. Sets up source access and sync, then writes candidate people. Deep Context owns matching, worth review, person merging, enrichment, and indexing. No LLM, paid research, or upload in this skill.
---

# import-messages

`$import-messages` imports iMessage and WhatsApp contact metadata as candidate
people. Every contact with a usable phone or email is retained, including unnamed
contacts and contacts without messages. Deep Context makes identity and worth
decisions and combines people across sources.

For the pipeline walkthrough and privacy map, see
[`message-import-pipeline.md`](../../docs/message-import-pipeline.md).

It runs a **fixed checklist and always reruns it end to end**, idempotent against
fixed paths.

## How to run this skill

**FIRST, create a literal, visible checklist with all four steps below and step
through it, marking each complete as you go.** Mandatory (TaskCreate / update_plan
/ your harness's todo tool). Seed it with these exact titles:

```
1. Choose Messages sources (iMessage / WhatsApp)
2. Link & discover message contacts (Full Disk Access + WhatsApp QR)
3. Import message contacts
4. Suggest next sources & processing
```

Then: **work the checklist 1 → 4, one item `in_progress` at a time**; run from the
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
- **Consent gates (pause for the user):** macOS Full Disk Access (Step 2); the
  WhatsApp QR scan (Step 2). The pinned wacli fork auto-downloads a prebuilt binary without a prompt
  (blocks only on an unsupported platform); no Go toolchain is needed.

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

### Step 1 — Choose Messages sources (iMessage / WhatsApp)

Ask the user which channels to add: **iMessage**, **WhatsApp**, **both**, or
**skip**. Record the choice. If the user skips, stop. Source extraction is local
and the flow never uploads to a Powerset set.

There is *no* sync-window or sync-mode question. `$import-messages` owns one
automatic strategy:

- **Empty WhatsApp store:** run an unbounded account sync, then target every DM
  with at most 20 stored rows whose actual latest message is within the last
  three years.
- **Populated WhatsApp store:** run an incremental account sync, compare each
  DM's message count and latest timestamp immediately before and after it, then
  target only recent shallow chats that changed plus unfinished targets from
  the previous run.

The immediate SQLite comparison is more reliable than a saved wall-clock
timestamp because WhatsApp can return delayed messages carrying older
timestamps. One native wacli command keeps a single connection open, sends at
most ten conversation requests at once, waits ten seconds for each response
wave, and pauses ten seconds between batches of ten. If an entire batch remains
silent after identity fallback, it cools down for one minute before continuing.
Each conversation can request up to ten 500-row chunks, but continues only while
rows grow and the phone reports that more remain. A DM first uses its last
successful private history identity (`pn` or `lid`); an unknown chat tries the
phone-number JID first, then the mapped LID after a timeout or empty/no-growth
response. Successful identities are cached in the private wacli database for
later incremental runs. A real response with zero older rows completes that
chat unless the protocol explicitly reports that more history remains. Timeouts
and chats that grow but remain shallow stay resumable in
`.powerpacks/messages/history-depth/` for the next `$import-messages` run. The
account owner's self-chat is excluded.

The first account sync takes 30 minutes up to a few hours depending on history
size (hard cap 3 h), and targeted depth can add up to two hours. Heartbeats every
~2 minutes are normal; keep waiting on the same process. Rerunning the same
`$import-messages` command resumes unfinished targeted chats.

iMessage always does a cheap local `chat.db` read.

### Step 2 — Link & discover message contacts

Discover message contacts with the split Messages discovery handler. It extracts,
normalizes, and merges the selected sources, then stops without materializing
people or touching Powerset. The current iMessage default also includes every
Contacts.app row with a phone number, even when no message exists. Pass only the
channels chosen in Step 1:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/discover/messages/discover.py discover \
  --include-imessage --include-whatsapp
```

(Drop `--include-imessage` or `--include-whatsapp` if that channel wasn't
picked. WhatsApp account sync and per-chat depth are selected automatically.)
It writes `.powerpacks/messages/contacts.csv`, with status and counts in the fixed
stage directory `.powerpacks/network-import/discover/messages/manifest.json`. It
does not create a run directory or a step ledger. WhatsApp runs also overwrite
`.powerpacks/messages/history-depth/results.csv`, `progress.jsonl`, and
`manifest.json`; those artifacts store hashed chat references and aggregate
counters only. The manifest also stores one privacy-safe digest of direct-chat
counts and latest timestamps. That digest makes an interrupted sync—or
unrelated rows returned during a targeted request—trigger one catch-up scan on
the next `$import-messages` run without a ledger or raw identifier snapshot. It
pauses at consent gates; resolve each, then re-run the same `discover` command
to advance:

- **iMessage Full Disk Access** (`status: blocked_user_action`, step `check_imessage`):
  open the macOS pane, ask the user to enable Full Disk Access for this terminal,
  wait for confirmation, then re-run discovery:

  ```bash
  cd "$REPO" && uv run --project . python packs/ingestion/primitives/discover/messages/extract_imessage.py open-privacy-settings
  ```

- **WhatsApp helper (pinned wacli fork):** downloads automatically when missing
  or stale — the flow fetches the prebuilt binary for this platform from the
  fork's release without prompting (no toolchain needed). The only block here is
  `status: blocked_user_action` on an **unsupported platform** (no prebuilt
  binary): surface the message and stop.
- **WhatsApp QR / expired session** (`status: blocked_user_action`, step
  `authenticate_whatsapp`): surface the QR page, have the user scan it in
  WhatsApp, then re-run discovery. (Default provider is wacli.)
**After a completed discovery — deeper-history re-link prompt.** When the
discovery result reports `whatsapp_pairing_state == "pre_full_sync"` at the top
level of `.powerpacks/network-import/discover/messages/manifest.json` (a WhatsApp
link set up before full history sync — upstream wacli or an old build, so only
recent history was pulled), **stop and ask the user before continuing to Step
3** — do not log out or re-pair without an explicit yes:

> Your WhatsApp link predates full history sync, so only recent history was
> pulled. Re-link now to fetch years more? This logs you out and shows a fresh
> QR to scan. Your already-imported contacts stay either way. (yes / no)

- **User says yes** → log out, then re-run the Step 2 `discover` command to issue
  a fresh QR (the re-paired session syncs full history):

  ```bash
  cd "$REPO" && uv run --project . python packs/ingestion/primitives/discover/messages/whatsapp_wacli.py logout
  ```

- **User says no** → proceed to Step 3 with the contacts already imported; the
  deepen is optional.

A missing `whatsapp_pairing_state` (or `pairing.state == "full_sync"`) means
nothing to do — continue to Step 3.

**Consent gates: Full Disk Access, WhatsApp QR.** The pinned wacli fork itself
auto-downloads (blocks only on an unsupported platform). The deeper-history
re-link is an explicit yes/no prompt (stop and ask), never auto-executed. The run
completes with `selected_steps_completed` once contacts are merged.

### Step 3 — Import message contacts

Write source names, identifiers, channels, message counts, and dates to
`.powerpacks/network-import/import/messages/people.csv`. Group metadata stays
in the source `contacts.csv`. Rows use
canonical `candidate:phone:` or `candidate:email:` IDs. No catalog, review file,
minimum-message floor, or import confirmation is required.

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/imports/messages/importer.py run
```

Report the manifest's `stats.people` and `stats.candidates`; both count the
source candidates. Deep Context combines source files before processing them.

### Step 4 — Suggest next sources & processing

Check which sources are imported and suggest the missing ones (skip the ones
already present):

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/imports/status.py status
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

Report channels linked, contacts discovered/imported, and whether the user chose
to process now. Imports stay staged until Deep Context combines and processes
the sources. Rerunning this skill repeats the checklist using the existing stores.
