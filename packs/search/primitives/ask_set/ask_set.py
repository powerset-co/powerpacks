"""Ask the set about a search's pinned candidates, as agent messages over the relay.

Who to ask is worked out here: the members of this machine's sets (share/web/sets.py) who shared each
pinned candidate into the share_v1 network (the candidate's summaries document lists them in
allowed_operator_ids). Each of them gets one `ask` message holding the question and the candidates they
know; their laptop answers with one `ask_answer` message (ask_worker.answer_message), which lands in
this machine's `.powerpacks/inbox`. The run keeps what went out in `ask.json`.

The relay only carries the messages; it holds them for a member who is offline.

Changelog:
- 2026-10-08: created; replaces the relay's ask tasks (ask_status and upload `--ask`).
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from packs.indexing.lib.identity import stable_person_id
from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.share.web.sets import Member, Sets
from packs.ingestion.schemas.people_schema import extract_public_identifier
from packs.powerset.primitives.agent_inbox.messages import Ask
from packs.search.primitives.deep_search.results_web import snapshot

ASK = "ask"
ANSWER = "ask_answer"
AWAKE_SECONDS = 90  # three missed 30 s heartbeats


def pinned_candidates(rendered: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """The snapshot's pinned candidates, and how many pins had no LinkedIn URL."""
    pinned = {person for person, labels in rendered["tags"]["assignments"].items()
              if any(label.casefold() == "pinned" for label in labels)}
    candidates = []
    skipped = 0
    for rank, row in enumerate(rendered["search"]["candidates"], start=1):
        if row["person_id"] not in pinned:
            continue
        public_identifier = extract_public_identifier(row["linkedin_url"])
        if not public_identifier:
            skipped += 1
            continue
        candidates.append({"public_identifier": public_identifier, "linkedin_url": row["linkedin_url"],
                           "name": row["name"], "local_rank": rank})
    return candidates, skipped


def _owners(sets: Sets, candidates: list[dict[str, Any]]) -> dict[str, list[Member]]:
    """Per candidate slug, the set members (not me) who shared that person."""
    me = sets.me()
    members: dict[str, Member] = {}
    for view in sets.kept():
        for member in sets.members(view):
            if member.operator_id != me.operator_id:
                members.setdefault(member.operator_id, member)
    ids = {stable_person_id(public_identifier=candidate["public_identifier"]): candidate["public_identifier"]
           for candidate in candidates}
    shared_by = sets.shared_by(list(ids)) if members and ids else {}
    return {slug: [members[op] for op in shared_by.get(person_id, ()) if op in members]
            for person_id, slug in ids.items()}


def preview(run_dir: Path, sets: Sets) -> dict[str, Any]:
    """The pinned candidates and who would be asked, without sending."""
    candidates, skipped = pinned_candidates(snapshot.export_snapshot(run_dir))
    owners = _owners(sets, candidates)
    operators: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        for member in owners[candidate["public_identifier"]]:
            operators.setdefault(member.operator_id, {"operator_id": member.operator_id, "name": member.name,
                                                      "candidates": 0})["candidates"] += 1
    return {"pinned": candidates, "skipped": skipped,
            "candidates": [{"public_identifier": candidate["public_identifier"], "name": candidate["name"],
                            "owners": [{"operator_id": m.operator_id, "name": m.name}
                                       for m in owners[candidate["public_identifier"]]]}
                           for candidate in candidates],
            "operators": list(operators.values())}


def send(run_dir: Path, question: str, sets: Sets) -> dict[str, Any]:
    """One `ask` message per member who knows a pinned candidate; the run keeps ask.json."""
    candidates, _ = pinned_candidates(snapshot.export_snapshot(run_dir))
    owners = _owners(sets, candidates)
    ask_id = str(uuid.uuid4())
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        for member in owners[candidate["public_identifier"]]:
            by_owner.setdefault(member.operator_id, []).append(
                {"public_identifier": candidate["public_identifier"], "linkedin_url": candidate["linkedin_url"],
                 "name": candidate["name"]})
    payloads = {operator_id: {"ask_id": ask_id, "question": question, "candidates": theirs}
                for operator_id, theirs in by_owner.items()}
    for payload in payloads.values():
        Ask.parse(payload)  # the recipient's limits (question length, candidates per ask): refuse before sending
    for operator_id, payload in payloads.items():
        sets.message(operator_id, ASK, payload)
    ask = {"ask_id": ask_id, "question": question, "sent_at": now_iso(),
           "candidates": [{**candidate, "owners": [{"operator_id": m.operator_id, "name": m.name}
                                                   for m in owners[candidate["public_identifier"]]]}
                          for candidate in candidates]}
    write_json(run_dir / "ask.json", ask)
    return {"status": "sent", "ask": ask}


def status(run_dir: Path, sets: Sets) -> dict[str, Any]:
    """What went out and the answers that came back, from ask.json and this machine's inbox."""
    path = run_dir / "ask.json"
    if not path.is_file():
        return {"ask": None}
    ask = json.loads(path.read_text(encoding="utf-8"))
    answers: dict[tuple[str, str], dict[str, Any]] = {}
    for message_path in (sets.data_root / "inbox").glob("*.json"):
        message = json.loads(message_path.read_text(encoding="utf-8"))
        if message["kind"] == ANSWER and message["payload"]["ask_id"] == ask["ask_id"]:
            for item in message["payload"]["answers"]:
                answers[(item["public_identifier"], message["from"]["operator_id"])] = item["answer"]
    seen = sets.presence()
    candidates = []
    pending = 0
    for candidate in ask["candidates"]:
        owners = []
        for owner in candidate["owners"]:
            answer = answers.get((candidate["public_identifier"], owner["operator_id"]))
            pending += answer is None
            owners.append({**owner, "awake": _awake(seen.get(owner["operator_id"], "")),
                           "status": "pending" if answer is None else "declined" if answer.get("declined") else "answered",
                           "answer": None if answer is None or answer.get("declined") else answer})
        candidates.append({"public_identifier": candidate["public_identifier"], "name": candidate["name"],
                           "owners": owners})
    return {"ask": ask, "answers": {"pending": pending, "candidates": candidates}}


def _awake(last_seen: str) -> bool:
    return bool(last_seen) and time.time() - datetime.fromisoformat(last_seen).timestamp() < AWAKE_SECONDS
