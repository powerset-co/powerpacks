"""Read the saved Logbook for the People page: its entries, one entry's conversations,
one conversation's messages.

Flow: every read parses ``<root>/manifest.json`` (``logbook_export.build_logbook``
writes it) into ``Entry`` rows. GET `/api/people/logbook/entries` lists them, each
person entry named with the People parent whose build writes that slug
(``logbook_people.parent_slugs``); GET `/api/people/logbook/entry?slug=` adds the
entry's conversations; GET `/api/people/logbook/conversation?slug=&path=` reads one
conversation file the manifest lists for that entry and parses the markdown
``EntryWriter`` wrote back into messages. Only files already on disk are read: nothing
builds, syncs or writes, and a request string is only ever a key into the manifest.

Changelog:
  2026-09-30: created.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.logbook.logbook_common import MANIFEST_JSON
from packs.ingestion.primitives.logbook.logbook_people import parent_slugs
from packs.ingestion.primitives.share.web.logbook_participants import Participant, email_participants

NO_ENTRY = "No saved logbook here."
NO_CONVERSATION = "This conversation isn't in the logbook."
MISSING_FILE = "This conversation's file is missing from the logbook."

# logbook_export._format_message: "**<at[:16]> · <sender>:**", then the text on the same
# line, or after a blank line when it is long or multiline. at[:16] of an ISO timestamp is
# "YYYY-MM-DD HH:MM"; a message without one is "unknown-date".
_HEADER = re.compile(r"^\*\*(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?|unknown-date) · (.*?):\*\*(?: (.*))?$")
_UNKNOWN_DATE = "unknown-date"
# EntryWriter.write: "\n## <year>\n\n" before the first message of each year.
_YEAR = re.compile(r"^## \d{4}$")


@dataclass(frozen=True)
class Conversation:
    """One file of an entry: a Gmail thread, a DM or a group chat."""

    # The manifest's rel_path: the key the page sends back to read it.
    path: str
    container_id: str
    channel: str
    kind: str
    title: str
    messages: int
    first_at: str | None
    last_at: str | None


@dataclass(frozen=True)
class Entry:
    """One person or group of the saved Logbook."""

    slug: str
    name: str
    kind: str
    # The People parent whose build writes this slug; None for groups and CLI-built entries.
    parent_id: str | None
    messages: int
    conversations: tuple[Conversation, ...]

    def summary(self) -> dict[str, Any]:
        dates = [at for row in self.conversations for at in (row.first_at, row.last_at) if at]
        return {
            "slug": self.slug,
            "name": self.name,
            "kind": self.kind,
            "parent_id": self.parent_id,
            "messages": self.messages,
            "channels": sorted({row.channel for row in self.conversations}),
            "first_at": min(dates, default=None),
            "last_at": max(dates, default=None),
        }


@dataclass(frozen=True)
class Message:
    # As the file wrote it, "YYYY-MM-DD HH:MM"; "" when the store had no date.
    at: str
    sender: str
    text: str


def _conversation(meta: dict[str, Any]) -> Conversation:
    return Conversation(
        path=meta["rel_path"],
        container_id=meta["container_id"],
        channel=meta["channel"],
        kind=meta["kind"],
        title=meta["container_title"],
        messages=int(meta["messages"]),
        first_at=meta["first_at"] or None,
        last_at=meta["last_at"] or None,
    )


class LogbookArchive:
    def __init__(self, db: Db, root: Path, *, gmail_store: Path) -> None:
        self.db = db
        self.root = root
        self.gmail_store = gmail_store

    def entries(self) -> tuple[Entry, ...]:
        """Every saved entry, each conversation most recent first; none before the first build."""
        return self._read(parent_slugs(self.db))

    def entry(self, slug: str) -> Entry:
        """The saved entry; an unknown slug raises ``LookupError``."""
        return self._find(slug, parent_slugs(self.db))

    def conversation(self, slug: str, path: str) -> tuple[tuple[Message, ...], tuple[Participant, ...] | None]:
        """One conversation the entry lists; any other path raises ``LookupError``."""
        conversation = next((row for row in self._find(slug, {}).conversations if row.path == path), None)
        if conversation is None:
            raise LookupError(NO_CONVERSATION)
        root = self.root.resolve()
        file = (root / path).resolve()
        if not file.is_relative_to(root / slug) or not file.is_file():
            raise LookupError(MISSING_FILE)
        participants = email_participants(self.gmail_store, conversation.container_id) if conversation.channel == "gmail" else None
        return parse_conversation(file.read_text(encoding="utf-8")), participants

    def _find(self, slug: str, parents: dict[str, str]) -> Entry:
        found = next((entry for entry in self._read(parents) if entry.slug == slug), None)
        if found is None:
            raise LookupError(NO_ENTRY)
        return found

    def _read(self, parents: dict[str, str]) -> tuple[Entry, ...]:
        manifest = self.root / MANIFEST_JSON.name
        if not manifest.exists():
            return ()
        raw = json.loads(manifest.read_text(encoding="utf-8"))["entries"]
        return tuple(
            Entry(
                slug=slug,
                name=entry["name"] or slug,
                kind=entry["kind"],
                parent_id=parents.get(slug) if entry["kind"] == "person" else None,
                messages=int(entry["messages"]),
                conversations=tuple(sorted((_conversation(meta) for meta in entry["containers"].values()),
                                           key=lambda row: row.last_at or "", reverse=True)),
            )
            for slug, entry in raw.items()
        )


def entries_payload(entries: tuple[Entry, ...]) -> dict[str, Any]:
    return {"entries": [entry.summary() for entry in entries]}


def entry_payload(entry: Entry) -> dict[str, Any]:
    return {**entry.summary(), "conversations": [{key: value for key, value in asdict(row).items() if key != "container_id"}
                             for row in entry.conversations]}


def conversation_payload(messages: tuple[Message, ...], participants: tuple[Participant, ...] | None) -> dict[str, Any]:
    return {"messages": [asdict(message) for message in messages],
            "participants": [asdict(person) for person in participants] if participants is not None else None}


def parse_conversation(markdown: str) -> tuple[Message, ...]:
    """The messages of one conversation file: its frontmatter and ``# title`` skipped, the
    ``## year`` markers dropped, every other line kept in the message it follows."""
    lines = markdown.split("\n")
    start = lines.index("---", 1) + 1 if lines and lines[0] == "---" else 0
    messages: list[list[str]] = []
    heads: list[tuple[str, str]] = []
    for position in range(start, len(lines)):
        line = lines[position]
        header = _HEADER.match(line)
        if header:
            at, sender, text = header.groups()
            heads.append(("" if at == _UNKNOWN_DATE else at, sender))
            messages.append([] if text is None else [text])
        elif messages and not _is_year_marker(lines, position):
            messages[-1].append(line)
    return tuple(
        Message(at, sender, _body(body))
        for (at, sender), body in zip(heads, messages, strict=True)
    )


def _is_year_marker(lines: list[str], position: int) -> bool:
    """EntryWriter's exact marker: a blank line, ``## <year>``, a blank line, then a message of
    that year. A body line of the same text sits against its message or another marker."""
    line = lines[position]
    if not _YEAR.match(line) or position + 2 >= len(lines) or lines[position - 1] or lines[position + 1]:
        return False
    header = _HEADER.match(lines[position + 2])
    return header is not None and header.group(1).startswith(line.removeprefix("## "))


def _body(lines: list[str]) -> str:
    # A long or multiline text sits between blank lines after its header (_format_message).
    return "\n".join(lines).strip("\n")
