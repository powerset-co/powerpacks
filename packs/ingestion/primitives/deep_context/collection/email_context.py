"""Select bounded, signal-dense Gmail context from shared msgvault rows."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from typing import Protocol

from packs.ingestion.primitives.deep_context.collection.models import (
    EmailMessage,
    EmailRankedMessage,
)
from packs.ingestion.primitives.discover.gmail.msgvault import store as gni


class EmailRow(Protocol):
    def __getitem__(self, key: str) -> object: ...


class EmailContext:
    """Apply the one Gmail scoring, deduplication, and windowing policy."""

    DEFAULT_SNIPPET_CHARS = 200
    DEFAULT_HEAD_CHARS = 300
    DEFAULT_TAIL_CHARS = 300
    # Three candidates per output slot leaves room for per-thread ranking and dedup.
    CANDIDATE_ROWS_PER_OUTPUT = 3
    NEARDUP_THRESHOLD = 0.6
    QUOTE_CUT = re.compile(
        r"(?im)^\s*(on .{0,120}wrote:|-+\s*original message\s*-+|-+\s*forwarded message\s*-+|"
        r"from:\s.+|sent from my .+|get outlook for .+)\s*$"
    )
    SIGNAL_FEATURES = (
        (re.compile(r"\+?\d[\d().\-  ]{7,}\d"), 3),
        (
            re.compile(
                r"https?://|www\.|linkedin\.com/in/|github\.com/|"
                r"[a-z0-9-]+\.(?:com|io|co|org|net)\b",
                re.I,
            ),
            2,
        ),
        (re.compile(r"(?<![\w.])@[A-Za-z0-9_]{2,}"), 1),
        (
            re.compile(
                r"\b(?:co-?founder|founder|ceo|cto|coo|cfo|vp|head of|director|"
                r"principal|engineer|developer|manager|realtor|broker|partner|associate|"
                r"analyst|consultant|professor|lecturer|recruiter|designer|attorney|"
                r"architect|scientist)\b",
                re.I,
            ),
            2,
        ),
        (re.compile(r"\b(?:DRE|CalBRE|NMLS|License|Lic\.?)\s*#?\s*\d", re.I), 3),
        (
            re.compile(
                r"\b(?:at|@)\s+[A-Z][A-Za-z0-9&.\-]+"
                r"(?:\s+[A-Z][A-Za-z0-9&.\-]+)*"
            ),
            1,
        ),
    )

    def __init__(
        self,
        store: gni.MsgvaultStore,
        *,
        snippet_chars: int = DEFAULT_SNIPPET_CHARS,
        head_chars: int = DEFAULT_HEAD_CHARS,
        tail_chars: int = DEFAULT_TAIL_CHARS,
    ) -> None:
        self.store = store
        self.snippet_chars = snippet_chars
        self.head_chars = head_chars
        self.tail_chars = tail_chars

    @staticmethod
    def clean_text(value: object, limit: int | None = None) -> str:
        text = html.unescape(str(value or ""))
        text = re.sub(r"\s+", " ", text).strip()
        if limit is not None and 0 < limit < len(text):
            text = text[:limit]
        return text

    @classmethod
    def clean_body(cls, value: object, head_chars: int, tail_chars: int) -> str:
        """Keep new-message substance and its signature, excluding quoted history."""
        text = html.unescape(str(value or ""))
        cut = cls.QUOTE_CUT.search(text)
        if cut:
            text = text[: cut.start()]
        lines = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
        text = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if not text:
            return ""
        if len(text) <= head_chars + tail_chars:
            return text
        return f"{text[:head_chars].strip()} … {text[-tail_chars:].strip()}"

    @classmethod
    def signal_score(cls, text: str) -> int:
        """Score signature/bio features plus a small length bonus."""
        text = text or ""
        score = sum(weight for pattern, weight in cls.SIGNAL_FEATURES if pattern.search(text))
        return score + min(len(text) // 200, 3)

    @staticmethod
    def shingles(text: str, size: int = 3) -> frozenset[str]:
        tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(tokens) < size:
            return frozenset(tokens)
        return frozenset(" ".join(tokens[index : index + size]) for index in range(len(tokens) - size + 1))

    @staticmethod
    def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)

    def select_emails_from_rows(
        self,
        rows: Iterable[EmailRow],
        email: str,
        per_person: int,
        accounts: set[str],
    ) -> tuple[list[EmailMessage], int]:
        """Select contact/owner mail breadth-first, then depth, with near-dup removal."""
        dropped = 0
        by_thread: dict[tuple[str, object], list[EmailRankedMessage]] = {}
        for index, row in enumerate(rows):
            sender = str(row["sender_email"] or "").strip()
            if sender == email:
                from_role = "contact"
            elif sender and sender in accounts:
                from_role = "me"
            else:
                dropped += 1
                continue
            text = self.clean_body(row["body_text"], self.head_chars, self.tail_chars)
            text = text or self.clean_text(row["snippet"], self.snippet_chars)
            at = str(row["at"] or "").strip()
            rank = (self.signal_score(text), 1 if from_role == "contact" else 0, at)
            message = EmailMessage(
                at=at,
                sender=sender,
                from_role=from_role,
                subject=self.clean_text(row["subject"]),
                snippet=text,
            )
            conversation_id = row["conversation_id"]
            key = ("thread", conversation_id) if conversation_id not in (None, "", "None") else ("msg", index)
            by_thread.setdefault(key, []).append(EmailRankedMessage(rank, message))
        for messages in by_thread.values():
            messages.sort(key=lambda ranked: ranked.rank, reverse=True)
        leaders = sorted((messages[0] for messages in by_thread.values()), key=lambda ranked: ranked.rank, reverse=True)
        rest = sorted(
            (message for messages in by_thread.values() for message in messages[1:]),
            key=lambda ranked: ranked.rank,
            reverse=True,
        )
        kept: list[EmailMessage] = []
        kept_shingles: list[frozenset[str]] = []
        for ranked in leaders + rest:
            if len(kept) >= per_person:
                break
            message = ranked.message
            shingles = self.shingles(message.snippet)
            if any(self.jaccard(shingles, prior) >= self.NEARDUP_THRESHOLD for prior in kept_shingles):
                continue
            kept.append(message)
            kept_shingles.append(shingles)
        return kept, dropped

    def recent_emails_for(
        self,
        email: str,
        per_person: int,
        accounts: set[str],
    ) -> tuple[list[EmailMessage], int]:
        """Fetch one contact through the shared store, then apply the selector."""
        rows = self.store.fetch_recent_rows(
            email,
            per_person * self.CANDIDATE_ROWS_PER_OUTPUT,
        )
        return self.select_emails_from_rows(
            rows,
            email,
            per_person,
            accounts,
        )
