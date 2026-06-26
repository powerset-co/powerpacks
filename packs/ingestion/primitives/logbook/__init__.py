"""$logbook — raw verbatim message archive per person/group.

The inverse of ``deep_context``: instead of synthesizing facts and discarding the
text, this pipeline downloads EVERYTHING for a set of people across Gmail,
iMessage, and WhatsApp and formats it into readable, append-only markdown — one
file per email thread / DM / group, ``## YYYY`` headings inside, stable ids in the
frontmatter so future syncs append (never overwrite). No LLM, no network, no
spend — all reads are local SQLite. See ``packs/ingestion/skills/logbook``.
"""
