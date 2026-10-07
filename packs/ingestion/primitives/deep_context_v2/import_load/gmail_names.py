"""The written name of a Gmail candidate, read from the mail archive's headers.

Every message header names its sender and recipients as the mail client wrote them. One address
collects several spellings over the years ("Erin McCarthy" 33 times, "Erin from Harmonic" once).
The most frequent one is the person's name; the rest are noise. A header that is blank or is the
address itself names nobody. Nothing is dropped for disagreeing: a client's nickname once does not
cost the person their name.

Created: 2026-10-06
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# Every header that named an address, with how often each spelling appeared. Sender and recipient
# headers both count; the participant row is the archive's own pick and counts once more.
HEADER_NAMES_SQL = """
SELECT lower(p.email_address) AS email, trim(r.display_name) AS name, COUNT(*) AS seen
FROM message_recipients r
JOIN participants p ON p.id = r.participant_id
WHERE p.email_address IS NOT NULL AND r.display_name IS NOT NULL AND trim(r.display_name) != ''
GROUP BY lower(p.email_address), trim(r.display_name)
"""

QUOTES = "\"'“”‘’"


def _clean(name: str) -> str:
    """Quotes off the ends, single spaces inside."""
    return " ".join(name.strip().strip(QUOTES).split())


def header_names(msgvault_db: Path) -> dict[str, str]:
    """address -> the name most often written for it in the archive's headers. An address whose
    headers only ever carried the address itself, or nothing, is absent."""
    conn = sqlite3.connect(f"file:{msgvault_db}?mode=ro", uri=True)
    counts: dict[str, dict[str, int]] = {}
    for email, raw, seen in conn.execute(HEADER_NAMES_SQL):
        name: str = _clean(raw)
        # The address written as the name names nobody.
        if not name or "@" in name:
            continue
        per_address: dict[str, int] = counts.setdefault(email, {})
        per_address[name] = per_address.get(name, 0) + seen
    conn.close()
    best: dict[str, str] = {}
    for email, per_address in counts.items():
        # Most seen wins; a tie goes to the first alphabetically, so a rerun picks the same name.
        winner: str = ""
        winner_seen: int = 0
        for name in sorted(per_address):
            if per_address[name] > winner_seen:
                winner = name
                winner_seen = per_address[name]
        best[email] = winner
    return best
