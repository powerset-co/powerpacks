---
name: import-gmail
description: Add Gmail to your local search index. Use for $import-gmail. Sets up msgvault/Gmail (OAuth + authorize), asks which accounts and how many years to sync, syncs + imports Gmail contacts (resolved to LinkedIn via Parallel.ai), then fan-in merges all imported sources and rebuilds the Modal search index. Always reruns the full checklist; overwrites in place.
---

<!--
Created: 2026-06-20
Changelog:
- 2026-07-13: Added the product architecture guide; fixed multi-account discovery
  and authorization instructions; documented the local directory, Parallel,
  RapidAPI, Modal, privacy, and missing identity-review boundaries.
- 2026-06-20: New skill, split out of $setup (replaces the old discovery-only
  $import-email). Carries the Gmail/msgvault block (status -> ask accounts/years
  -> OAuth app -> authorize -> sync -> import) plus the shared fan-in + Modal
  index + validate so Gmail can be added on its own and merged into the index.
-->

# import-gmail

`$import-gmail` adds **Gmail** to your local search index: set up msgvault, sync
the chosen accounts (bounded by a years-back window), import contacts (resolved
to LinkedIn on Parallel.ai), then **merge all imported sources + rebuild the
index**. Run `$setup` (LinkedIn) first for the best results — Gmail merges on top
of whatever is already imported.

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

**FIRST, create a literal, visible checklist with all ten steps below and step
through it, marking each complete as you go.** Mandatory (TaskCreate / update_plan
/ your harness's todo tool). Seed it with these exact titles:

```
0. Check prereqs (Powerset login + runtime keys)
1. Check msgvault status
2. Ask which Gmail accounts to link + how far back to sync
3. Create msgvault OAuth app (browser, if not configured)
4. Authorize Gmail accounts
5. Sync Gmail archives
6. Import Gmail contacts
7. Merge all sources
8. Index the merged network
9. Validate the search index
```

Step 3 is conditional (no-op if OAuth already configured). Keep it in the
checklist and mark it complete as a no-op when it doesn't apply.

Then: **work the checklist 0 → 9, one item `in_progress` at a time**; run from the
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
- **Consent gates (pause for the user):** msgvault browser/gcloud OAuth-app
  creation and Gmail account authorization (Steps 3-4); the external identity
  resolution/profile hydration in Step 6; and the cloud upload/provider-backed
  Modal indexing in Step 8. The Step 6 primitive auto-runs Parallel below 25
  unresolved contacts and does not internally gate RapidAPI cache misses, so the
  agent must obtain approval before invoking it rather than relying on the child
  gates. `index-people --max-usd 0` is also currently uncapped internal mode.

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

### Step 6 — Import Gmail contacts

Import first reuses the local identity directory, resolves only the remaining
contacts to LinkedIn via **Parallel.ai**, hydrates accepted LinkedIn URLs through
the local profile cache or **RapidAPI**, then writes
`.powerpacks/network-import/import/gmail/people.csv`. Run **without** the spend
flag first — the primitive auto-approves small batches (under its threshold) and
otherwise blocks:

Before running it, explain that Parallel receives name, email, and an
email-domain-derived company, while RapidAPI receives accepted LinkedIn URLs;
then get explicit approval for those provider calls. The current flow has no
human identity-verification gate: a normalized match at confidence 0.75 or above
can proceed directly to hydration, and a found result with missing or zero provider
confidence is currently normalized to 0.90. The separate Gmail verification/review
proposal is not wired into `$import-gmail` yet.

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/gmail.py run
```

- If it completes (small batch auto-approved), proceed.
- **If it blocks reporting pending Parallel contacts** (at/above the threshold),
  tell the user the contact count and ask to approve the spend. On approval:

  ```bash
  cd "$REPO" && uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/gmail.py run --approve-parallel-spend
  ```

### Step 7 — Merge all sources

Fan-in merges the per-source `import/<source>/people.csv` files into one network
(Gmail here, plus LinkedIn/Messages if already imported):

```bash
cd "$REPO" && uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py fan-in \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Writes `.powerpacks/network-import/merged/people.csv` (default
`--include-existing-artifacts` picks up every imported source).

### Step 8 — Index the merged network

Index the merged people.csv on Modal (generic indexer, no import stage) and
download the duckdb:

This uploads the complete merged CSV, including email and interaction metadata,
to a workspace-shared Modal volume. Input and run paths are prefixed by
`POWERPACKS_OPERATOR_ID`, caches are shared, and a missing operator ID falls back
to the all-zero path. Modal may use provider-backed classification and embeddings,
and the current default `--max-usd 0` is uncapped internal mode. Show that boundary
and obtain explicit approval before invoking the command.

```bash
cd "$REPO" && uv run --project . python packs/indexing/modal/linkedin_modal_pipeline.py index-people \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Run it in the background and keep Step 8 `in_progress` until the command
**exits 0**. **Expect 5–30+ minutes**; most work (embeddings, classification,
duckdb build) runs server-side on Modal, so long quiet stretches are **normal —
not a hang**. It prints a final `{"status": "completed", ...}` on success and
writes progress to `.powerpacks/runs/setup-gmail-modal/status.json`; if that lags
the live stdout, **trust the running process**. Do not treat pre-existing
`.powerpacks/search-index/` files as this run's output — confirm with Step 9.
Reassure the user every poll while it runs.

### Step 9 — Validate the search index

```bash
cd "$REPO" && uv run --project . python packs/indexing/primitives/validate_search_index/validate_search_index.py
```

JSON with `status` (`ok`/`fail`/`missing`), per-table row counts, `total_people`,
`summary`. Pass only on `status: ok` (exit 0); on `fail`/`missing` (exit 1) report
the `errors`. Echo the `summary`.

---

## Done

Report a terse summary: N Gmail accounts synced, contacts imported, merged network
of M people, index validated. Remind the user that rerunning `$import-gmail`
reruns the whole checklist: an explicit history window is rescanned with
`--noresume`, while msgvault deduplicates stored messages and preserves its db.
LinkedIn (`$setup`) and iMessage/WhatsApp (`$import-messages`)
are separate skills.
