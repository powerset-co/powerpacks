#!/usr/bin/env python3
"""Extract WhatsApp contact metadata from a running WAHA session.

This primitive expects:

- a WAHA container reachable at `--base-url` (default http://127.0.0.1:3000)
- an authenticated session whose status is `WORKING`

Use `waha_runtime` to start the container and `waha_session` to authenticate.

Privacy contract:

- never read or store message content
- only collect: phone, name, source, group flags/names, message counts, and
  the most recent message timestamp
- write a manifest with diagnostics + counts so the harness can repair runs

Stdlib-only. Output CSV matches the shape consumed by
`normalize_message_contacts` and the canonical `message-contact.schema.json`.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4


CSV_HEADERS = [
    "phone",
    "name",
    "source",
    "is_in_group_chats",
    "group_names",
    "message_count",
    "imessage_message_count",
    "whatsapp_message_count",
    "last_message",
    "imessage_last_message",
    "whatsapp_last_message",
    "skip",
    "match_status",
    "matched_person_id",
    "matched_name",
    "matched_linkedin_url",
    "match_confidence",
    "match_method",
    "match_reason",
]

DEFAULT_BASE_URL = os.environ.get("POWERPACKS_WAHA_BASE_URL", "http://127.0.0.1:3000")
DEFAULT_API_KEY = os.environ.get("POWERPACKS_WAHA_API_KEY", "powerpacks-local")
DEFAULT_SESSION = os.environ.get("POWERPACKS_WAHA_SESSION", "default")
DEFAULT_OUT_DIR = Path(".powerpacks/messages")
DEFAULT_MESSAGE_COUNT_CACHE = DEFAULT_OUT_DIR / "whatsapp.message-count-cache.json"
GROUP_SEPARATOR = " | "
MIN_PHONE_DIGITS = 7
MAX_PHONE_DIGITS = 15
MESSAGE_PAGE_SIZE = 500
MIN_REQUEST_INTERVAL = float(os.environ.get("POWERPACKS_WHATSAPP_MIN_REQUEST_INTERVAL", "1.0"))
DEFAULT_HEARTBEAT_INTERVAL = int(os.environ.get("POWERPACKS_WHATSAPP_HEARTBEAT_INTERVAL", "30"))
GROUP_PARTICIPANTS_TIMEOUT = int(os.environ.get("POWERPACKS_WHATSAPP_GROUP_PARTICIPANTS_TIMEOUT", "120"))


@dataclass
class Contact:
    phone: str
    name: str = ""
    source: str = "whatsapp"
    is_in_group_chats: bool = False
    group_names: set[str] = field(default_factory=set)
    message_count: int | None = None
    last_message: str | None = None


# ---------------------------------------------------------------------------
# JSON / IO helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def emit_progress(value: dict[str, Any], progress_path: Path | None = None) -> None:
    payload = {"primitive": "extract_whatsapp_contacts", "event": "progress", "timestamp": now_iso(), **value}
    line = json.dumps(payload, sort_keys=True)
    print(line, file=sys.stderr, flush=True)
    if progress_path:
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        with progress_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


# ---------------------------------------------------------------------------
# WAHA HTTP
# ---------------------------------------------------------------------------

def _waha_get(
    base_url: str,
    api_key: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = 60,
    retries: int = 3,
    backoff: float = 2.0,
) -> tuple[int, Any]:
    """GET WAHA endpoint with retries; return (status, parsed_json_or_text)."""
    query = ""
    if params:
        cleaned = {k: ("true" if v is True else "false" if v is False else v) for k, v in params.items() if v is not None}
        query = "?" + urllib.parse.urlencode(cleaned)
    url = base_url.rstrip("/") + path + query
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            time.sleep(MIN_REQUEST_INTERVAL)
            req = urllib.request.Request(
                url,
                method="GET",
                headers={"X-Api-Key": api_key, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                try:
                    return resp.status, json.loads(body.decode("utf-8")) if body else None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return resp.status, body
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                payload = None
                try:
                    payload = json.loads(exc.read().decode("utf-8"))
                except Exception:
                    pass
                return exc.code, payload
            last_error = exc
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last_error = exc
        time.sleep(backoff ** attempt)
    if last_error:
        raise last_error
    return 599, None


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def canonicalize_phone(raw: str) -> str:
    value = (raw or "").strip()
    digits = re.sub(r"[^\d]", "", value)
    if len(digits) < MIN_PHONE_DIGITS:
        return ""
    if value.startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) <= MAX_PHONE_DIGITS:
        return f"+{digits}"
    return digits


def extract_jid(raw_id: Any) -> str:
    if isinstance(raw_id, dict):
        serialized = raw_id.get("_serialized", "")
        if serialized:
            return str(serialized)
        user = raw_id.get("user", "")
        server = raw_id.get("server", "")
        if user and server:
            return f"{user}@{server}"
        return str(user)
    return str(raw_id) if raw_id is not None else ""


def jid_to_phone(jid: str) -> str | None:
    if not jid or "@g.us" in jid or "@lid" in jid:
        return None
    match = re.match(r"(\d+)@", jid)
    if not match:
        return None
    digits = match.group(1)
    if not (MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS):
        return None
    return f"+{digits}"


def chat_message_count_hint(chat: dict) -> int | None:
    for key in ("messagesCount", "messages_count", "messageCount", "totalMessages", "total_messages"):
        value = chat.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def group_chat_name(chat: dict, chat_id: str) -> str:
    metadata = chat.get("groupMetadata") if isinstance(chat.get("groupMetadata"), dict) else {}
    candidates = (
        chat.get("name"),
        chat.get("subject"),
        chat.get("formattedTitle"),
        metadata.get("subject"),
        metadata.get("name"),
        metadata.get("formattedTitle"),
    )
    for candidate in candidates:
        cleaned = re.sub(r"\s+", " ", str(candidate or "").strip())
        if cleaned and cleaned != chat_id:
            return cleaned
    return ""


def parse_timestamp(ts: Any) -> str | None:
    if not ts:
        return None
    try:
        if isinstance(ts, (int, float)):
            value = float(ts)
            if value > 1e12:
                value /= 1000
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        return str(ts)
    except (ValueError, TypeError, OSError):
        return None


def chat_last_message(chat: dict[str, Any]) -> str | None:
    candidates = (
        chat.get("timestamp"),
        chat.get("last_message_timestamp"),
        chat.get("conversationTimestamp"),
        chat.get("lastMessageRecvTimestamp"),
    )
    for candidate in candidates:
        parsed = parse_timestamp(candidate)
        if parsed:
            return parsed
    last_message = chat.get("lastMessage")
    if isinstance(last_message, dict):
        for key in ("timestamp", "messageTimestamp", "t"):
            parsed = parse_timestamp(last_message.get(key))
            if parsed:
                return parsed
    return None


def load_message_count_cache(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"version": 1, "chats": {}}
    data = read_json(path, {})
    if not isinstance(data, dict):
        return {"version": 1, "chats": {}}
    chats = data.get("chats")
    if not isinstance(chats, dict):
        data["chats"] = {}
    data["version"] = 1
    return data


def cache_count_for_chat(cache: dict[str, Any], jid: str, last_message: str | None) -> int | None:
    if not last_message:
        return None
    chats = cache.get("chats")
    if not isinstance(chats, dict):
        return None
    entry = chats.get(jid)
    if not isinstance(entry, dict) or entry.get("last_message") != last_message:
        return None
    value = entry.get("message_count")
    if isinstance(value, int) and value >= 0:
        return value
    return None


def update_count_cache(cache: dict[str, Any], jid: str, count: int, last_message: str | None, source: str) -> None:
    if not last_message:
        return
    chats = cache.setdefault("chats", {})
    if not isinstance(chats, dict):
        cache["chats"] = {}
        chats = cache["chats"]
    chats[jid] = {
        "message_count": max(0, int(count or 0)),
        "last_message": last_message,
        "source": source,
        "updated_at": now_iso(),
    }


def serialize_groups(groups: Iterable[str]) -> str:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in groups or []:
        value = re.sub(r"\s+", " ", str(raw or "").strip())
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    cleaned.sort(key=str.casefold)
    return GROUP_SEPARATOR.join(cleaned)


# ---------------------------------------------------------------------------
# WAHA-driven extraction
# ---------------------------------------------------------------------------

def _ensure_session_working(base_url: str, api_key: str, session: str) -> dict[str, Any]:
    state: dict[str, Any] = {"reachable": False}
    try:
        status, payload = _waha_get(base_url, api_key, f"/api/sessions/{session}", retries=2)
    except Exception as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
        state["working"] = False
        return state
    state["reachable"] = True
    state["http_status"] = status
    if isinstance(payload, dict):
        state["status"] = payload.get("status")
        state["engine"] = (payload.get("engine") or {}).get("engine") if isinstance(payload.get("engine"), dict) else None
        state["me"] = payload.get("me")
    state["working"] = state.get("status") == "WORKING"
    return state


def _get_chat_message_count(base_url: str, api_key: str, session: str, chat_id: str) -> int:
    """Best-effort capped message count for a 1:1 chat.

    We only need a relationship-strength signal. If chat metadata lacks a
    message-count hint, fetch one page and treat 500 as "500+" rather than
    paginating through the full history.
    """
    encoded = urllib.parse.quote(chat_id, safe="")
    try:
        status, payload = _waha_get(
            base_url, api_key,
            f"/api/{session}/chats/{encoded}/messages",
            params={"limit": MESSAGE_PAGE_SIZE, "offset": 0, "downloadMedia": False},
            retries=2,
        )
    except Exception:
        return 0
    if status != 200 or not isinstance(payload, list):
        return 0
    return len(payload)


def _fetch_group_participants_endpoint(
    base_url: str,
    api_key: str,
    session: str,
    chat_id: str,
    endpoint: str,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    encoded = urllib.parse.quote(chat_id, safe="")
    try:
        status, payload = _waha_get(
            base_url, api_key,
            f"/api/{session}/groups/{encoded}/{endpoint}",
            timeout=GROUP_PARTICIPANTS_TIMEOUT,
            retries=2,
        )
    except Exception as exc:
        return None, {
            "step": "group_participants",
            "endpoint": endpoint,
            "chat_id": chat_id,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if status == 200 and isinstance(payload, list):
        return payload, None
    return None, {
        "step": "group_participants",
        "endpoint": endpoint,
        "chat_id": chat_id,
        "http_status": status,
        "payload": payload if isinstance(payload, (str, int, float, bool, type(None))) else str(payload)[:500],
    }


def _fetch_group_participants(
    base_url: str,
    api_key: str,
    session: str,
    chat_id: str,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    # Prefer WAHA's newer participants/v2 endpoint for WEBJS/Chrome stability;
    # fall back to the legacy endpoint for older images/engines.
    first_error: dict[str, Any] | None = None
    for endpoint in ("participants/v2", "participants"):
        participants, error = _fetch_group_participants_endpoint(base_url, api_key, session, chat_id, endpoint)
        if participants is not None:
            return participants, None
        if first_error is None:
            first_error = error
    return None, first_error


def extract_contacts(
    base_url: str,
    api_key: str,
    session: str,
    *,
    fetch_message_counts: bool = True,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
    message_count_cache: dict[str, Any] | None = None,
) -> tuple[dict[str, Contact], dict[str, Any]]:
    """Pull contacts and chat metadata from the WAHA API."""

    started = time.time()
    count_cache = message_count_cache if message_count_cache is not None else {"version": 1, "chats": {}}
    diagnostics: dict[str, Any] = {
        "raw_contacts": 0,
        "raw_chats": 0,
        "direct_chats": 0,
        "group_chats": 0,
        "group_participants_fetched": 0,
        "group_participants_fallback": 0,
        "group_participants_empty": 0,
        "group_participants_failed": 0,
        "lid_resolved": 0,
        "group_member_phones": 0,
        "fetched_message_counts_for": 0,
        "message_count_cap": MESSAGE_PAGE_SIZE,
        "message_count_mode": "single_page_serial",
        "request_interval_seconds": MIN_REQUEST_INTERVAL,
        "message_count_completed": 0,
        "message_count_total": 0,
        "message_count_cached": 0,
        "message_count_cache_entries_before": len(count_cache.get("chats") or {}) if isinstance(count_cache.get("chats"), dict) else 0,
        "errors": [],
    }

    def progress(stage: str, **extra: Any) -> None:
        if not progress_callback:
            return
        progress_callback({
            "stage": stage,
            "elapsed_seconds": round(time.time() - started, 1),
            **extra,
        })

    # 1. Pull all chats.
    status, chats_payload = _waha_get(base_url, api_key, f"/api/{session}/chats", retries=3)
    if status != 200 or not isinstance(chats_payload, list):
        diagnostics["errors"].append({"step": "chats", "http_status": status, "payload": chats_payload})
        chats_payload = []
    diagnostics["raw_chats"] = len(chats_payload)

    # 2. Pull contacts for name resolution.
    status, contacts_payload = _waha_get(
        base_url, api_key, "/api/contacts/all",
        params={"session": session},
        retries=3,
    )
    if status != 200 or not isinstance(contacts_payload, list):
        diagnostics["errors"].append({"step": "contacts", "http_status": status, "payload": contacts_payload})
        contacts_payload = []
    diagnostics["raw_contacts"] = len(contacts_payload)

    # 3. Index by JID and name; build @lid → phones map.
    jid_to_name: dict[str, str] = {}
    phone_to_name: dict[str, str] = {}
    name_to_phones: dict[str, list[str]] = {}
    lid_jids_by_name: dict[str, list[str]] = {}
    all_contact_phones: set[str] = set()

    for raw in contacts_payload:
        if not isinstance(raw, dict):
            continue
        raw_jid = extract_jid(raw.get("id", ""))
        name = (raw.get("name") or raw.get("pushname") or raw.get("shortName") or "").strip()
        if raw_jid and name:
            jid_to_name[raw_jid] = name
        phone = jid_to_phone(extract_jid(raw.get("phoneNumber", ""))) or jid_to_phone(raw_jid)
        if phone:
            all_contact_phones.add(phone)
            if name:
                existing = phone_to_name.get(phone, "")
                if not existing or len(name) > len(existing):
                    phone_to_name[phone] = name
                name_to_phones.setdefault(name, []).append(phone)
        elif "@lid" in raw_jid and name:
            lid_jids_by_name.setdefault(name, []).append(raw_jid)

    lid_to_phones: dict[str, list[str]] = {}
    for name, lid_jids in lid_jids_by_name.items():
        phones = name_to_phones.get(name, [])
        if phones:
            for lid_jid in lid_jids:
                lid_to_phones[lid_jid] = phones
    diagnostics["lid_resolved"] = len(lid_to_phones)

    # 4. Walk chats: split into direct + group, build group membership data.
    direct_chats: dict[str, dict[str, Any]] = {}
    group_member_phones: set[str] = set()
    group_names_by_phone: dict[str, set[str]] = {}
    group_member_phone_to_name: dict[str, str] = {}

    for chat in chats_payload:
        if not isinstance(chat, dict):
            continue
        chat_id = extract_jid(chat.get("id", ""))
        if not chat_id:
            continue
        if "@g.us" in chat_id:
            diagnostics["group_chats"] += 1
            display_name = group_chat_name(chat, chat_id)
            fallback_participants = chat.get("participants") or (chat.get("groupMetadata") or {}).get("participants") or []
            fetched_participants, participant_error = _fetch_group_participants(base_url, api_key, session, chat_id)
            if fetched_participants:
                participants = fetched_participants
                diagnostics["group_participants_fetched"] += 1
            else:
                if fetched_participants == []:
                    diagnostics["group_participants_empty"] += 1
                if participant_error:
                    participant_error["group_name"] = display_name
                    diagnostics["errors"].append(participant_error)
                    diagnostics["group_participants_failed"] += 1
                participants = fallback_participants
                if fallback_participants:
                    diagnostics["group_participants_fallback"] += 1
            for p in participants:
                if not isinstance(p, dict):
                    continue
                p_jid = extract_jid(p.get("id", ""))
                p_phone_jid = extract_jid(p.get("phoneNumber", "")) or extract_jid(p.get("pn", ""))
                phone = jid_to_phone(p_phone_jid) or jid_to_phone(p_jid)
                if phone:
                    group_member_phones.add(phone)
                    if display_name:
                        group_names_by_phone.setdefault(phone, set()).add(display_name)
                    name = (
                        str(p.get("name") or p.get("pushname") or p.get("pushName") or p.get("shortName") or "").strip()
                        or jid_to_name.get(p_jid, "")
                        or jid_to_name.get(p_phone_jid, "")
                    )
                    if name and phone not in group_member_phone_to_name:
                        group_member_phone_to_name[phone] = name
                else:
                    for ph in lid_to_phones.get(p_jid, []):
                        group_member_phones.add(ph)
                        if display_name:
                            group_names_by_phone.setdefault(ph, set()).add(display_name)
                        name = jid_to_name.get(p_jid, "")
                        if name and ph not in group_member_phone_to_name:
                            group_member_phone_to_name[ph] = name
        else:
            direct_chats[chat_id] = chat
    diagnostics["direct_chats"] = len(direct_chats)
    diagnostics["group_member_phones"] = len(group_member_phones)

    # 5. Build base contacts dict from contact list.
    contacts_by_phone: dict[str, Contact] = {
        phone: Contact(
            phone=phone,
            name=phone_to_name.get(phone, ""),
            source="whatsapp",
            is_in_group_chats=phone in group_member_phones,
            group_names=set(group_names_by_phone.get(phone, set())),
        )
        for phone in all_contact_phones
    }

    # 6. Resolve direct-chat message counts for chats without a count hint.
    direct_jids = [
        jid for jid in direct_chats
        if jid_to_phone(jid) or lid_to_phones.get(jid)
    ]
    counts_by_jid: dict[str, int] = {}
    needs_fetch: list[str] = []
    for jid in direct_jids:
        chat = direct_chats.get(jid, {})
        last_message = chat_last_message(chat)
        hinted = chat_message_count_hint(chat)
        if hinted is not None:
            counts_by_jid[jid] = hinted
            update_count_cache(count_cache, jid, hinted, last_message, "chat_hint")
        elif fetch_message_counts:
            cached_count = cache_count_for_chat(count_cache, jid, last_message)
            if cached_count is not None:
                counts_by_jid[jid] = cached_count
                diagnostics["message_count_cached"] += 1
            else:
                needs_fetch.append(jid)
        else:
            needs_fetch.append(jid)

    if fetch_message_counts and needs_fetch:
        diagnostics["fetched_message_counts_for"] = len(needs_fetch)
        diagnostics["message_count_total"] = len(needs_fetch)
        progress("message_counts_start", total=len(needs_fetch), cap=MESSAGE_PAGE_SIZE)
        last_emit = 0.0
        for completed_count, jid in enumerate(needs_fetch, start=1):
            try:
                counts_by_jid[jid] = _get_chat_message_count(base_url, api_key, session, jid)
                update_count_cache(count_cache, jid, counts_by_jid[jid], chat_last_message(direct_chats.get(jid, {})), "messages_page")
            except Exception as exc:
                diagnostics["errors"].append({"step": "message_count", "jid": jid, "error": str(exc)})
                counts_by_jid[jid] = 0
            diagnostics["message_count_completed"] = completed_count
            if time.time() - last_emit >= max(1, heartbeat_interval) or completed_count == len(needs_fetch):
                progress(
                    "message_counts_progress",
                    completed=completed_count,
                    total=len(needs_fetch),
                    remaining=len(needs_fetch) - completed_count,
                )
                last_emit = time.time()

    # 7. Collapse direct-chat stats by phone.
    direct_stats: dict[str, dict[str, Any]] = {}
    for jid in direct_jids:
        direct_phone = jid_to_phone(jid)
        phones = [direct_phone] if direct_phone else lid_to_phones.get(jid, [])
        if not phones:
            continue
        count = int(counts_by_jid.get(jid, 0))
        chat = direct_chats[jid]
        last_message = chat_last_message(chat)
        for phone in phones:
            resolved_name = (
                jid_to_name.get(jid, "")
                or phone_to_name.get(phone, "")
                or group_member_phone_to_name.get(phone, "")
            )
            current = direct_stats.get(phone)
            if not current:
                direct_stats[phone] = {"count": count, "last_message": last_message, "name": resolved_name}
                continue
            current["count"] = int(current.get("count", 0) or 0) + count
            if last_message and (not current.get("last_message") or last_message > current["last_message"]):
                current["last_message"] = last_message
            if resolved_name:
                existing = current.get("name", "") or ""
                if not existing or len(resolved_name) > len(existing):
                    current["name"] = resolved_name

    # 8. Apply direct-chat metadata onto the union baseline.
    for phone, stats in direct_stats.items():
        existing = contacts_by_phone.get(phone)
        if existing:
            if not existing.name and stats.get("name"):
                existing.name = stats["name"]
            existing.is_in_group_chats = existing.is_in_group_chats or (phone in group_member_phones)
            existing.group_names = existing.group_names | group_names_by_phone.get(phone, set())
            count = int(stats.get("count", 0) or 0)
            existing.message_count = count or None
            existing.last_message = stats.get("last_message") or existing.last_message
        else:
            contacts_by_phone[phone] = Contact(
                phone=phone,
                name=stats.get("name", "") or "",
                source="whatsapp",
                is_in_group_chats=phone in group_member_phones,
                group_names=set(group_names_by_phone.get(phone, set())),
                message_count=int(stats.get("count", 0) or 0) or None,
                last_message=stats.get("last_message"),
            )

    # 9. Fold in group-only contacts.
    for phone in group_member_phones:
        existing = contacts_by_phone.get(phone)
        group_name = phone_to_name.get(phone, "") or group_member_phone_to_name.get(phone, "")
        if existing:
            existing.is_in_group_chats = True
            existing.group_names = existing.group_names | group_names_by_phone.get(phone, set())
            if not existing.name and group_name:
                existing.name = group_name
        else:
            contacts_by_phone[phone] = Contact(
                phone=phone,
                name=group_name,
                source="whatsapp",
                is_in_group_chats=True,
                group_names=set(group_names_by_phone.get(phone, set())),
            )

    # Re-canonicalize phone keys to be safe.
    canonical: dict[str, Contact] = {}
    for phone, contact in contacts_by_phone.items():
        canonical_phone = canonicalize_phone(phone)
        if not canonical_phone:
            continue
        contact.phone = canonical_phone
        canonical[canonical_phone] = contact
    diagnostics["message_count_cache_entries_after"] = len(count_cache.get("chats") or {}) if isinstance(count_cache.get("chats"), dict) else 0
    return canonical, diagnostics


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_csv(path: Path, contacts: dict[str, Contact]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(
        contacts.values(),
        key=lambda c: ((c.message_count or 0), c.last_message or "", c.phone),
        reverse=True,
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for contact in rows:
            writer.writerow({
                "phone": contact.phone,
                "name": contact.name or "",
                "source": contact.source,
                "is_in_group_chats": "true" if contact.is_in_group_chats else "false",
                "group_names": serialize_groups(contact.group_names),
                "message_count": "" if contact.message_count is None else str(contact.message_count),
                "imessage_message_count": "",
                "whatsapp_message_count": "" if contact.message_count is None else str(contact.message_count),
                "last_message": contact.last_message or "",
                "imessage_last_message": "",
                "whatsapp_last_message": contact.last_message or "",
                "skip": "",
                "match_status": "",
                "matched_person_id": "",
                "matched_name": "",
                "matched_linkedin_url": "",
                "match_confidence": "",
                "match_method": "",
                "match_reason": "",
            })
    return len(rows)


def write_jsonl(path: Path, contacts: dict[str, Contact]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(
        contacts.values(),
        key=lambda c: ((c.message_count or 0), c.last_message or "", c.phone),
        reverse=True,
    )
    with path.open("w", encoding="utf-8") as handle:
        for contact in rows:
            handle.write(json.dumps({
                "phone": contact.phone,
                "name": contact.name or "",
                "sources": [contact.source],
                "is_in_group_chats": bool(contact.is_in_group_chats),
                "group_names": sorted(contact.group_names, key=str.casefold),
                "message_count": contact.message_count,
                "imessage_message_count": None,
                "whatsapp_message_count": contact.message_count,
                "last_message": contact.last_message,
                "imessage_last_message": None,
                "whatsapp_last_message": contact.last_message,
                "skip": False,
                "match": {
                    "status": None,
                    "person_id": None,
                    "name": None,
                    "linkedin_url": None,
                    "confidence": None,
                    "method": None,
                    "reason": None,
                },
            }, sort_keys=True) + "\n")
    return len(rows)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    state = _ensure_session_working(args.base_url, args.api_key, args.session)
    payload = {
        "primitive": "extract_whatsapp_contacts",
        "command": "check",
        "checked_at": now_iso(),
        "base_url": args.base_url,
        "session": args.session,
        "session_state": state,
        "ready": bool(state.get("working")),
    }
    emit(payload)
    return 0 if state.get("working") else 1


def cmd_extract(args: argparse.Namespace) -> int:
    run_id = args.run_id or f"whatsapp-{uuid4()}"
    output_csv = Path(args.output_csv) if args.output_csv else DEFAULT_OUT_DIR / f"{run_id}.contacts.csv"
    output_jsonl = Path(args.output_jsonl) if args.output_jsonl else None
    manifest_path = Path(args.manifest) if args.manifest else output_csv.with_suffix(output_csv.suffix + ".manifest.json")
    message_count_cache_path = Path(args.message_count_cache) if args.message_count_cache else DEFAULT_MESSAGE_COUNT_CACHE

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()

    session_state = _ensure_session_working(args.base_url, args.api_key, args.session)
    if not session_state.get("working"):
        # Write empty CSV so downstream steps still see expected artifacts.
        write_csv(output_csv, {})
        manifest = {
            "run_id": run_id,
            "primitive": "extract_whatsapp_contacts",
            "status": "failed",
            "error": "WAHA session is not WORKING",
            "session_state": session_state,
            "artifacts": {"csv": str(output_csv), "manifest": str(manifest_path)},
            "started_at": now_iso(),
        }
        write_json(manifest_path, manifest)
        emit(manifest)
        return 2

    try:
        progress_path = Path(args.progress_jsonl) if args.progress_jsonl else manifest_path.with_suffix(manifest_path.suffix + ".progress.jsonl")
        message_count_cache = load_message_count_cache(message_count_cache_path)
        contacts, diagnostics = extract_contacts(
            args.base_url,
            args.api_key,
            args.session,
            fetch_message_counts=not args.skip_message_counts,
            progress_callback=lambda payload: emit_progress(payload, progress_path),
            heartbeat_interval=args.heartbeat_interval,
            message_count_cache=message_count_cache,
        )
        message_count_cache["updated_at"] = now_iso()
        message_count_cache["session"] = args.session
        write_json(message_count_cache_path, message_count_cache)
    except Exception as exc:
        write_csv(output_csv, {})
        manifest = {
            "run_id": run_id,
            "primitive": "extract_whatsapp_contacts",
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "session_state": session_state,
            "artifacts": {"csv": str(output_csv), "manifest": str(manifest_path)},
            "started_at": now_iso(),
        }
        write_json(manifest_path, manifest)
        emit(manifest)
        return 1

    csv_rows = write_csv(output_csv, contacts)
    jsonl_rows = write_jsonl(output_jsonl, contacts) if output_jsonl else 0

    elapsed_ms = int((time.time() - started) * 1000)
    manifest = {
        "run_id": run_id,
        "primitive": "extract_whatsapp_contacts",
        "status": "completed",
        "started_at": now_iso(),
        "elapsed_ms": elapsed_ms,
        "base_url": args.base_url,
        "session": args.session,
        "session_state": session_state,
        "artifacts": {
            "csv": str(output_csv),
            "jsonl": str(output_jsonl) if output_jsonl else None,
            "manifest": str(manifest_path),
            "progress_jsonl": str(progress_path),
            "message_count_cache": str(message_count_cache_path),
        },
        "counts": {
            "csv_rows": csv_rows,
            "jsonl_rows": jsonl_rows,
            "contacts": len(contacts),
            "with_message_count": sum(1 for c in contacts.values() if c.message_count),
            "in_group_chats": sum(1 for c in contacts.values() if c.is_in_group_chats),
        },
        "diagnostics": diagnostics,
    }
    write_json(manifest_path, manifest)
    emit(manifest)
    return 0


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--session", default=DEFAULT_SESSION)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract WhatsApp contacts from a running WAHA session")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Verify the WAHA session is authenticated")
    add_common_args(check)
    check.set_defaults(func=cmd_check)

    extract = sub.add_parser("extract", help="Pull contacts from WAHA into CSV/JSONL")
    add_common_args(extract)
    extract.add_argument("--output-csv", help="CSV output path", default=None)
    extract.add_argument("--output-jsonl", help="Optional JSONL output path", default=None)
    extract.add_argument("--manifest", help="Manifest output path")
    extract.add_argument("--run-id", help="Stable id used in default file names")
    extract.add_argument("--heartbeat-interval", type=int, default=DEFAULT_HEARTBEAT_INTERVAL,
                         help="Seconds between progress heartbeats while capped message-count sync is running")
    extract.add_argument("--progress-jsonl", help="Progress/heartbeat JSONL path; defaults next to the manifest")
    extract.add_argument("--message-count-cache", default=str(DEFAULT_MESSAGE_COUNT_CACHE), help=argparse.SUPPRESS)
    extract.add_argument("--skip-message-counts", action="store_true",
                         help="Skip per-chat message-count fetches (faster, less complete; not recommended for normal imports)")
    extract.set_defaults(func=cmd_extract)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
