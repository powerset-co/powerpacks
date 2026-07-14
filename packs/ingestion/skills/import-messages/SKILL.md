---
name: import-messages
description: Add iMessage/WhatsApp contacts to your local search index. Use for $import-messages. Discovers message contacts (Full Disk Access + WhatsApp QR), matches LinkedIn/Gmail people, deep-researches eligible unresolved contacts, and requires review before any Messages rows materialize. Then fan-in merges sources and rebuilds the index through a workspace-shared Modal volume. Never uploads to a Powerset set.
---

<!--
Created: 2026-06-20
Changelog:
- 2026-07-13: Added the product architecture guide; documented both OpenRouter
  calls, Parallel and Modal payloads, Contacts.app inclusion, provider gates, and
  the current explicit-exclusion review semantics.
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

For a product-level walkthrough, source-specific privacy map, provider payloads,
approval gates, and architecture diagram, see
[`message-import-pipeline.md`](../../docs/message-import-pipeline.md).

It runs a **fixed checklist and always reruns it end to end**, idempotent against
fixed paths.

## How to run this skill

**FIRST, create a literal, visible checklist with all ten steps below and step
through it, marking each complete as you go.** Mandatory (TaskCreate / update_plan
/ your harness's todo tool). Seed it with these exact titles:

```
0. Check Powerset runtime credentials
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
- **No Powerset set upload.** Resolve message contacts using your already-imported
  Gmail + LinkedIn data plus approved provider calls. Never call
  `sync_powerset_candidates`. Powerpacks never selects message bodies, but wacli
  owns its local provider store; OpenRouter/Parallel receive the stated metadata,
  and the reviewed merged `people.csv` is uploaded to a workspace-shared Modal
  volume to build the downloadable index. Inputs/runs are operator-prefixed,
  caches are shared, and a missing operator ID uses the all-zero path.
- **Review is required whenever a row can be materialized.** Message contacts are **never** persisted into
  `import/messages/people.csv` without the user reviewing them first. Step 5 (the
  review UI + explicit "done") is mandatory when unresolved candidates exist;
  never auto-approve it or run Step 6 before the user confirms. The reviewer must
  explicitly exclude every unwanted row. If the current research queue is empty,
  show the outcome counts, mark provider research/review as no-ops, and run the
  explicit empty-source reconciliation before fan-in; the review builder cannot
  open an empty first-run artifact.
- **Consent gates (pause for the user):** macOS Full Disk Access (Step 2);
  Homebrew installs requested by the WhatsApp child and the WhatsApp QR scan
  (Step 2); OpenRouter name triage, OpenRouter/direct OpenAI
  scoring, and Parallel deep research
  (Steps 4-5); contact review, any new-row import confirmation, and potential
  RapidAPI profile-cache misses (Steps 5-6);
  and the Modal cloud upload/provider-backed indexing (Step 8).

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
**skip**. Record the choice. Messages **pull all history** — there is *no*
sync-window question (unlike Gmail). If the user skips, stop. Source extraction
is local and the flow never uploads to a Powerset set; approved provider and
Modal boundaries are listed in Guardrails.

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

(Drop `--include-imessage` or `--include-whatsapp` if that channel wasn't picked.)
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
gmail + linkedin people from `$setup`/`$import-gmail` — so you don't pay to
re-research people already in your index. Combine the per-source people.csv (same
schema, one header) and match. The optional `--candidates` catalog is deliberately
omitted, so only those local Gmail/LinkedIn rows participate:

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
against — skip and everyone goes to research in Step 4.) Matched contacts are
excluded from paid research in Step 4; they're already searchable via their
gmail/linkedin source, so this run does not re-add them to the Messages people.csv.

### Step 4 — Deep-research discovered contacts

Triage who is "worth enriching", queue the unmatched, then deep-research them.
The first OpenRouter call sends contact names only. Estimate it first, show the
estimate, and obtain explicit approval before `review`:

```bash
cd "$REPO"
uv run --project . python packs/ingestion/primitives/llm_review_contacts/llm_review_contacts.py estimate \
  --input .powerpacks/messages/contacts.csv
# Only after the user approves the displayed estimate:
uv run --project . python packs/ingestion/primitives/llm_review_contacts/llm_review_contacts.py review \
  --input .powerpacks/messages/contacts.csv
uv run --project . python packs/ingestion/primitives/prepare_research_queue/prepare_research_queue.py prepare \
  --input .powerpacks/messages/contacts.csv \
  --output .powerpacks/messages/research_queue.csv
```

The standalone `review` primitive does not enforce the orchestrator's historical
$10 auto-approval threshold; the explicit estimate and approval above are the
gate. **If the research queue is empty**, there are no eligible unresolved rows;
that may mean local matches, OpenRouter `skip=yes`, or missing/blocked/unsearchable
names. Report those counts separately rather than claiming everyone matched. Then
mark the remaining research and review steps (4-5) as no-ops, explicitly clear the
prior Messages source slice, and continue to fan-in. Do not invoke the review
builder: without per-handle research artifacts it exits nonzero rather than opening
an empty review. The reconciliation command writes header-only source files, removes
prior Messages rows from the shared directory, and invalidates the stale review:

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/messages.py reconcile-empty
```

Otherwise
estimate, then — **Parallel.ai spend gate: show the count + estimated cost and
ask before running** — run deep research:

```bash
cd "$REPO"
uv run --project . python packs/ingestion/primitives/deep_research_contacts/deep_research_contacts.py estimate \
  --input .powerpacks/messages/research_queue.csv --output-dir .powerpacks/messages/research
# Only after the user approves the spend:
uv run --project . python packs/ingestion/primitives/deep_research_contacts/deep_research_contacts.py run \
  --input .powerpacks/messages/research_queue.csv --output-dir .powerpacks/messages/research
```

### Step 5 — Review contacts & approve for import

Build the review CSV from the research, then open the review UI. Building the CSV
performs a second scoring call over the public research profile plus
phone, area code, message counts, source, timestamps, group names, and any
retarget hint. It prefers OpenRouter but falls back to direct OpenAI when only
`OPENAI_API_KEY` exists. The standalone primitive currently lacks a dry-run
estimate or an internal approval gate, so disclose the selected provider and
payload and obtain explicit approval before running `build`:

```bash
cd "$REPO"
uv run --project . python packs/ingestion/primitives/build_research_review_csv/build_research_review_csv.py build \
  --research-dir .powerpacks/messages/research \
  --queue-csv .powerpacks/messages/research_queue.csv \
  --output-csv .powerpacks/messages/research_review.csv
uv run --project . python packs/ingestion/primitives/review_research_web/review_research_web.py serve \
  --csv .powerpacks/messages/research_review.csv \
  --research-dir .powerpacks/messages/research --open
```

Tell the user the review URL and that they must **explicitly exclude** anyone they
do not want indexed. Today the materializer can retain a researched row with a
LinkedIn URL when `exclude` is blank, even if the UI initially renders it as
unselected. **For a non-empty review this is a mandatory hard user-action gate:
never auto-approve it, and never start Step 6 until the user explicitly says they
are done.** Any row not explicitly excluded may become searchable; do not describe
the behavior as "only clicked rows import."

### Step 6 — Import reviewed message contacts

Materialize the reviewed contacts into this source's canonical
`.powerpacks/network-import/import/messages/people.csv` — the file the Step 7
merge reads — and update the shared `directory.csv`. It converts your reviewed
`research_review.csv` into the people schema, applying your Step 5 exclusions,
deduping by LinkedIn identity, and attaching the message `interaction_counts`:

This command can send eligible LinkedIn URLs to RapidAPI when the local profile
cache misses. The import stage has no active cost preview or approval gate, so
disclose that payload and obtain explicit current-run approval before invoking
it.

```bash
cd "$REPO" && uv run --project . python packs/ingestion/primitives/import_contacts_pipeline/messages.py run
```

Rows not explicitly rejected can be written; the Step 5 instruction to exclude
every unwanted row is therefore load-bearing. If it blocks with an import-confirmation,
re-run with `--confirm-import`. The new people.csv is then picked up by the merge
in Step 7.

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

This uploads the complete merged CSV, including reviewed phone and interaction
metadata, to a workspace-shared Modal volume. Input/run paths are prefixed by
`POWERPACKS_OPERATOR_ID`, caches are shared, and a missing operator ID falls back
to the all-zero path. Modal may use provider-backed classification and embeddings,
and the current default `--max-usd 0` is uncapped internal mode. Show that boundary
and obtain explicit approval first.

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

Report a terse summary: channels linked, N contacts discovered, K reviewed and
not explicitly excluded, merged network of M people, index validated. Remind the user that
rerunning `$import-messages` reruns the whole checklist, and that LinkedIn
(`$setup`) and Gmail (`$import-gmail`) are separate skills.
