"""Messages discovery vertical: the discovery store, its channels, and the
per-source leaf extractors.

Modules, not a re-export surface — import `discover`, `channels.*`,
`extract_imessage`, `extract_whatsapp`, `whatsapp_wacli`, `merge_contacts`, or
`models` directly. The channels own their `IMESSAGE_*` / `WHATSAPP_*` path
constants; test patches already target those concrete modules.

Changelog:
  2026-07-30 (shim deleted): the 16-name re-export block is gone. Nothing
    imported it (grep-verified: `imports/status.py`, `pipeline/graph.py`,
    `deep_context/collect_person_context.py`, and every test name the concrete
    module), so it was a second, competing import path for the same objects —
    and a package `__init__` that pulls in every submodule makes `import
    packs...discover.messages.models` drag the wacli client along with it.
"""
