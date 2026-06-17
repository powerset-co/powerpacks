# 🔧 App Self-Management Plan — Update · Secrets · Reboot

> Created: 2026-06-17
>
> **Goal:** three GUI buttons in the Powerpacks Console — (1) **update to latest**
> (pull + install + refresh), (2) **secrets/readiness check** (do they have Modal +
> other secrets?), (3) **is this daemonized? → reboot-yourself** (install, launch
> new, kill old — the "yank the batteries and fall on it" reboot). All grounded in
> what already exists (`file:line` from a read-only sweep on 2026-06-17).
>
> **Changelog**
> - 2026-06-17: initial plan.

---

## 0. 🧭 What already exists (so we wire, not rebuild)

| Capability | Status today | Where |
|---|---|---|
| **Daemon / launch / restart** | ✅ **already built** — `start\|stop\|status\|restart\|run\|daemon-install\|daemon-uninstall\|daemon-status`, launchd plist (`RunAtLoad`+`KeepAlive`), PID file | `scripts/run-powerpacks-console.sh` (actions ~80; plist ~246-282); PID `.powerpacks/servers/powerpacks-console.pid` |
| **Button → backend → poll** | ✅ pattern exists — POST endpoint → `startSetupJob(action, cmd[], timeout)` spawns child, returns job id, FE polls `GET /local-api/setup/jobs/{id}` every 2s | `app/local-api/jobs.ts:18-121`; routes `app/local-api/routes/onboarding.ts`; FE `app/src/local/powerpacksApi.ts` |
| **Update scripts** | ✅ exist — `git pull --ff-only` + `sync-agent-files.sh` + `install.sh <host>` | `bin/update-claude-code`, `bin/update-codex`, `install.sh` |
| **Secrets/readiness** | ✅ partial — `GET /local-api/env/status` (per-key present/missing + `summary.ready`); `bin/doctor run` JSON; `pull_runtime_keys check` (Modal/OpenAI); `auth.py whoami` (Powerset login) | `app/local-api/routes/env.ts:97-168`; `packs/powerset/primitives/doctor/doctor.py`; `pull_runtime_keys.py:152-170` |
| **Remote-latest version check** | ❌ **does NOT exist** — nothing fetches/compares remote version or git hash | — |
| **App serving** | `npm run dev` (Vite + local-api **dev-only plugin**), port 5177. Prod build = static SPA, **no backend** | `scripts/run-powerpacks-console.sh:164-185`; `app/vite.config.ts`; `app/local-api/powerpacksLocalApiPlugin.ts` |

**Implication:** Feature 3 (daemon/reboot) is ~80% built, Feature 2 (secrets) ~70%, Feature 1 (update) needs the one missing remote-check piece + a GUI trigger. All three reuse the existing job-spawn-poll pattern and a new route file `app/local-api/routes/system.ts`.

Version source of truth: `pyproject.toml` (`powerpacks` 0.8.0) + `app/package.json` & `.release-please-manifest.json` (`powerpacks-console` 0.7.0) + git tags `powerpacks-vX.Y.Z` / `powerpacks-console-vX.Y.Z`.

---

## 1. ⬆️ Feature 1 — "Update to latest" button

**Two-step UX: check (cheap, read-only) → apply (guarded).**

- **A. Check for updates** — `GET /local-api/system/update-status` → spawn `git -C <repo> fetch --quiet` then compare `git rev-parse @` vs `git rev-parse origin/main` (and tag vs `.release-please-manifest.json`). Return `{ current_hash, latest_hash, behind: n, current_version, latest_version, dirty: bool }`. *(Future: curl `api.github.com/repos/powerset-co/powerpacks/commits/main` for the hash without a local fetch once the repo is public — the user's "curl the hash" idea.)*
- **B. Update now** — `POST /local-api/system/update` → `startSetupJob("update", [...])` running the host's updater (`bin/update-claude-code` or `bin/update-codex`; detect host from which skills dir exists / env marker). That does `git pull --ff-only` + `sync-agent-files.sh` + `install.sh <host>`. **Guards:** refuse if `dirty` (offer stash), ff-only only.
- **C. Self-refresh** — two cases:
  - FE/skills-only change → on job `completed`, `window.location.reload()`.
  - local-api / Vite / app code changed → the running dev server is stale → **requires a server restart** → hand off to Feature 3's reboot flow ("Update applied — restart to finish").

**Open Qs:** which host(s) to support (claude-code / codex / both)? Auto-restart after update or prompt?

---

## 2. 🔑 Feature 2 — "Secrets / readiness check" button

**One endpoint that answers "do they have Modal + xyz secrets?" in one call.**

- **A. Unified endpoint** — `GET /local-api/system/readiness` aggregating:
  - `env/status` → API keys (`OPENAI_API_KEY`, `RAPIDAPI_LINKEDIN_KEY`, `PARALLEL_API_KEY`, `APOLLO_API_KEY`, optional `TURBOPUFFER_API_KEY`/`DATABASE_URL`).
  - `pull_runtime_keys check` → **Modal tokens** (`MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`) + OpenAI — **this is the gap**: `env/status` intentionally omits Modal, so the readiness endpoint must call `pull_runtime_keys` for it.
  - `auth.py whoami` → Powerset login (`logged_in`/`anonymous`, expiry).
  - (optional) `bin/doctor run` for the full picture + `fix_command`/`fix_kind` per item.
  - Return `{ services: [{ name, status: ok|missing|warn, missing: [...], fix }], ready: bool }`.
- **B. GUI rows** per service with status + fix: Modal → `$powerset env pull` (after login); OpenAI/RapidAPI/Parallel → inline entry (already writable via `env.ts` `WRITABLE_ENV_KEYS`); Powerset login → browser login.
- **Gap to close:** add Modal-token coverage (via `pull_runtime_keys check`) into the unified endpoint, since `env/status` alone misses it.

---

## 3. ♻️ Feature 3 — "Is this daemonized? → reboot-yourself" button

**The meme: yank the batteries, fall on it, it comes back up. ~80% already built in `run-powerpacks-console.sh`.**

- **A. Daemon status** — `GET /local-api/system/daemon-status` → spawn `run-powerpacks-console.sh daemon-status` → `{ daemonized: bool, pid, port, running, plist_path }`.
- **B. Daemonize + relaunch** — `POST /local-api/system/daemonize` → `startSetupJob` running a **detached** (`start_new_session`) script that: `daemon-install` (writes launchd plist, `RunAtLoad`+`KeepAlive`) → launch new instance → `kill $(cat …console.pid)` (old). Must be detached so killing the parent Vite process doesn't abort the job mid-flight.
- **C. Survive the self-kill** — the endpoint that triggers the restart **loses its own HTTP response when the parent dies.** Handle it:
  1. Endpoint returns immediately with `{ job_id, expected_url: "http://localhost:5177" }`.
  2. FE shows "Restarting…", then **polls a health endpoint** on 5177 until it answers, then `reload()`.
  3. Add `GET /local-api/health` (cheap 200) so the FE can detect "new server is up."

**Open Q:** confirm-before-kill UX (a "yes, reboot" modal) — destructive-ish (drops the current session briefly).

---

## 4. 🧱 Cross-cutting

- **New route file** `app/local-api/routes/system.ts` for all three (`update-status`, `update`, `readiness`, `daemon-status`, `daemonize`, `health`).
- **Reuse** `startSetupJob` + `GET /local-api/setup/jobs/{id}` polling + `.powerpacks/runs/job-logs/<action>.log`. No new job machinery.
- **Spend/safety:** update = git/install only (free); readiness = read-only; daemonize/reboot = local process only (free). No paid APIs. Confirm before kill/restart and before `git pull` on a dirty tree.
- **The only hard part** is §3.C (HTTP response lost on self-kill) → detached spawn + FE health-poll + reload. Everything else is wiring existing primitives to endpoints + buttons.

---

## 5. ✅ Validation (per the pipeline doc's discipline — real path, cheap, force state)

- **Update:** on a deliberately-behind clone, assert `update-status` reports `behind>0`; assert `update` is ff-only and **refuses a dirty tree**; assert version bump after.
- **Secrets:** with a key removed from `.env`, assert `readiness` flips that service to `missing` and surfaces the right `fix`; assert Modal tokens are actually covered (the gap).
- **Daemon/reboot:** `daemon-status` false→true after install; after reboot, assert a process is **listening on 5177** and the FE health-poll reconnects + reloads; assert old PID is gone.

---

## 6. ❓ Open questions for review

1. Which host(s) does "update" target — claude-code, codex, or both (auto-detect)?
2. Remote-latest source — local `git fetch` now, or GitHub API tag/hash (needs public repo) later?
3. Update flow — auto-restart after pull, or prompt "restart to finish"?
4. Where do these buttons live — a new **Settings / System** page in the console?
5. Reboot confirmation UX — modal "yes, reboot" before the self-kill?
