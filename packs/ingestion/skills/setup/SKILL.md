---
name: setup
description: Deterministic Powerpacks setup. Use for $setup. Steps through update, Powerset login, runtime-key pull, systematic Gmail/msgvault setup, Gmail sync/import, LinkedIn import, local-only Messages (iMessage/WhatsApp) link/import, source merge, Modal index, and search-index validation. Always reruns the full checklist; overwrites in place.
---

<!--
Created: 2026-06-17
Changelog:
- 2026-06-17: Promoted the former $setup-beta to $setup. Setup now does the full
  multi-source flow: systematic msgvault/Gmail setup + the unified merge+index
  path (fan-in -> Modal index-people), with LinkedIn enriched on Modal
  (import-linkedin). Replaces the prior LinkedIn-only fast path.
- 2026-06-18: Added local-only Messages (iMessage + WhatsApp) ingestion as Steps
  11-16 (link/pull -> local match vs gmail+linkedin -> triage + Parallel deep
  research -> review -> persist to import/messages/people.csv). No Powerset and
  no upload. Renumbered merge/index/validate to 17-19.
- 2026-06-19: Made the Step 15 review mandatory/un-skippable. Renamed Messages
  step titles to plain discover -> match -> deep-research -> review/approve ->
  import language (nothing is "imported" until the user approves in Step 15);
  clarified Step 16 as the per-source import that feeds the fan-in merge.
-->

# setup

`$setup` connects your sources and builds one local search index: same prereqs,
then **systematic Gmail/msgvault setup**, then a **unified merge + index** that
combines LinkedIn + Gmail into one local search index.

It runs a **fixed checklist and always reruns it end to end**. Reruns
are idempotent against fixed paths. The one exception is the msgvault store
(`~/.msgvault/msgvault.db`): it is the durable, incrementally-synced archive —
**never delete it**; re-syncs resume from the last message via `--after`.

## How to run this skill

**FIRST, before running anything: create a literal, visible checklist with all
twenty steps below and step through it, marking each item complete as you go.**
Mandatory. Use your harness's plan/todo/task tool:

- **Claude Code:** `TaskCreate` one task per step (0–19), then `TaskUpdate` each
  to `in_progress` then `completed`.
- **Codex:** `update_plan` with the twenty steps, updating status as you go.
- **Any other harness:** its equivalent todo/plan mechanism.

Seed the checklist with these exact item titles:

```
0.  Update Powerpacks (bin/update-codex)
1.  Check Powerset login + credentials
2.  Log in to Powerset (only if not logged in)
3.  Pull runtime keys (Modal + OpenAI)
4.  Check msgvault status
5.  Ask which Gmail accounts to link + how far back to sync
6.  Create msgvault OAuth app (browser, if not configured)
7.  Authorize Gmail accounts
8.  Sync Gmail archives
9.  Import Gmail contacts
10. Import LinkedIn Connections.csv
11. Choose Messages sources (iMessage / WhatsApp)
12. Link & discover message contacts (Full Disk Access + WhatsApp QR)
13. Match contacts against LinkedIn & Gmail
14. Deep-research discovered contacts
15. Review contacts & approve for import
16. Import approved message contacts
17. Merge all sources
18. Index the merged network
19. Validate the search index
```

Some steps are conditional (2 if already logged in, 6 if OAuth is already
configured, 11–16 if the user skips Messages or one of the message channels).
Keep them in the checklist and mark them complete as a no-op when they don't
apply — do not drop them.

Then:

1. **Work the checklist in order 0 → 19.** Exactly one item `in_progress` at a
   time; mark it `completed` before the next. No batching, reordering, skipping.
   One-line result per step.
2. **Run from the canonical repo root.** Resolve once and `cd` there (see *Repo
   root*). `.powerpacks/...` paths are relative to that root.
3. **Deterministic & in-place.** Overwrite the fixed derived paths
   (`.powerpacks/network-import/...`, `.powerpacks/search-index/...`); rely on
   the primitives to overwrite. Don't pre-delete with `rm` and don't invent
   timestamped/alternate folders. **Never delete `~/.msgvault/msgvault.db`** —
   it is the durable Gmail archive and re-syncs incrementally.

### Guardrails (hard rules)

- **No context pass. Do not go exploring.** This skill is self-contained and
  authoritative. Do not read agent memory, prior state, other docs, or primitive
  source to re-derive paths. Build the checklist and execute it directly.
- **Do not edit code.** Only if you hit an actual blocking bug, and say so.
  Otherwise only invoke the primitives below.
- **Do not write scripts to do the work.** Reuse the exact primitive commands.
  Plain shell for `cp`/`test`/`wc`/`cat` is fine.
- **Never call `msgvault` (or `msgvault sync-full`) directly.** Gmail syncing
  happens *only* through Step 8's `gmail.py discover --sync-after "$SYNC_AFTER"`.
  A bare `msgvault sync-full <email>` has no date bound and pulls the entire
  mailbox history, ignoring the chosen window. Do not invent a "repair sync" or
  fall back to the raw binary. **Re-authorizing a lapsed token is
  `msgvault_setup.py add-account --force-auth` (OAuth only, no sync) — see Step 7;
  it is never a `sync-full`.** If discover fails, recover by syncing *less* (a
  narrower `--sync-after` window), never the full mailbox; if that still fails,
  surface the error and stop.
- **Messages is local-only.** Steps 12–16 resolve message contacts using your
  already-imported Gmail + LinkedIn data plus Parallel.ai deep research **only**.
  Never call Powerset (`sync_powerset_candidates`) and never upload. No message
  bodies are read, only contact metadata.
- **Messages review is always required.** Message contacts are **never**
  persisted into `import/messages/people.csv` without the user reviewing them
  first. Step 15 (the review UI + explicit "done") is mandatory on every run —
  never auto-approve it, never skip it, and never run Step 16 before the user
  confirms. `import/messages/people.csv` contains only the rows the reviewer
  keeps.
- **Consent gates (pause for the user):** Powerset browser login; msgvault
  browser/gcloud OAuth-app creation and Gmail account authorization; Gmail
  Parallel.ai spend at/above the auto-approve threshold (Step 5); and, for
  Messages, **macOS Full Disk Access (Step 12), the WhatsApp QR scan (Step 12),
  the LLM "worth enriching" + Parallel deep-research spend (Step 14), and the
  contact review (Step 15)**. Everything else runs without asking.

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

### Step 0 — Update Powerpacks

```bash
cd "$REPO" && bin/update-codex
```

### Step 1 — Check Powerset login + credentials

```bash
test -f "$HOME/.powerpacks/credentials.json" && echo "credentials.json: present" || echo "credentials.json: MISSING"
cd "$REPO" && uv run --project . python packs/powerset/primitives/auth/auth.py whoami
```

If missing or `whoami` fails → Step 2. Otherwise Step 2 is a no-op → Step 3.

### Step 2 — Log in (only if Step 1 said not logged in)

```bash
cd "$REPO" && uv run --project . python packs/powerset/primitives/auth/auth.py login
```

Browser consent. If it can't open, print the URL for the user. Re-run `whoami`.

### Step 3 — Pull provisioned runtime keys (Modal + OpenAI)

```bash
cd "$REPO" && uv run --project . python packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py pull --env-file .env
```

Verify: `… pull_runtime_keys.py check --env-file .env`.

### Step 4 — Check msgvault status

Safe, local. Drives the next two steps:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py status
```

The JSON reports `gcloud`, `config.oauth_configured`, `database.exists`, and
authorized `accounts`. If `oauth_configured` is true and the db exists, Step 6 is
a no-op.

### Step 5 — Ask which Gmail accounts to link (and how far back to sync)

Ask the user for **every** Gmail address they want searchable, in one prompt.
Treat the first as the **primary** (used to create the OAuth app in Step 6); the
rest are additional accounts authorized in Step 7. Compare against the already
`accounts` from Step 4 — only accounts not already authorized need Steps 6–7.
Record the list and carry it into the next two steps. Do not guess emails.

**In the same prompt, ask how far back to sync.** How many years of mail should
be archived? **Default is 3 years.** The user may answer **`all`** (full mailbox
history) or any number of years (e.g. **1**, **2**, **5**). Record the answer as
`$SYNC_YEARS` and carry it into Step 8. A wide window — especially `all` — makes
the first sync much longer; if the user asks for more than the 3-year default,
confirm before running Step 8.

### Step 6 — Create msgvault OAuth app (browser, if not configured)

Only if Step 4 showed OAuth not configured. One-time browser setup for the
**primary** Gmail account from Step 5 — **consent: drives Chrome + gcloud,
creates the Google OAuth Desktop app, inits the db, authorizes the primary
account**:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py browser-setup \
  --email <primary-gmail> --add-account --init-db
```

Writes `~/.msgvault/config.toml`, the client secret, and `~/.msgvault/msgvault.db`
(do not delete the db). If already configured, mark this step a no-op.

### Step 7 — Authorize Gmail accounts

For each **additional** account from Step 5 (the primary is already authorized) —
**consent: per-account browser OAuth grant**:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py add-account --email <email>
```

Re-run `msgvault_setup.py status` and confirm every requested account appears
under `accounts`.

**Re-authorizing a lapsed account.** If Step 4 (or a later sync error) shows an
account that is *already in the vault* but whose token is **expired, revoked, or
missing**, re-authorize it the same way — adding `--force-auth` so msgvault
replaces the stale token with a fresh OAuth grant. This is **OAuth only; it
downloads no mail**:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/msgvault_setup/msgvault_setup.py add-account --email <email> --force-auth
```

The account keeps every previously-synced message; Step 8's bounded `discover`
then **resumes from the last synced message** (`--after`) and downloads only what
is new. **Never** re-authorize by running `msgvault sync-full <email>` — it has
no resume bound and re-pulls the entire mailbox, which is exactly what turns a
re-auth into an hours-long full sync.

### Step 8 — Sync Gmail archives

For each authorized account, sync the archive and build the discover artifacts,
using the window chosen in Step 5. Compute `SYNC_AFTER` from `$SYNC_YEARS`
(default `3`; `all` = full history) and pass it via `--sync-after` so the sync is
bounded:

```bash
cd "$REPO"
# $SYNC_YEARS from Step 5: a number (default 3) or the word "all".
if [ "${SYNC_YEARS:-3}" = "all" ]; then
  SYNC_AFTER="2004-01-01"   # pre-Gmail = the entire mailbox
else
  SYNC_AFTER="$(date -v-${SYNC_YEARS:-3}y +%Y-%m-%d 2>/dev/null || date -d "${SYNC_YEARS:-3} years ago" +%Y-%m-%d)"
fi
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/gmail.py discover \
  --account-email <email> --sync-after "$SYNC_AFTER"
```

(Repeat per account, or omit `--account-email` for all linked accounts.) Writes
`.powerpacks/network-import/discover/gmail/<account>/`. The 3-year window is the
default; if the user asks for more/less history, change `-v-3y` (e.g. `-v-5y`)
or pass a specific `--sync-after YYYY-MM-DD`. A large first sync can take a while;
msgvault skips already-downloaded messages on reruns.

**Only sync through this `discover` command.** Do not run `msgvault sync-full`
(or any raw `msgvault` command) yourself — it has no `--after` bound and will
pull the entire mailbox (years past the 3-year window). `discover` is what
passes `--after "$SYNC_AFTER"` to msgvault. If `discover` errors or the sync is
too slow/large, recover by syncing **less, never more**: pass a more recent
`--sync-after` (e.g. `-v-1y`, or a specific later date) so it covers a smaller
window. Never fall back to the raw binary or an unbounded full sync. If a
narrower window still fails, surface the error and stop.

### Step 9 — Import Gmail contacts

Import resolves contacts to LinkedIn via **Parallel.ai**, then writes
`.powerpacks/network-import/import/gmail/people.csv`. Run **without** the spend
flag first — the primitive auto-approves small batches (under its threshold) and
otherwise blocks:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/gmail.py run
```

- If it completes (small batch auto-approved), proceed.
- **If it blocks reporting pending Parallel contacts** (at/above the threshold),
  tell the user the contact count and ask to approve the spend. On approval:

  ```bash
  cd "$REPO" && uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/gmail.py run --approve-parallel-spend
  ```

### Step 10 — Import LinkedIn Connections.csv

Ask the user for their `Connections.csv` path. Place it at the canonical input
(overwrite), then enrich it **on Modal** — the same shared enrichment + cache
prod uses — and download the enriched people.csv to the path the merge reads:

```bash
cd "$REPO"
DEST=".powerpacks/network-import/discover/linkedin/Connections.csv"
mkdir -p "$(dirname "$DEST")"
cp -f "<user-csv-path>" "$DEST"
uv run --project . python packs/indexing/modal/linkedin_modal_pipeline.py import-linkedin --csv "$DEST"
```

This runs only the Modal import/enrich stage (no local DuckDB) and writes the
enriched `.powerpacks/network-import/import/linkedin/people.csv` for the merge.
Because enrichment runs on Modal it needs no local RapidAPI key, and the shared
volume cache keeps reruns cheap. It can take a few minutes; the command prints
progress.

### Step 11 — Choose Messages sources (iMessage / WhatsApp)

Ask the user whether to add **Messages** to the local index, and which channels:
**iMessage**, **WhatsApp**, **both**, or **skip**. Record the choice. Messages
**pull all history** — there is *no* sync-window question here (unlike Gmail). If
the user skips Messages, mark Steps 12–16 complete as a no-op and go to Step 17.

This flow is **local only**: it resolves contacts with your already-imported
Gmail + LinkedIn data plus Parallel.ai deep research. It **never** calls Powerset
and **never** uploads (see Guardrails).

### Step 12 — Link & discover message contacts

Discover message contacts with the contacts orchestrator in **selective mode**
(extract + merge only — it stops after the merge and never touches Powerset).
Nothing is imported here; this only discovers who you've messaged. Pass only the
channels chosen in Step 11:

```bash
cd "$REPO" && uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py run \
  --include-imessage --include-whatsapp --include-contact-merge
```

(Drop `--include-imessage` or `--include-whatsapp` if that channel wasn't picked.)
It writes `.powerpacks/messages/contacts.csv`, pausing at consent gates; resolve
each, then re-run with `continue` (same flags) to advance:

- **iMessage Full Disk Access** (`status: blocked_user_action`, step `check_imessage`):
  open the macOS pane, ask the user to enable Full Disk Access for this terminal,
  wait for confirmation, then `continue`:

  ```bash
  cd "$REPO" && uv run --project . python packs/messages/primitives/extract_imessage_contacts/extract_imessage_contacts.py open-privacy-settings
  ```

- **WhatsApp QR / expired session** (`status: blocked_user_action`, step
  `authenticate_whatsapp`): surface the QR page, have the user scan it in
  WhatsApp, then `continue`. (Default provider is wacli.)

**Consent gates: Full Disk Access, WhatsApp QR.** The run completes with
`selected_steps_completed` once contacts are merged.

### Step 13 — Match contacts against LinkedIn & Gmail

Resolve contacts you already have **for free** by matching against the enriched
gmail + linkedin people from Steps 9–10 — so you don't pay to re-research people
already in your index. Combine the per-source people.csv (same schema, one
header) and match; pass **no** `--candidates`, so no Powerset catalog is fetched:

```bash
cd "$REPO"
GM=".powerpacks/network-import/import/gmail/people.csv"
LI=".powerpacks/network-import/import/linkedin/people.csv"
LOCAL=".powerpacks/messages/_local_people.csv"
{ [ -f "$GM" ] && cat "$GM" || cat "$LI"; } > "$LOCAL"
[ -f "$GM" ] && [ -f "$LI" ] && tail -n +2 "$LI" >> "$LOCAL"
uv run --project . python packs/messages/primitives/match_local_candidates/match_local_candidates.py match \
  --contacts .powerpacks/messages/contacts.csv --local-people "$LOCAL"
```

Matched contacts are excluded from paid research in Step 14; they're already
searchable via their gmail/linkedin source, so this run does not re-add them to
the Messages people.csv.

### Step 14 — Deep-research discovered contacts

Triage who is "worth enriching", queue the unmatched, then deep-research them:

```bash
cd "$REPO"
uv run --project . python packs/messages/primitives/llm_review_contacts/llm_review_contacts.py review \
  --input .powerpacks/messages/contacts.csv
uv run --project . python packs/messages/primitives/prepare_research_queue/prepare_research_queue.py prepare \
  --input .powerpacks/messages/contacts.csv \
  --output .powerpacks/messages/research_queue.csv
```

`llm_review` uses OpenRouter and auto-approves small batches (under its ~$10
threshold); if it blocks for spend, show the cost and ask. **If the research
queue is empty** (everyone matched), skip the rest of this step. Otherwise
estimate, then — **Parallel.ai spend gate: show the count + estimated cost and
ask before running** — run deep research:

```bash
cd "$REPO"
uv run --project . python packs/messages/primitives/deep_research_contacts/deep_research_contacts.py estimate \
  --input .powerpacks/messages/research_queue.csv --output-dir .powerpacks/messages/research
# Only after the user approves the spend:
uv run --project . python packs/messages/primitives/deep_research_contacts/deep_research_contacts.py run \
  --input .powerpacks/messages/research_queue.csv --output-dir .powerpacks/messages/research
```

### Step 15 — Review contacts & approve for import

Build the review CSV from the research, then open the review UI:

```bash
cd "$REPO"
uv run --project . python packs/messages/primitives/build_research_review_csv/build_research_review_csv.py build \
  --research-dir .powerpacks/messages/research \
  --queue-csv .powerpacks/messages/research_queue.csv \
  --output-csv .powerpacks/messages/research_review.csv
uv run --project . python packs/messages/primitives/review_research_web/review_research_web.py serve \
  --csv .powerpacks/messages/research_review.csv \
  --research-dir .powerpacks/messages/research --open
```

Tell the user the review URL and that they should **exclude** anyone they don't
want indexed. **This review is mandatory and a hard user-action gate: always run
it, never auto-approve, never skip it, and never start Step 16 until the user
explicitly says they are done.** Only rows they keep become searchable. (If
Step 14 produced no contacts to review, the review opens empty — still confirm
with the user before moving on; nothing is persisted in that case.)

### Step 16 — Import approved message contacts

The Messages equivalent of **Step 9 (Import Gmail)** and **Step 10 (Import
LinkedIn)**: it produces this source's canonical
`.powerpacks/network-import/import/messages/people.csv` — the file the Step 17
merge actually reads — and updates the shared `directory.csv`. It converts your
reviewed `research_review.csv` into the people schema, applying your Step 15
exclusions, deduping by LinkedIn identity, and attaching the message
`interaction_counts`:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/messages.py run
```

**Why this isn't optional / why Step 17 can't just pick it up:** the merge looks
for each source's `import/<source>/people.csv`. It does **not** read the messages
`contacts.csv` or `research_review.csv` — those are intermediate, different-schema
(`handle`/`bucket`/`exclude`/`network_*`) artifacts under `.powerpacks/messages/`,
not the people schema. Until this step writes `import/messages/people.csv`, there
is nothing for Step 17 to merge from messages.

Only reviewer-kept rows are written. If it blocks with an import-confirmation,
re-run with `--confirm-import`. If it surfaces an `enrich_people` spend gate
(LinkedIn profile enrichment), handle it like Step 9. The new people.csv is then
picked up by the merge in Step 17.

### Step 17 — Merge all sources

Fan-in merges the per-source `import/<source>/people.csv` files into one network:

```bash
cd "$REPO" && uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py fan-in \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Writes `.powerpacks/network-import/merged/people.csv` (default
`--include-existing-artifacts` picks up gmail + linkedin + messages).

### Step 18 — Index the merged network

Index the merged people.csv on Modal (generic indexer, no import stage) and
download the duckdb:

```bash
cd "$REPO" && uv run --project . python packs/indexing/modal/linkedin_modal_pipeline.py index-people \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Run it in the background and keep Step 12 `in_progress` until the command
**exits 0**. This stage is long and mostly quiet — set expectations and don't
panic:

- **Expect 5–30+ minutes.** Most of the work (embeddings, role/company
  classification, duckdb build) runs **server-side on Modal**, so the local
  process can print little or nothing for **many minutes at a stretch**. A long
  silence with the process still alive is **normal and expected — not a hang**.
  Do not interrupt it, do not retry, do not declare failure on quiet.
- **The authoritative signal is the process itself**, not a status file: it
  stays running until done and prints a final `{"status": "completed", ...}` on
  success. `index-people` writes its progress to
  `.powerpacks/runs/setup-gmail-modal/status.json` (stages `enriching` →
  `importing` → `indexing` → `completed`) — poll that, **not**
  `setup-linkedin-modal/status.json`, which belongs to a different (prod
  `pipeline`) run and will look stale/"importing" forever here. If the gmail
  status file lags the live stdout, **trust the running process and its stdout.**
- **Do not treat pre-existing files in `.powerpacks/search-index/` as this run's
  output.** They may be left over from a prior run. The index is done only when
  the command exits 0 and has freshly downloaded `local-search.duckdb` +
  `manifest.json`. Confirm with Step 19, not by eyeballing the directory.

While it runs, reassure the user in plain language every poll, e.g. "Still
indexing on Modal (~N min in) — this stage is quiet by design; the job is alive
and I'm waiting for it to finish." Then proceed to Step 19 once it exits 0.

### Step 19 — Validate the search index

```bash
cd "$REPO" && uv run --project . python packs/indexing/primitives/validate_search_index/validate_search_index.py
```

JSON with `status` (`ok`/`fail`/`missing`), per-table row counts,
`total_people`, `summary`. Pass only on `status: ok` (exit 0); on `fail`/
`missing` (exit 1) report the `errors`. Echo the `summary`.

---

## Done

Report a terse summary: logged in as <email>, keys pulled, N Gmail accounts
synced, LinkedIn imported, merged network of M people, index validated. Remind
the user that rerunning `$setup` reruns the whole checklist (Gmail re-syncs
incrementally; the msgvault db is preserved).
