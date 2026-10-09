"""Answer an Ask the Set message from local family evidence, send the answer back, and audit.

Changelog:
  2026-10-09: reason and relationship are cut to the asker's limit (MAX_TEXT) instead of failing.
  2026-10-09: the model sees the ask's role (title, company, job description).
  2026-10-08: answer `ask` agent messages; the relay's leased ask tasks are gone.
  2026-10-08: add the owner's answer worker and run CLI.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.indexing.lib.llm_config import DEFAULT_SYNTHESIS_MODEL
from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.deep_context_v2.db import queries_dedupe, queries_enrich, queries_worth
from packs.ingestion.primitives.deep_context_v2.db.schema import LINKEDIN_PARENT_PREFIX, Verdict
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import CACHE_RELATIVE_DIR, profile_from_record
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesCaller, OpenAIResponsesConfig
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts, collapse
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path, read_usable_cached_profile
from packs.ingestion.schemas.people_schema import extract_public_identifier
from packs.powerset.primitives.agent_inbox.messages import MAX_TEXT
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth

_EMAIL = re.compile(r"[^\s@\"<>]+@[^\s@\"<>]+\.[^\s@\"<>]+")
_PROMPT = Path(__file__).with_name("prompts") / "answer.txt"
_HTTP_TIMEOUT = 30
MESSAGES_PATH = "/v2/agent-messages"
ANSWER = "ask_answer"


class _Answer(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)

    verdict: Literal["recommend", "not_fit", "unsure"]
    reason: str
    can_intro: bool
    relationship: str
    last_contact: Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")] | None
    confidence: float = Field(ge=0, le=1)


def _evidence(conn: sqlite3.Connection, data_root: Path, slug: str) -> tuple[dict, list[dict]] | None:
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


async def _answer(task: dict, evidence: dict) -> dict:
    config = OpenAIResponsesConfig.resolve(model=DEFAULT_SYNTHESIS_MODEL, effort="low", timeout=120, max_retries=0)
    async with OpenAIResponsesCaller(config) as caller:
        result = await caller.call(
            system_prompt=_PROMPT.read_text(encoding="utf-8"),
            user_prompt=json.dumps({**task, "evidence": evidence}, ensure_ascii=False),
            schema=_Answer.model_json_schema(), schema_name="ask_answer", context="ask answer",
        )
    answer = _Answer.model_validate(result).model_dump()
    # The asker's inbox rejects longer text (messages.Verdict): cut it rather than lose the whole answer.
    answer["reason"], answer["relationship"] = answer["reason"][:MAX_TEXT], answer["relationship"][:MAX_TEXT]
    if _EMAIL.search(json.dumps(answer, ensure_ascii=False)):
        raise ValueError("answer contains an email address")
    return answer


def answer_message(message: dict, *, repo_root: Path, env_file: Path) -> dict:
    """Answer one `ask` message from local family evidence and send one `ask_answer` back to the asker."""
    load_dotenv(env_file, override=False)
    data_root = repo_root / ".powerpacks"
    payload = message["payload"]
    answers = []
    for candidate in payload["candidates"]:
        slug = candidate["public_identifier"]
        with closing(open_store(store_path(data_root))) as conn:
            found = _evidence(conn, data_root, slug)
        audit = data_root / "asks" / f"{message['id']}-{slug}.json"
        if found is None:
            answer: dict = {"declined": True, "reason": "not_in_store"}
        elif audit.is_file():
            # Answered before, but the reply did not reach the relay: send the same answer, no second model call.
            answer = json.loads(audit.read_text(encoding="utf-8"))["answer"]
        else:
            evidence, used = found
            try:
                answer = asyncio.run(_answer({"question": payload["question"], "role": payload["role"], "candidate": candidate}, evidence))
            except Exception as exc:
                # Exception text can carry model output; log only its type. The asker sees "couldn't answer".
                print(f"ask-worker: {slug} failed ({type(exc).__name__})", file=sys.stderr)
                answers.append({"public_identifier": slug, "answer": {"declined": True, "reason": "failed"}})
                continue
            write_json(audit,
                       {"message": message, "candidate": candidate, "evidence_used": used, "answer": answer,
                        "answered_at": now_iso()})
        answers.append({"public_identifier": slug, "answer": answer})
        print(f"ask-worker: {slug} {answer.get('verdict', 'declined')}", file=sys.stderr)
    reply = {"to": message["from"]["operator_id"], "kind": ANSWER,
             "payload": {"ask_id": payload["ask_id"], "answers": answers}}
    request = urllib.request.Request(
        auth.api_base(env_file) + MESSAGES_PATH, data=json.dumps(reply).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {auth.bearer_token(env_file)}", "Content-Type": "application/json",
                 "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
        json.load(response)
    return {"ask_id": payload["ask_id"], "answers": answers}
