"""Typed records for everything wacli hands back, parsed ONCE at the boundary.

wacli is an external Go binary: its `--json` output is untrusted shape, not a
contract we control, so every `isinstance` / `or ""` / int-coercion guard for it
lives here and nowhere else. A caller receives a frozen dataclass with settled
types and stops re-guessing whether `data` is a dict or whether `requests_sent`
came back as a string.

The parsers, in the order the flow meets them:

- `AuthStatus` — `wacli auth status --json` (`auth.py`).
- `PairingMarker` — our own `.powerpacks-pairing.json` full-sync stamp, which is
  a plain local file and can be hand-edited (`pairing.py`).
- `GroupInfo` / `GroupParticipant` — `wacli groups info --jid ... --json`, whose
  participant rows carry either a phone number or only a JID (`sync.py`).
- `BackfillBatchResult` / `BackfillChatResult` — `wacli history backfill-batch
  --json`, which reports per-chat outcomes and may omit a requested chat
  entirely (`backfill.py`).
- `PriorDepthManifest` — the previous run's `history-depth/manifest.json`, read
  back to decide bootstrap vs incremental (`depth.py`).

`HistoryDepthTarget` and `HistoryDepthAttempt` are the stage's own records
rather than wacli parses; they live here because the store queries produce them
and the backfill/stage modules consume them (keeping them here is what stops
those three modules from importing each other in a cycle).

Changelog:
  2026-07-30 (wacli split): created with the module split. The ad-hoc dict
    parsing that used to sit inline in `auth_status`,
    `normalize_group_info_payload`, `read_pairing_marker`,
    `history_backfill_json_data` + `WacliHistoryDepthAdapter.run`, and
    `run_history_depth_stage`'s previous-manifest block became these classes.
    Parsed values are unchanged, including the deliberate `Any` on
    `PairingMarker.wacli_version` / `paired_at` (the status payload echoes them
    verbatim, so coercing a missing key to `""` would change the emitted JSON).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.messages.wacli.util import (  # noqa: E402
    canonicalize_phone,
    clean_name,
    jid_to_phone,
    result_int,
)


@dataclass(frozen=True)
class AuthStatus:
    """`wacli auth status --json`: is this store linked to an account?"""

    authenticated: bool
    raw_success: Any
    error: Any
    linked_jid: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AuthStatus:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return cls(
            authenticated=bool(data.get("authenticated")),
            raw_success=payload.get("success"),
            error=payload.get("error"),
            linked_jid=str(data.get("linked_jid") or ""),
        )


@dataclass(frozen=True)
class PairingMarker:
    """Our `.powerpacks-pairing.json` stamp: did OUR flow pair this session?"""

    full_sync: bool
    wacli_version: Any
    paired_at: Any

    @classmethod
    def from_payload(cls, payload: Any) -> PairingMarker | None:
        if not isinstance(payload, dict):
            return None
        return cls(
            full_sync=bool(payload.get("full_sync")),
            wacli_version=payload.get("wacli_version"),
            paired_at=payload.get("paired_at"),
        )


@dataclass(frozen=True)
class GroupParticipant:
    """One usable member of a group: a canonical phone plus wacli's display name."""

    phone: str
    name: str

    def as_cache_entry(self) -> dict[str, str]:
        return {"phone": self.phone, "name": self.name}


@dataclass(frozen=True)
class GroupInfo:
    """`wacli groups info --jid <jid> --json` for one group chat.

    Participants without any resolvable phone (LID-only rows) are dropped at the
    parse, so the cache never carries a member we cannot key on.
    """

    jid: str
    name: str
    participant_count: int
    participants: tuple[GroupParticipant, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> GroupInfo | None:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            return None
        jid = str(data.get("JID") or "")
        if not jid:
            return None
        participants: list[GroupParticipant] = []
        for raw in data.get("Participants") or []:
            if not isinstance(raw, dict):
                continue
            phone = canonicalize_phone(str(raw.get("PhoneNumber") or ""))
            if not phone:
                phone = jid_to_phone(str(raw.get("JID") or "")) or ""
            if not phone:
                continue
            participants.append(GroupParticipant(phone=phone, name=clean_name(raw.get("DisplayName"))))
        return cls(
            jid=jid,
            name=clean_name(data.get("Name")),
            participant_count=int(data.get("ParticipantCount") or len(participants)),
            participants=tuple(participants),
        )

    def as_cache_entry(self) -> dict[str, Any]:
        return {
            "jid": self.jid,
            "name": self.name,
            "participant_count": self.participant_count,
            "participants": [participant.as_cache_entry() for participant in self.participants],
        }


@dataclass(frozen=True)
class BackfillChatResult:
    """One chat's slice of a `history backfill-batch` run.

    The all-default instance is the MISSING result: wacli returned no row for a
    chat we requested, which the stage treats as retryable rather than as proof
    the server has no older history.
    """

    chat: str = ""
    requests_sent: int = 0
    responses_seen: int = 0
    messages_received: int = 0
    error: str = ""
    end_type: str = ""
    present: bool = False

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> BackfillChatResult:
        return cls(
            chat=str(item.get("chat") or ""),
            requests_sent=result_int(item, "requests_sent"),
            responses_seen=result_int(item, "responses_seen"),
            messages_received=result_int(item, "messages_received"),
            error=str(item.get("error") or ""),
            end_type=str(item.get("end_type") or ""),
            present=True,
        )


MISSING_BACKFILL_CHAT = BackfillChatResult()


@dataclass(frozen=True)
class BackfillBatchResult:
    """`wacli --json history backfill-batch`, keyed by chat JID.

    wacli nests its result under `data` but has also returned it flat; both
    shapes land here, and anything unparseable degrades to "no chats reported".
    """

    chats: dict[str, BackfillChatResult] = field(default_factory=dict)

    @classmethod
    def from_command_json(cls, payload: Any) -> BackfillBatchResult:
        data = payload if isinstance(payload, dict) else {}
        nested = data.get("data")
        data = nested if isinstance(nested, dict) else data
        raw_chats = data.get("chats")
        parsed: dict[str, BackfillChatResult] = {}
        for item in raw_chats if isinstance(raw_chats, list) else []:
            if not isinstance(item, dict) or not item.get("chat"):
                continue
            chat = BackfillChatResult.from_item(item)
            parsed[chat.chat] = chat
        return cls(chats=parsed)

    def chat(self, chat_jid: str) -> BackfillChatResult:
        return self.chats.get(chat_jid, MISSING_BACKFILL_CHAT)


@dataclass(frozen=True)
class PriorDepthManifest:
    """The previous `history-depth/manifest.json`, read back for resume decisions.

    A key that is absent is not the same as a key that is zero: an older
    manifest predating the source watermark has no `source_total_messages` at
    all, which forces a bootstrap. `source_total_messages is None` carries that
    distinction.
    """

    source_total_messages: int | None
    dm_state_sha256: str
    policy_version: int

    @classmethod
    def from_payload(cls, payload: Any) -> PriorDepthManifest:
        manifest = payload if isinstance(payload, dict) else {}
        counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
        source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
        policy = manifest.get("policy") if isinstance(manifest.get("policy"), dict) else {}
        return cls(
            source_total_messages=(
                result_int(counts, "source_total_messages")
                if "source_total_messages" in counts
                else None
            ),
            dm_state_sha256=str(source.get("dm_state_sha256") or ""),
            policy_version=result_int(policy, "version"),
        )

    @property
    def has_source_total(self) -> bool:
        return self.source_total_messages is not None

    @property
    def has_source_digest(self) -> bool:
        return len(self.dm_state_sha256) == 64


@dataclass(frozen=True)
class HistoryDepthTarget:
    chat_jid: str
    chat_ref: str
    kind: str
    current_count: int
    current_latest_ts: int = 0
    state_changed: bool = False


@dataclass(frozen=True)
class HistoryDepthAttempt:
    returncode: int
    requests_sent: int
    responses_seen: int
    target_added: int
    unrelated_added: int
    after_count: int
    error_category: str
    retryable: bool
    after_latest_ts: int = 0
    messages_received: int = 0
    end_type: str = ""
