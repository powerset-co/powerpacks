"""Lease Ask the Set tasks, answer from local family evidence, post, and audit.

Changelog:
  2026-10-08: add the owner's answer worker and run CLI.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.indexing.lib.llm_config import DEFAULT_SYNTHESIS_MODEL
from packs.ingestion.primitives.common.jsonio import emit, now_iso, write_json
from packs.ingestion.primitives.deep_context_v2.db import queries_dedupe, queries_enrich, queries_worth
from packs.ingestion.primitives.deep_context_v2.db.schema import LINKEDIN_PARENT_PREFIX, Verdict
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.enrich.profiles import CACHE_RELATIVE_DIR, profile_from_record
from packs.ingestion.primitives.deep_context_v2.openai import OpenAIResponsesCaller, OpenAIResponsesConfig
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts, collapse
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path, read_usable_cached_profile
from packs.ingestion.schemas.people_schema import extract_public_identifier
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth

_EMAIL = re.compile(r"[^\s@\"<>]+@[^\s@\"<>]+\.[^\s@\"<>]+")
_PROMPT = Path(__file__).with_name("prompts") / "answer.txt"
_HTTP_TIMEOUT = 30


class _Answer(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)

    verdict: Literal["recommend", "not_fit", "unsure"]
    reason: str = Field(max_length=240)
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
            user_prompt=json.dumps({"question": task["question"], "candidate": task["candidate"],
                                    "evidence": evidence}, ensure_ascii=False),
            schema=_Answer.model_json_schema(), schema_name="ask_answer", context="ask answer",
        )
    answer = _Answer.model_validate(result).model_dump()
    if _EMAIL.search(json.dumps(answer, ensure_ascii=False)):
        raise ValueError("answer contains an email address")
    return answer


def _request(base: str, path: str, headers: dict, body: dict | None = None) -> dict:
    request = urllib.request.Request(base + path, headers=headers,
                                     data=json.dumps(body).encode("utf-8") if body is not None else None)
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
        return json.load(response)


def run(*, repo_root: Path, env_file: Path, device_id: str) -> list[dict]:
    """Post answers for this device's leased tasks; leave failed tasks to expire and retry."""
    headers = {"Authorization": f"Bearer {auth.bearer_token(env_file)}", "X-Device-Id": device_id,
               "Content-Type": "application/json", "Accept": "application/json"}
    base = auth.api_base(env_file)
    tasks = _request(base, "/v2/ask-tasks?limit=20", headers)["tasks"]
    load_dotenv(env_file, override=False)
    data_root = repo_root / ".powerpacks"
    posted = []
    for task in tasks:
        slug = task["candidate"]["public_identifier"]
        try:
            task_id = str(UUID(task["task_id"]))
            with closing(open_store(store_path(data_root))) as conn:
                found = _evidence(conn, data_root, slug)
            if found is None:
                answer = {"declined": True, "reason": "not_in_store"}
            else:
                evidence, used = found
                answer = asyncio.run(_answer(task, evidence))
            _request(base, f"/v2/ask-tasks/{task_id}/answer", headers, answer)
            if found is not None:
                write_json(data_root / "asks" / f"{task_id}.json",
                           {"task": task, "evidence_used": used, "answer": answer, "answered_at": now_iso()})
            posted.append(answer)
            print(f"ask-worker: {slug} {answer.get('verdict', 'declined')}", file=sys.stderr)
        except Exception as exc:
            # Exception text can contain model output or HTTP bodies; log only the exception type.
            print(f"ask-worker: {slug} failed ({type(exc).__name__})", file=sys.stderr)
    return posted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--env-file", type=Path, default=_REPO_ROOT / ".env")
    args = parser.parse_args(argv)
    device_id = (_REPO_ROOT / ".powerpacks" / "device-id").read_text(encoding="utf-8").strip()
    emit(run(repo_root=_REPO_ROOT, env_file=args.env_file, device_id=device_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
