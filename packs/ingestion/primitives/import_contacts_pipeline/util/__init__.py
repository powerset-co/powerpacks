"""Shared helpers for the import stage, split out of the stage modules.

`parsing` — tiny tolerant field parsers (bools, ints, names, phone digits).
`floor`   — the deterministic "worth researching" candidate floor + the
            message-contact field readers (channels, counts, last activity).
"""
