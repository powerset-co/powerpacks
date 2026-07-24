# Code hygiene (audit rules) 🧹

Created: 2026-07-23
Changelog: (temporary file — will fold into AGENTS.md; agents MUST read this before editing/moving code)
  - 2026-07-23 (class-sharing pass): added "Shared homes" + "Orchestrator pattern"
    sections after the five OO refactors landed.

## Imports
- One import list at the top. Never duplicated try/except import blocks, never inline/conditional imports.
- Script+module compatibility uses the ONE bootstrap stanza (before the first `packs.*` import; it cannot live in `__init__.py` — script mode never imports the package):
  ```python
  _REPO_ROOT = Path(__file__).resolve().parents[N]  # N = depth to repo root
  if str(_REPO_ROOT) not in sys.path:
      sys.path.insert(0, str(_REPO_ROOT))
  ```
- No bare-module `sys.path` hacks to reach sibling primitives — use package imports.

## Docstrings & comments
- Every function has a docstring stating CURRENT behavior, present tense.
- Change history never lives in function docstrings or inline comments — it goes in a module-top `Changelog:` block (dated) inside the module docstring.
- Inline comments explain invariants and WHY (phase walkthroughs, dedup keys, loud-failure guards) — comment non-obvious flows heavily; never narrate the edit you just made.

## Signatures & config
- Wide config-style functions: keyword-only (`*`). STRICT — no `**kwargs` catch-alls (they hide typos and never-honored options).
- One name per concept. No parameter aliases.
- Layered config resolves in ONE function returning a frozen dataclass; precedence documented once: explicit caller/CLI override > packaged state (e.g. accounts.json) > config defaults. `| None = None` params are inherit-sentinels — document the convention where used.

## Typed payloads
- Stage manifests / ledgers are dataclasses (per-vertical `models.py`, or a shared dataclass for shared shapes) — never ad-hoc dicts. A stage cannot invent fields inline.

## Shared homes (don't re-implement these)
- **Typed stage manifests** live in `primitives/common/manifests.py`: `StagePayload`
  (base for the per-vertical `models.py` dataclasses) + `write_stage_manifest`
  (fingerprinted, no-op-when-unchanged) + the fingerprint helpers. Any stage that
  writes a fingerprinted stage manifest reaches here — do not copy the chain into a
  new vertical's `common`. (`imports/common.py` keeps a *separate*, intentionally
  divergent `write_manifest` + `collect_artifact_paths` — different dedup/match
  rules and a source-derived path — that is a different contract; leave it alone.)
- **The spend gate** lives in `primitives/common/gates.py`: `EXIT_NEEDS_APPROVAL`
  (the `--approve-spend`-gated `run` exit code), `exit_code_for_status`,
  `manifest_emit_payload` (the terse CLI JSON from a typed orchestrator manifest),
  and `needs_approval_payload` (the canonical **step-gate** shape:
  step/provider/estimated_calls/message[/continue_command]). A spend-gated
  orchestrator that names a blocked step uses `needs_approval_payload`. A gate with
  a genuinely different shape (e.g. enrich_people's *credit-gate*: reason/
  paid_call_count/cache_hit_count/estimated_credits) keeps its own literal — don't
  bend one shape onto the other just for symmetry, but DO use the shared exit code +
  emit helpers.

## Orchestrator pattern (copy the shape, not a base class)
The discover/import verticals converged on the same OO shape, but their bodies
differ enough that a shared base class would own nothing. Copy the SHAPE; do not
extract a base:
- **Channel class** (`MessageChannel`/`IMessageChannel`/`WhatsAppChannel`,
  `GmailAccountChannel`): one source. Owns its fixed output paths (read from the
  module-level constants at call time so tests can patch them) and its
  extract/sync -> normalize/spawn subprocess chain; records what it contributed on
  `self.artifacts`; its `run()` returns `None` on success or a blocked/failed
  payload that short-circuits the store.
- **Store/orchestrator** (`MessagesDiscovery`, `GmailDiscovery`): owns the output
  dir (the ONE mkdir), the enabled channels, the run loop (stop at the first
  blocked/failed channel), the merge, and the single typed stage manifest.
- Do NOT hoist a shared base for these. The failure payloads differ (typed
  `GmailDiscoveryFailed` vs the `blocked_child`/`failed_child` dicts, which live
  only in `messages/discover.py`), and the merge bodies differ (a subprocess
  `merge_contacts` union vs the gmail incremental/full-recount merge plan). A base
  would own a ~3-line loop whose failure handling also differs. Keep the shape
  consistent across new verticals; keep the bodies local.

## Docs & READMEs
- NO per-file `<name>.README.md` sidecars. A module's usage/docs live in its own
  module docstring (behavior + CLI usage, present tense, terse).
- At most ONE `README.md` per directory, and it describes the directory as a
  whole — never a single file.

## Structure
- Primitives match pipeline stages: `discover/`, `imports/`, `enrich/`, `deep_context/`, `logbook/`, `setup/` — with per-vertical subpackages (`gmail/`, `messages/`, `linkedin/`, `twitter/`) and a vertical-local `util.py`. No flat primitive dumps, no huge files.
- Every CLI entry file keeps its `if __name__ == "__main__"` guard (file-path invocation is how skills run them — a missing guard is a silent no-op).
- Large automation drivers (browser flows, gcloud orchestration, …) decompose
  into a clearly-named subpackage (e.g. `setup/automations/`) of ~200–300-line
  single-concern modules; the original CLI path stays as a thin
  argparse+dispatch entry so skill commands don't change.

## Moves & deletions
- Verify consumers by REAL imports/invocations (grep code, not doc mentions) before keeping or deleting.
- Delete dead code together with its tests and listing entries. No legacy flags/modes — migrate stored state to the source of truth (`overrides/review.csv` etc.), then read from it.
- Moving files: `git mv`, update EVERY reference (skills, tests, docs, bin, adapters, `py_cmd` strings), finish with a zero-stale-reference grep. Tests that `mock.patch` module globals must import the concrete submodule, not a package `__init__` re-export.

## Output & tests
- CLIs emit JSON; status reporting is terse one-liners.
- Test fixtures use obviously-synthetic data only — never real contact PII.
