# Code hygiene (audit rules) 🧹

Created: 2026-07-23
Changelog: (temporary file — will fold into AGENTS.md; agents MUST read this before editing/moving code)

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

## Structure
- Primitives match pipeline stages: `discover_contacts_pipeline/`, `import_contacts_pipeline/`, `enrich/`, `deep_context/`, `logbook/`, `setup/` — with per-vertical subpackages (`gmail/`, `messages/`, `linkedin/`, `twitter/`) and a vertical-local `util.py`. No flat primitive dumps, no huge files.
- Every CLI entry file keeps its `if __name__ == "__main__"` guard (file-path invocation is how skills run them — a missing guard is a silent no-op).

## Moves & deletions
- Verify consumers by REAL imports/invocations (grep code, not doc mentions) before keeping or deleting.
- Delete dead code together with its tests and listing entries. No legacy flags/modes — migrate stored state to the source of truth (`overrides/review.csv` etc.), then read from it.
- Moving files: `git mv`, update EVERY reference (skills, tests, docs, bin, adapters, `py_cmd` strings), finish with a zero-stale-reference grep. Tests that `mock.patch` module globals must import the concrete submodule, not a package `__init__` re-export.

## Output & tests
- CLIs emit JSON; status reporting is terse one-liners.
- Test fixtures use obviously-synthetic data only — never real contact PII.
