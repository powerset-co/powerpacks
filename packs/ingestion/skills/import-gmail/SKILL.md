---
name: import-gmail
description: Add Gmail contacts to your local network. Use for $import-gmail. Sets up msgvault/Gmail (OAuth + authorize), asks which accounts and how many years to sync, syncs mail down, and imports contacts free and locally (shared identity directory only) — unresolved contacts go to a research-candidates pool. No Parallel.ai, no RapidAPI, no index build — identity resolution and indexing happen later in $deep-setup. Always reruns the full checklist; overwrites in place.
---

<!--
Created: 2026-06-20
Changelog:
- 2026-07-14: Refocused on contact sync only. Step 6 import is now free/local
  (directory reuse only): Parallel.ai LinkedIn resolution + RapidAPI hydration
  move to the centralized $deep-setup processing layer, and unresolved contacts
  land in import/gmail/candidates.csv. Dropped the Modal index/validate steps;
  ends by suggesting missing sources and offering to process contacts.
- 2026-07-13: Added the product architecture guide; fixed multi-account discovery
  and authorization instructions; documented the local directory, Parallel,
  RapidAPI, Modal, privacy, and missing identity-review boundaries.
- 2026-06-20: New skill, split out of $setup (replaces the old discovery-only
  $import-email).
-->

# import-gmail

`$import-gmail` adds **Gmail** contacts to your local network: set up msgvault,
sync the chosen accounts (bounded by a years-back window), then import contacts
**free and locally** — people already known to your identity directory attach
immediately; everyone else worth researching goes to a **candidates pool** for
the `$deep-setup` processing layer, which builds cross-channel context and
resolves identities once. This skill itself calls **no paid providers and builds
no index**. Run `$setup` (LinkedIn) first for the best results — Gmail merges on
top of whatever is already imported.

For a product-level walkthrough, lookup stages, provider payloads, approval
boundaries, and architecture diagram, see
[`gmail-import-pipeline.md`](../../docs/gmail-import-pipeline.md).

It runs a **fixed checklist and always reruns it end to end**. Reruns are
idempotent against fixed paths. The one exception is the msgvault store
(`~/.msgvault/msgvault.db`): it is the durable, incrementally-synced archive —
**never delete it**. With the explicit history window below, reruns rescan that
window deterministically and msgvault skips messages already stored. Last-message
resume inference applies only when no explicit window is supplied.

## How to run this skill

**FIRST, create a literal, visible checklist with all nine steps below and step
through it, marking each complete as you go.** Mandatory (TaskCreate / update_plan
/ your harness's todo tool). Seed it with these exact titles:

```
0. Check prereqs (Powerset login + runtime keys)
1. Check msgvault status
2. Ask which Gmail accounts to link + how far back to sync
3. Create msgvault OAuth app (browser, if not configured)
4. Authorize Gmail accounts
5. Sync Gmail archives
6. Import Gmail contacts (free, local)
7. Merge all sources
8. Suggest next sources & processing
```

Step 3 is conditional (no-op if OAuth already configured). Keep it in the
checklist and mark it complete as a no-op when it doesn't apply.

Then: **work the checklist 0 → 8, one item `in_progress` at a time**; run from the
canonical repo root (resolve once, see *Repo root*); overwrite fixed derived
paths and rely on the primitives — don't pre-delete or invent folders.
**Never delete `~/.msgvault/msgvault.db`.**

### Guardrails (hard rules)

- **No context pass. Do not go exploring.** Self-contained and authoritative.
  Build the checklist and execute it directly.
- **Do not edit code.** Only invoke the primitives below (flag an actual blocking
  bug if you hit one). Plain shell for `cp`/`test`/`wc`/`cat` is fine; no glue
  scripts.
- **Never call `msgvault` (or `msgvault sync-full`) directly.** Gmail syncing
  happens *only* through Step 5's `gmail.py discover --sync-after "$SYNC_AFTER"`.
  A bare `msgvault sync-full <email>` has no date bound and pulls the entire
  mailbox, ignoring the chosen window. **Re-authorizing a lapsed token is
  `msgvault_setup.py add-account --force-auth` (OAuth only, no sync) — see Step 4;
  it is never a `sync-full`.** If discover fails, recover by syncing *less* (a
  narrower `--sync-after`), never the full mailbox.
- **No paid providers, no index.** This skill never calls Parallel.ai, RapidAPI,
  OpenAI, or Modal. Identity resolution for unresolved contacts and the index
  rebuild belong to `$deep-setup`. (The import primitive keeps a
  `--resolve-legacy` escape hatch for the old in-import behavior; do not use it
  in this flow.)
- **Consent gates (pause for the user):** msgvault browser/gcloud OAuth-app
  creation and Gmail account authorization (Steps 3-4). Everything after OAuth
  is free and local.

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

### Step 0 — Check prereqs (Powerset login + runtime keys)

```bash
cd "$REPO" && uv run --project . python packs/powerset/primitives/auth/auth.py whoami
cd "$REPO" && uv run --project . python packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py check --env-file .env
```

If `whoami` fails or keys are missing, tell the user to run **`$setup`** first (or
`auth.py login` + `pull_runtime_keys.py pull --env-file .env`) and stop here.

### Step 1 — Check msgvault status

Safe, local. Drives the next two steps:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py status
```

The JSON reports `gcloud`, `config.oauth_configured`, `database.exists`, and
authorized `accounts`. If `oauth_configured` is true and the db exists, Step 3 is
a no-op.

### Step 2 — Ask which Gmail accounts to link (and how far back to sync)

Ask the user for **every** Gmail address they want searchable, in one prompt.
Treat the first as the **primary** (used to create the OAuth app in Step 3); the
rest are additional accounts authorized in Step 4. Compare against the already
`accounts` from Step 1 — only accounts not already authorized need Steps 3–4.
Record the list. Do not guess emails.

**In the same prompt, ask how far back to sync.** How many years of mail should be
archived? **Default is 3 years.** The user may answer **`all`** (full mailbox
history) or any number of years (e.g. **1**, **2**, **5**). Record the answer as
`$SYNC_YEARS` and carry it into Step 5. A wide window — especially `all` — makes
the first sync much longer; if the user asks for more than 3 years, confirm before
running Step 5.

### Step 3 — Create msgvault OAuth app (browser, if not configured)

Only if Step 1 showed OAuth not configured. One-time browser setup for the
**primary** Gmail account from Step 2 — **consent: drives Chrome + gcloud, creates
the Google OAuth Desktop app, inits the db, authorizes the primary account**:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py browser-setup \
  --email <primary-gmail> --add-account --init-db
```

Writes `~/.msgvault/config.toml`, the client secret, and `~/.msgvault/msgvault.db`
(do not delete the db). If already configured, mark this step a no-op.

### Step 4 — Authorize Gmail accounts

Re-run `msgvault_setup.py status` after Step 3, because a fresh
`browser-setup --add-account` already authorized the primary. For **every requested
account still absent from the current `status.accounts`**, run the per-account
browser OAuth grant. This includes the primary only when OAuth configuration
pre-existed and it is still absent:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py add-account --email <email>
```

Re-run `msgvault_setup.py status` and confirm every requested account appears
under `accounts`.

**Re-authorizing a lapsed account.** If an account is *already in the vault* but
its token is **expired, revoked, or missing**, re-authorize the same way with
`--force-auth` (msgvault replaces the stale token; **OAuth only, downloads no
mail**):

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py add-account --email <email> --force-auth
```

The account keeps every previously-synced message; Step 5's bounded `discover`
rescans the chosen window and deduplicates stored messages. **Never** re-authorize with
`msgvault sync-full <email>` — it re-pulls the entire mailbox.

### Step 5 — Sync Gmail archives

Sync all authorized accounts and build the discover artifacts in **one command**,
using the window from Step 2. Passing one account per separate invocation rewrites
the stable discovery manifest and can leave only the last account available to
Step 6. Compute `SYNC_AFTER` from `$SYNC_YEARS` (default `3`; `all` = full history)
and repeat `--account-email` once per selected account:

```bash
cd "$REPO"
# $SYNC_YEARS from Step 2: a number (default 3) or the word "all".
if [ "${SYNC_YEARS:-3}" = "all" ]; then
  SYNC_AFTER="2004-01-01"   # pre-Gmail = the entire mailbox
else
  SYNC_AFTER="$(date -v-${SYNC_YEARS:-3}y +%Y-%m-%d 2>/dev/null || date -d "${SYNC_YEARS:-3} years ago" +%Y-%m-%d)"
fi
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/gmail.py discover \
  --account-email <first-email> \
  --account-email <second-email> \
  --sync-after "$SYNC_AFTER"
```

Omit extra repeated flags when only one account was selected. Writes
`.powerpacks/network-import/discover/gmail/<account>/`. **Only sync through this
`discover` command** — never `msgvault sync-full` (no `--after` bound → entire
mailbox). If `discover` errors or the sync is too slow/large, recover by syncing
**less, never more** (a more recent `--sync-after`). If a narrower window still
fails, surface the error and stop. A large first sync can take a while; msgvault
skips already-downloaded messages on reruns.

Content boundary: the bounded `msgvault sync-full` child downloads messages into
msgvault's local full-message archive. The current command does not request
attachment suppression, so supported msgvault builds may also store attachments.
Powerpacks' subsequent SQLite reader selects contact/interaction metadata only and
does not send bodies, subjects, snippets, MIME, or attachments to identity providers.

### Step 6 — Import Gmail contacts (free, local)

Import applies the **local identity directory** to the discovered Gmail queues
(people already resolved by prior imports attach immediately), writes
`.powerpacks/network-import/import/gmail/people.csv`, and stages every
still-unresolved contact worth researching in
`.powerpacks/network-import/import/gmail/candidates.csv` for `$deep-setup`.
No Parallel.ai, no RapidAPI, no spend prompt:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/gmail.py run
```

Report the manifest's `stats`: people imported and candidates staged. Identity
resolution for the candidates (Parallel.ai with dossier context, judged and
user-reviewable) happens in `$deep-setup`, not here.

### Step 7 — Merge all sources

Fan-in merges the per-source `import/<source>/people.csv` files into one network
(Gmail here, plus LinkedIn/Messages if already imported):

```bash
cd "$REPO" && uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py fan-in \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Writes `.powerpacks/network-import/merged/people.csv` (default
`--include-existing-artifacts` picks up every imported source).

### Step 8 — Suggest next sources & processing

Check which sources are imported and suggest the missing ones (skip the ones
already present):

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/status.py status
```

- `messages.import.imported: false` → suggest **`$import-messages`**
  (iMessage/WhatsApp contacts give `$deep-setup` cross-channel context).
- `linkedin.import.imported: false` → suggest **`$setup`** (LinkedIn is the
  identity backbone).
- Report candidate counts (`import.candidates` per source) so the user knows how
  many contacts are waiting for research.

Then ask, **in plain product words grounded in what the status check just
found — name the imported sources, never the skill**. Pattern:

> "I see iMessage and WhatsApp are imported alongside Gmail — do you want to
> enrich your contacts?"

(Adapt the source list to what's actually imported; `$deep-setup` is the
internal route — do not say its name or describe its machinery in the ask.)
If yes → run the `$deep-setup` flow. If no → say their new contacts become
searchable after the next enrichment run; nothing is lost, the candidates
stay staged.

---

## Done

Report a terse summary: N Gmail accounts synced, K contacts imported (directory
hits), C research candidates staged, merged network of M people, and whether the
user chose to process now. Remind the user that rerunning `$import-gmail` reruns
the whole checklist: an explicit history window is rescanned with `--noresume`,
while msgvault deduplicates stored messages and preserves its db. LinkedIn
(`$setup`) and iMessage/WhatsApp (`$import-messages`) are separate skills.
