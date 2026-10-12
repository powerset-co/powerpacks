"""Prepare local Ask the Set evidence and deliver validated Codex answers; never run inference."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.deep_context_v2.db import queries_dedupe, queries_enrich, queries_worth
from packs.ingestion.primitives.deep_context_v2.db.schema import LINKEDIN_PARENT_PREFIX, Verdict
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import CACHE_RELATIVE_DIR, profile_from_record
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts, collapse
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path, read_usable_cached_profile
from packs.ingestion.schemas.people_schema import extract_public_identifier
from packs.ingestion.primitives.share.web.sets import Sets
from packs.powerset.primitives.agent_inbox.collaboration_messages import Failure
from packs.powerset.primitives.agent_inbox.messages import MAX_TEXT, parse

_EMAIL = re.compile(r"[^\s@\"<>]+@[^\s@\"<>]+\.[^\s@\"<>]+")
ANSWER = "ask_answer"


class _Answer(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)

    verdict: Literal["recommend", "not_fit", "unsure"]
    reason: str
    can_intro: bool
    relationship: str
    last_contact: Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")] | None
    confidence: float = Field(ge=0, le=1)


def _evidence(conn: sqlite3.Connection, data_root: Path, slug: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    parents = queries_dedupe.current_parents(conn)
    parent = None
    url = ""
    for linkedin in queries_enrich.current_linkedins(conn):
        if (linkedin.verdict == Verdict.CONFIRMED
                and extract_public_identifier(linkedin.linkedin_url) == slug.lower()
                and parents.get(linkedin.candidate_id) == LINKEDIN_PARENT_PREFIX + linkedin.member_id):
            parent = parents[linkedin.candidate_id]
            url = linkedin.linkedin_url
            break
    if parent is None:
        return None

    candidate_ids = sorted(candidate for candidate, current in parents.items() if current == parent)
    rows = queries_dedupe.facts_json(conn, candidate_ids)
    facts = {}
    if rows:
        facts = collapse([SynthesizedFacts.from_payload(json.loads(row)) for row in rows.values()]).to_payload()
        # These fields carry addresses and verbatim message evidence, not judgments about a person.
        del facts["identifiers"], facts["owned_identifiers"]
        facts["shared_context"] = [{"overlap": item["overlap"], "detail": item["detail"]}
                                   for item in facts["shared_context"]]

    counts = queries_worth.bundle_counts(conn)
    family_counts = [count for count in counts.channels if count.candidate_id in candidate_ids]
    interactions: dict[str, int] = {}
    for count in family_counts:
        interactions[count.channel] = interactions.get(count.channel, 0) + count.messages
    first = min((count.first_at for count in family_counts if count.first_at), default=None)
    last = max((count.last_at for count in family_counts if count.last_at), default=None)
    groups = sum(counts.groups.get(candidate, 0) for candidate in candidate_ids)
    channels = {
        "source_channels": sorted(interactions), "interaction_counts": interactions,
        "first_message_at": first, "last_message_at": last, "last_interaction": last,
        "from_me": sum(count.messages for count in family_counts if count.direction == "from_me"),
        "from_them": sum(count.messages for count in family_counts if count.direction == "from_them"),
        "group_count": groups,
    }
    record = read_usable_cached_profile(profile_cache_path(data_root / CACHE_RELATIVE_DIR, slug))
    profile = profile_from_record(url, record).judge_view() if record else {}
    used = [{"candidate_ids": candidate_ids, "fact_keys": sorted(facts),
             "channels": sorted(interactions), "counts": interactions, "group_count": groups}]
    return {"facts": facts, "profile": profile, "channels": channels}, used


class _CandidateAnswer(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    public_identifier: str
    answer: _Answer


class _BatchAnswer(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    answers: list[_CandidateAnswer]


class AskWorker:
    def __init__(self, sets: Sets) -> None:
        self.sets = sets
        self.data_root = sets.data_root

    def _path(self, question_id: str) -> Path:
        return self.data_root / "inbox" / f"{UUID(question_id)}.json"

    def get(self, question_id: str) -> dict[str, Any] | None:
        path = self._path(question_id)
        if not path.is_file():
            return None
        message: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if message["kind"] != "ask":
            return None
        parse(message)
        if message["from"]["operator_id"] not in self.sets.known_operators():
            raise PermissionError("sender is not in a set with this operator")
        return message

    def _prepare(self, message: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        candidates: list[dict[str, Any]] = []
        answers: list[dict[str, Any]] = []
        used: dict[str, list[dict[str, Any]]] = {}
        with closing(open_store(store_path(self.data_root))) as conn:
            for candidate in message["payload"]["candidates"]:
                slug = candidate["public_identifier"]
                audit = self.data_root / "asks" / f"{message['id']}-{slug}.json"
                if audit.is_file():
                    answers.append({"public_identifier": slug, "answer": json.loads(audit.read_text(encoding="utf-8"))["answer"]})
                    continue
                found = _evidence(conn, self.data_root, slug)
                if found is None:
                    answers.append({"public_identifier": slug, "answer": {"declined": True, "reason": "not_in_store"}})
                else:
                    candidates.append({**candidate, "evidence": found[0]})
                    used[slug] = found[1]
        return candidates, answers, used

    def pending(self) -> list[dict[str, Any]]:
        questions = []
        for path in sorted((self.data_root / "inbox").glob("*.json")):
            try:
                message = self.get(path.stem)
            except PermissionError:
                continue
            if message is None or "answered_at" in message:
                continue
            candidates: list[dict[str, Any]] = []
            answers: list[dict[str, Any]] = []
            if "answer" not in message:
                candidates, answers, _ = self._prepare(message)
            question = {"id": message["id"], "from_name": message["from"]["name"],
                        "question": message["payload"]["question"],
                        "context": {"role": message["payload"]["role"], "candidates": candidates},
                        "output_schema": _BatchAnswer.model_json_schema()}
            if "answer" in message:
                question["answer"] = message["answer"]
            elif not candidates:
                question["answer"] = {"answers": answers}
            for field in ("thread_id", "error"):
                if field in message:
                    question[field] = message[field]
            questions.append(question)
        return questions

    def thread(self, question_id: str, thread_id: str) -> dict[str, Any]:
        message = self.get(question_id)
        if message is None:
            raise FileNotFoundError("ask not found")
        if ("answered_at" in message or "answer" in message or "error" in message
                or ("thread_id" in message and message["thread_id"] != thread_id)):
            raise ValueError("question already has a thread or result")
        message = {**message, "thread_id": thread_id}
        write_json(self._path(question_id), message)
        return message

    def finish(self, question_id: str, request: dict[str, Any] | Failure) -> dict[str, Any]:
        message = self.get(question_id)
        if message is None:
            raise FileNotFoundError("ask not found")
        if "answered_at" in message:
            return message
        if "answer" not in message:
            candidates, answers, used = self._prepare(message)
            if isinstance(request, Failure):
                message["error"] = request.error
                answers.extend({"public_identifier": candidate["public_identifier"],
                                "answer": {"declined": True, "reason": "failed"}} for candidate in candidates)
            elif candidates:
                if "thread_id" not in message:
                    raise ValueError("record the Codex thread before answering")
                batch = _BatchAnswer.model_validate(request).model_dump()
                slugs = [item["public_identifier"] for item in batch["answers"]]
                expected = {candidate["public_identifier"] for candidate in candidates}
                if len(slugs) != len(expected) or set(slugs) != expected:
                    raise ValueError("answer must match the requested candidates")
                if _EMAIL.search(json.dumps(batch, ensure_ascii=False)):
                    raise ValueError("answer contains an email address")
                for item in batch["answers"]:
                    answer = item["answer"]
                    answer["reason"], answer["relationship"] = answer["reason"][:MAX_TEXT], answer["relationship"][:MAX_TEXT]
                    slug = item["public_identifier"]
                    candidate = next(candidate for candidate in message["payload"]["candidates"] if candidate["public_identifier"] == slug)
                    write_json(self.data_root / "asks" / f"{message['id']}-{slug}.json",
                               {"message": message, "candidate": candidate, "evidence_used": used[slug],
                                "answer": answer, "answered_at": now_iso()})
                answers.extend(batch["answers"])
            by_slug = {item["public_identifier"]: item for item in answers}
            message["answer"] = {"answers": [by_slug[candidate["public_identifier"]] for candidate in message["payload"]["candidates"]]}
            write_json(self._path(question_id), message)
        self.sets.message(message["from"]["operator_id"], ANSWER,
                          {"ask_id": message["payload"]["ask_id"], **message["answer"]})
        message["answered_at"] = now_iso()
        write_json(self._path(question_id), message)
        return message
