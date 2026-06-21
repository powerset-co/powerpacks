---
name: import-messages
description: Add iMessage/WhatsApp contacts to your local search index. Use for $import-messages. Discovers message contacts (Full Disk Access + WhatsApp QR), matches them against your LinkedIn/Gmail people, deep-researches the unmatched (Parallel.ai), opens a mandatory review, then fan-in merges all imported sources and rebuilds the Modal search index. Local only — never uploads to Powerset.
---

<!--
Created: 2026-06-20
Changelog:
- 2026-06-20: New skill, split out of $setup (replaces $import-contacts for the
  local-index use case). Carries the Messages block (choose channels -> discover
  -> match vs gmail+linkedin -> deep-research -> review -> import) plus the shared
  fan-in + Modal index + validate. Local only: no Powerset sync and no upload.
-->

# import-messages

`$import-messages` adds **iMessage / WhatsApp** contacts to your local search
index: discover who you've messaged, match them against your already-imported
LinkedIn/Gmail people (free), deep-research the rest, **review and approve**, then
**merge all imported sources + rebuild the index**. Run `$setup` (LinkedIn) and
`$import-gmail` first for the best matching — Messages merges on top of whatever
is already imported.

It runs a **fixed checklist and always reruns it end to end**, idempotent against
fixed paths.

## How to run this skill

**FIRST, create a literal, visible checklist with all ten steps below and step
through it, marking each complete as you go.** Mandatory (TaskCreate / update_plan
/ your harness's todo tool). Seed it with these exact titles:

```
0. Check prereqs (Powerset login + runtime keys)
1. Choose Messages sources (iMessage / WhatsApp)
2. Link & discover message contacts (Full Disk Access + WhatsApp QR)
3. Match contacts against LinkedIn & Gmail
4. Deep-research discovered contacts
5. Review contacts & approve for import
6. Import approved message contacts
7. Merge all sources
8. Index the merged network
9. Validate the search index
```

Then: **work the checklist 0 → 9, one item `in_progress` at a time**; run from the
canonical repo root (resolve once, see *Repo root*); overwrite fixed derived paths
and rely on the primitives.

### Guardrails (hard rules)

- **No context pass. Do not go exploring.** Self-contained and authoritative.
  Build the checklist and execute it directly.
- **Do not edit code.** Only invoke the primitives below. Plain shell for
  `cp`/`test`/`wc`/`cat` is fine; no glue scripts.
- **Local only.** Resolve message contacts using your already-imported Gmail +
  LinkedIn data plus Parallel.ai deep research **only**. Never call Powerset
  (`sync_powerset_candidates`) and never upload. No message bodies are read, only
  contact metadata.
- **Review is always required.** Message contacts are **never** persisted into
  `import/messages/people.csv` without the user reviewing them first. Step 5 (the
  review UI + explicit "done") is mandatory on every run — never auto-approve it,
  never skip it, and never run Step 6 before the user confirms. `people.csv`
  contains only the rows the reviewer keeps.
- **Consent gates (pause for the user):** macOS Full Disk Access (Step 2); the
  WhatsApp QR scan (Step 2); the LLM "worth enriching" + Parallel deep-research
  spend (Step 4); and the contact review (Step 5).

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

If `whoami` fails or keys are missing, tell the user to run **`$setup`** first and
stop here. (Matching in Step 3 is best when LinkedIn (`$setup`) and Gmail
(`$import-gmail`) are already imported.)

### Step 1 — Choose Messages sources (iMessage / WhatsApp)

Ask the user which channels to add: **iMessage**, **WhatsApp**, **both**, or
**skip**. Record the choice. Messages **pull all history** — there is *no*
sync-window question (unlike Gmail). If the user skips, stop. This flow is **local
only** (no Powerset, no upload — see Guardrails).

### Step 2 — Link & discover message contacts

Discover message contacts with the contacts orchestrator in **selective mode**
(extract + merge only — it stops after the merge and never touches Powerset).
Nothing is imported here; this only discovers who you've messaged. Pass only the
channels chosen in Step 1:

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

### Step 3 — Match contacts against LinkedIn & Gmail

Resolve contacts you already have **for free** by matching against the enriched
gmail + linkedin people from `$setup`/`$import-gmail` — so you don't pay to
re-research people already in your index. Combine the per-source people.csv (same
schema, one header) and match; pass **no** `--candidates`, so no Powerset catalog
is fetched:

```bash
cd "$REPO"
GM=".powerpacks/network-import/import/gmail/people.csv"
LI=".powerpacks/network-import/import/linkedin/people.csv"
LOCAL=".powerpacks/messages/_local_people.csv"
{ [ -f "$GM" ] && cat "$GM" || cat "$LI"; } > "$LOCAL" 2>/dev/null
[ -f "$GM" ] && [ -f "$LI" ] && tail -n +2 "$LI" >> "$LOCAL"
uv run --project . python packs/messages/primitives/match_local_candidates/match_local_candidates.py match \
  --contacts .powerpacks/messages/contacts.csv --local-people "$LOCAL"
```

(If neither gmail nor linkedin has been imported yet, there's nothing to match
against — skip and everyone goes to research in Step 4.) Matched contacts are
excluded from paid research in Step 4; they're already searchable via their
gmail/linkedin source, so this run does not re-add them to the Messages people.csv.

### Step 4 — Deep-research discovered contacts

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

### Step 5 — Review contacts & approve for import

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
it, never auto-approve, never skip it, and never start Step 6 until the user
explicitly says they are done.** Only rows they keep become searchable. (If Step 4
produced no contacts to review, the review opens empty — still confirm with the
user before moving on; nothing is persisted in that case.)

### Step 6 — Import approved message contacts

Materialize the reviewed contacts into this source's canonical
`.powerpacks/network-import/import/messages/people.csv` — the file the Step 7
merge reads — and update the shared `directory.csv`. It converts your reviewed
`research_review.csv` into the people schema, applying your Step 5 exclusions,
deduping by LinkedIn identity, and attaching the message `interaction_counts`:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/messages.py run
```

Only reviewer-kept rows are written. If it blocks with an import-confirmation,
re-run with `--confirm-import`. If it surfaces an `enrich_people` spend gate
(LinkedIn profile enrichment), show it and ask before approving. The new
people.csv is then picked up by the merge in Step 7.

### Step 7 — Merge all sources

Fan-in merges the per-source `import/<source>/people.csv` files into one network
(Messages here, plus LinkedIn/Gmail if already imported):

```bash
cd "$REPO" && uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py fan-in \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Writes `.powerpacks/network-import/merged/people.csv` (default
`--include-existing-artifacts` picks up every imported source).

### Step 8 — Index the merged network

Index the merged people.csv on Modal (generic indexer, no import stage) and
download the duckdb:

```bash
cd "$REPO" && uv run --project . python packs/indexing/modal/linkedin_modal_pipeline.py index-people \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Run it in the background and keep Step 8 `in_progress` until the command
**exits 0**. **Expect 5–30+ minutes**; most work runs server-side on Modal, so
long quiet stretches are **normal — not a hang**. It prints a final
`{"status": "completed", ...}` on success and writes progress to
`.powerpacks/runs/setup-gmail-modal/status.json`; if that lags the live stdout,
**trust the running process**. Do not treat pre-existing `.powerpacks/search-index/`
files as this run's output — confirm with Step 9. Reassure the user every poll.

### Step 9 — Validate the search index

```bash
cd "$REPO" && uv run --project . python packs/indexing/primitives/validate_search_index/validate_search_index.py
```

JSON with `status` (`ok`/`fail`/`missing`), per-table row counts, `total_people`,
`summary`. Pass only on `status: ok` (exit 0); on `fail`/`missing` (exit 1) report
the `errors`. Echo the `summary`.

---

## Done

Report a terse summary: channels linked, N contacts discovered, K approved &
imported, merged network of M people, index validated. Remind the user that
rerunning `$import-messages` reruns the whole checklist, and that LinkedIn
(`$setup`) and Gmail (`$import-gmail`) are separate skills.
