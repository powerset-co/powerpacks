---
name: setup
description: Deterministic LinkedIn-only Powerpacks setup. Use for $setup. Steps through update, a supported Powerset workspace or an already-prepared custom Modal workspace, LinkedIn Connections.csv import on Modal, source merge, Modal indexing, and local search-index validation. Always reruns the full checklist; overwrites in place. For Gmail use $import-gmail; for iMessage/WhatsApp use $import-messages.
---

<!--
Created: 2026-06-17
Changelog:
- 2026-06-17: Rewrote setup as a deterministic, rerunnable checklist.
- 2026-06-17: Promoted to a unified LinkedIn+Gmail+Messages multi-source flow.
- 2026-06-20: Split back to LinkedIn-only. Gmail moved to $import-gmail and
  iMessage/WhatsApp to $import-messages (each does its source + fan-in merge +
  Modal index). $setup now: update -> login -> keys -> import LinkedIn ->
  fan-in -> index -> validate. Uses the modern import -> fan-in -> index path so
  the $import-* skills can merge their sources on top of LinkedIn.
- 2026-07-09: Step 0 runs the updater for the CURRENT harness (bin/update-codex
  was hardcoded — a Claude Code run refreshed Codex's skills, never its own).
  Step 6: note the status.json path's historical name (index-people reuses the
  Gmail progress dir; the path is correct for LinkedIn runs).
- 2026-07-12: Step 1 is now an explicit choice — ask whether the user has a
  Powerset account to log in with (initializing .env from the hosted template
  on yes) instead of silently defaulting into Powerset login. The original
  custom-workspace route skipped login and checked local keys.
- 2026-07-13: Corrected the custom-workspace contract: local keys alone do not
  mount provider secrets into a sandbox. A custom Modal workspace must already
  contain the named OpenAI and RapidAPI secrets. Documented the current need
  for a unique `POWERPACKS_OPERATOR_ID` in shared workspaces.
- 2026-07-13: Step 6 now sets a count-based runtime expectation from the
  LinkedIn import summary and distinguishes warm-cache runs from cache-cold
  first runs so agents do not mistake a long, quiet Modal build for a hang.
-->

# setup

`$setup` connects **LinkedIn** and builds one local search index: update, Powerset
login, runtime keys, then import your LinkedIn `Connections.csv` (enriched on
Modal), merge, index, and validate.

It runs a **fixed checklist and always reruns it end to end**. Reruns are
idempotent against fixed paths; rely on the primitives to overwrite.

**Other sources are their own skills** (each adds its source on top of whatever
is already imported, then re-merges + re-indexes):
- **Gmail** → `$import-gmail`
- **iMessage / WhatsApp** → `$import-messages`

## How to run this skill

**FIRST, before running anything: create a literal, visible checklist with all
eight steps below and step through it, marking each item complete as you go.**
Mandatory. Use your harness's plan/todo/task tool:

- **Claude Code:** `TaskCreate` one task per step (0–7), then `TaskUpdate` each
  to `in_progress` then `completed`.
- **Codex:** `update_plan` with the eight steps, updating status as you go.
- **Any other harness:** its equivalent todo/plan mechanism.

Seed the checklist with these exact item titles:

```
0. Update Powerpacks (this harness's updater)
1. Choose credentials (Powerset or prepared Modal workspace)
2. Log in to Powerset (Powerset route, only if not logged in)
3. Pull or verify Modal access and provider secrets
4. Import LinkedIn Connections.csv
5. Merge all sources
6. Index the merged network
7. Validate the search index
```

Steps 2 and 3 depend on the Step 1 choice (Step 2 is a no-op on the
custom-workspace route or when already logged in) — keep them in the checklist
and mark them complete.

Then:

1. **Work the checklist in order 0 → 7.** Exactly one item `in_progress` at a
   time; mark it `completed` before the next. No batching, reordering, skipping.
   One-line result per step.
2. **Run from the canonical repo root.** Resolve once and `cd` there (see *Repo
   root*). `.powerpacks/...` paths are relative to that root.
3. **Deterministic & in-place.** Overwrite the fixed derived paths
   (`.powerpacks/network-import/...`, `.powerpacks/search-index/...`); rely on
   the primitives to overwrite. Don't pre-delete with `rm` and don't invent
   timestamped/alternate folders.

### Guardrails (hard rules)

- **No context pass. Do not go exploring.** This skill is self-contained and
  authoritative. Do not read agent memory, prior state, other docs, or primitive
  source to re-derive paths. Build the checklist and execute it directly.
- **Do not edit code.** Only if you hit an actual blocking bug, and say so.
  Otherwise only invoke the primitives below.
- **Do not write scripts to do the work.** Reuse the exact primitive commands.
  Plain shell for `cp`/`test`/`wc`/`cat` is fine.
- **Consent gates (pause for the user):** the Step 1 Powerset-account question
  (when the request didn't already answer it) and the Powerset browser login
  (Step 2). The LinkedIn import runs enrichment on Modal (no local key, no
  extra spend prompt). Everything else runs without asking.

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

Run the updater for the harness you are running in:

```bash
cd "$REPO" && bin/update-codex          # Codex
cd "$REPO" && bin/update-claude-code    # Claude Code
cd "$REPO" && adapters/pi/install.sh    # Pi
```

### Step 1 — Choose credentials (Powerset or prepared Modal workspace)

The import + index steps need access to a Modal workspace whose sandboxes can
mount the provider secrets used by the driver. A provisioned Powerset account
is the supported path. An advanced custom workspace works only when it already
contains Modal secrets named `powerset-openai` and `powerset-rapidapi` (or the
RapidAPI backup override is configured). A local `OPENAI_API_KEY` alone is not
forwarded into the sandbox. Decide the route in this order:

1. **The request already answered it.** "… using my Powerset account" (or any
   explicit ask to use Powerset) → **Powerset route**, no question. An explicit
   "without Powerset" / "my own workspace" → **custom-workspace route**, no question.
2. **Already logged in.** If `.env` exists, check:

   ```bash
   test -f "$HOME/.powerpacks/credentials.json" && echo "credentials.json: present" || echo "credentials.json: MISSING"
   cd "$REPO" && uv run --env-file .env --project . python packs/powerset/primitives/auth/auth.py whoami
   ```

   If `whoami` succeeds, say "already logged in to Powerset as <email> — using
   that account" and take the **Powerset route**.
3. **Otherwise ask the user and wait** (consent gate):

   > Do you have a Powerset account you'd like to log in with? It provisions
   > the supported Modal workspace this setup needs. If not, you need your own
   > Modal workspace with `powerset-openai` and `powerset-rapidapi` secrets
   > already configured, plus a working token or Modal profile.

   Yes → **Powerset route**. No → **custom-workspace route**.

**Powerset route only** — make sure `.env` carries the hosted Powerset config:

```bash
cd "$REPO"
[ -f .env ] || { cp packs/powerset/templates/env.powerset.example .env; chmod 600 .env; }
```

If `.env` already exists, preserve its secrets and other settings; only align
the public Powerset URL/Auth0 values with
`packs/powerset/templates/env.powerset.example` if they drifted.

### Step 2 — Log in (Powerset route, only if Step 1 said not logged in)

Custom-workspace route, or already logged in: mark complete as a no-op. Otherwise:

```bash
cd "$REPO" && uv run --env-file .env --project . python packs/powerset/primitives/auth/auth.py login
```

Browser consent. If it can't open, print the URL for the user. Re-run `whoami`.

### Step 3 — Pull or verify Modal access and provider secrets

**Powerset route** — pull the user's provisioned keys:

```bash
cd "$REPO" && uv run --env-file .env --project . python packs/powerset/primitives/pull_runtime_keys/pull_runtime_keys.py pull --env-file .env
```

Verify: `… pull_runtime_keys.py check --env-file .env`.

**Custom-workspace route** — no provisioning call. The driver accepts Modal
credentials from `.env` or an existing `~/.modal.toml` profile. Verify that the
selected workspace is reachable and already contains the named
`powerset-openai` secret. It also needs either a `powerset-rapidapi` secret or a
non-empty local `RAPIDAPI_LINKEDIN_KEY_BACKUP` override. Create the local env
file if the Modal profile is the only credential source, because the remaining
setup commands consistently load it. `secret list --json` returns names and
metadata, not secret values:

```bash
cd "$REPO"
[ -f .env ] || { touch .env; chmod 600 .env; }
uv run --env-file .env --project . modal secret list --json
```

Do not proceed until `powerset-openai` is present and either
`powerset-rapidapi` is present or `RAPIDAPI_LINKEDIN_KEY_BACKUP` is configured.
Check only whether the override is non-empty; never print its value. A local
`OPENAI_API_KEY` does not create or replace the workspace OpenAI secret.

**Current namespace limitation for either route:** `$setup` does not provision
a user-specific `POWERPACKS_OPERATOR_ID`. If a workspace administrator supplied
one, put it in `.env` before upload. Otherwise the driver uses an all-zero
namespace. Surface that as single-operator/development behavior and do not
claim multi-user input/run isolation.

### Step 4 — Import LinkedIn Connections.csv

Ask the user for their `Connections.csv` path. Place it at the canonical input
(overwrite), then enrich it **on Modal** — the same shared enrichment + cache
prod uses — which writes the enriched people.csv to the path the merge reads:

```bash
cd "$REPO"
DEST=".powerpacks/network-import/discover/linkedin/Connections.csv"
mkdir -p "$(dirname "$DEST")"
cp -f "<user-csv-path>" "$DEST"
uv run --env-file .env --project . python packs/indexing/modal/linkedin_modal_pipeline.py import-linkedin --csv "$DEST"
```

This runs only the Modal import/enrich stage (no local DuckDB) and writes the
enriched `.powerpacks/network-import/import/linkedin/people.csv` for the merge.
Because enrichment runs on Modal it needs no local RapidAPI key, and the shared
volume cache keeps reruns cheap. It can take a few minutes; the command prints
progress. Record the command's reported connection count for the Step 6 runtime
estimate (use the merged people count from Step 5 if the import summary is not
available).

### Step 5 — Merge all sources

Fan-in merges the per-source `import/<source>/people.csv` files into one network
(LinkedIn here; also Gmail/Messages if you've run those skills):

```bash
cd "$REPO" && uv run --env-file .env --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py fan-in \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Writes `.powerpacks/network-import/merged/people.csv` (default
`--include-existing-artifacts` picks up every imported source).

### Step 6 — Index the merged network

Index the merged people.csv on Modal (generic indexer, no import stage) and
download the duckdb:

```bash
cd "$REPO" && uv run --env-file .env --project . python packs/indexing/modal/linkedin_modal_pipeline.py index-people \
  --people-csv .powerpacks/network-import/merged/people.csv
```

Run it in the background and keep Step 6 `in_progress` until the command
**exits 0**. This stage is long and mostly quiet — set expectations and don't
panic:

- **Estimate from the Step 4 connection count** (or Step 5 merged people count)
  and state the estimate before launching the command. These are planning
  ranges, not deadlines:

  | Connections / people | Expected Modal index time |
  | ---: | ---: |
  | Up to 1,000 | 10–30 minutes |
  | 1,001–5,000 | 20–45 minutes |
  | 5,001–10,000 | 30–75 minutes |
  | 10,001–20,000 | 60–120 minutes |
  | More than 20,000 | 90 minutes–3 hours |

  Shared role/company/embedding caches usually put a repeat run toward the low
  end. A first run with many uncached companies can approach the high end. For
  example, about 15,000 connections should be introduced as **roughly a
  one-hour warm-cache run; allow up to two hours if cache-cold**.
- Most work (embeddings, role/company classification, cache materialization,
  and DuckDB build) runs **server-side on Modal**, so the local process can
  print little or nothing for **many minutes at a stretch**. A long silence with
  the process still alive is **normal and expected — not a hang**. Do not
  interrupt it, retry it, or declare failure because output is quiet.
- **The authoritative signal is the process itself**, not a status file: it stays
  running until done and prints a final `{"status": "completed", ...}` on
  success. `index-people` writes progress to
  `.powerpacks/runs/setup-gmail-modal/status.json` (the path name is historical —
  index-people reuses the Gmail progress dir for every vertical, including this
  LinkedIn run; stages `enriching` → `importing` → `indexing` → `completed`) —
  poll that, but if it lags the live stdout, **trust the running process and its
  stdout.**
- Poll the same process and status file about every **5 minutes**, or when the
  harness reports new output. A harness command/display timeout is not proof of
  failure if the original process is still alive. Do not launch a replacement
  indexer while that process exists.
- **Do not treat pre-existing files in `.powerpacks/search-index/` as this run's
  output.** They may be left over from a prior run. The index is done only when
  the command exits 0 and has freshly downloaded `local-search.duckdb` +
  `manifest.json`. Confirm with Step 7, not by eyeballing the directory.

While it runs, update the user only when the phase changes or roughly every
5–10 minutes, e.g. "Still indexing on Modal (~N min in; estimated X–Y min) —
the original job is alive." Then proceed to Step 7 once it exits 0.

### Step 7 — Validate the search index

```bash
cd "$REPO" && uv run --env-file .env --project . python packs/indexing/primitives/validate_search_index/validate_search_index.py
```

JSON with `status` (`ok`/`fail`/`missing`), per-table row counts,
`total_people`, `summary`. Pass only on `status: ok` (exit 0); on `fail`/
`missing` (exit 1) report the `errors`. Echo the `summary`.

---

## Done

Report a terse summary: credential route (logged in as <email> + keys pulled,
or prepared custom Modal workspace verified), LinkedIn imported, merged network of M
people, index validated. Remind the user that rerunning
`$setup` reruns the whole checklist, and that **Gmail** (`$import-gmail`) and
**iMessage/WhatsApp** (`$import-messages`) are separate skills that add their
source on top and re-merge + re-index.
