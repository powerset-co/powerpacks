"""Messages import vertical: the contacts-direct import and the local matcher.

Modules, not a re-export surface — import `importer`, `match_local_candidates`,
or `util` directly.

Changelog:
  2026-07-30 (shim deleted): the 13-name re-export block from `importer` is gone.
    Nothing imported it (grep-verified: every caller, test, and skill names the
    concrete module), so it was a second, competing import path for the same
    objects.
"""
