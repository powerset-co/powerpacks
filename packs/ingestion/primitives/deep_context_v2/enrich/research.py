"""Step 2, research: Parallel looks for the LinkedIn of a worth-yes family the connections do not name.

Who is researched: every worth-yes family on a p: id with no pre-match, no research row at its current
evidence handle, and no human LinkedIn verdict on any member. The handle is a sha256 over the family's
collapsed facts and the research prompt version; it never includes the parent id, so a family whose
facts are unchanged is never researched twice. Found: status complete, the URL in the result. Not found:
status no_match, the result kept whole when it is usable as a synthetic card (a name plus positions or a
location), else no result.

Created: 2026-10-07
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from parallel import Parallel
from parallel.types import RunInputParam, TaskGroupStatusEvent, TaskRunEvent, TaskSpecParam

from packs.ingestion.primitives.deep_context_v2.db import queries_enrich, queries_worth
from packs.ingestion.primitives.deep_context_v2.db.queries_enrich import Research, ResearchRow
from packs.ingestion.primitives.deep_context_v2.db.schema import ResearchStatus
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.enrich.family import Family, load_families
from packs.ingestion.primitives.deep_context_v2.enrich.pre_match import PreMatch, enters, pre_match_families
from packs.ingestion.primitives.deep_context_v2.node import Node
from packs.ingestion.primitives.deep_context_v2.openai import load_env
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import SynthesizedFacts
from packs.ingestion.schemas.people_schema import normalize_linkedin_url

from packs.ingestion.primitives.deep_context_v2 import assets

_HERE = Path(__file__).parent
RESEARCH_INSTRUCTIONS: str = assets.text(_HERE, "contact_research_instructions.txt")
_SCHEMAS: dict[str, Any] = assets.json_file(_HERE, "contact_research_schema.txt")
PROMPT_VERSION = "contact-research-2026-10-07"  # in every handle: a new prompt is a new question
# Parallel: the processor tier, its price, the API, and how runs are submitted and read back.
PARALLEL_PROCESSOR = "core2x"
PARALLEL_PRICE_PER_RUN_USD = 0.05   # core2x, per completed person
PARALLEL_BASE_URL = "https://api.parallel.ai"
PARALLEL_BETA_HEADER = "search-extract-2025-10-10,field-basis-2025-11-25"
PARALLEL_BATCH_SIZE = 500           # runs per add_runs call
PARALLEL_STREAM_TIMEOUT = 3600      # Parallel's maximum for one event stream
# The task spec has no instructions field: the objective rides on the output schema's description.
_OUTPUT_SCHEMA: dict[str, Any] = dict(_SCHEMAS["output"])
_OUTPUT_SCHEMA["description"] = RESEARCH_INSTRUCTIONS
PARALLEL_TASK_SPEC: TaskSpecParam = {"input_schema": {"json_schema": _SCHEMAS["input"]},
                             "output_schema": {"json_schema": _OUTPUT_SCHEMA}}


@dataclass(frozen=True)
class ResearchSubject:
    """One family to research: its handle and the dossier Parallel reads."""

    parent_id: str
    handle: str
    dossier: str
    guidance: str = ""   # the reviewer's words about the right person; '' for the pipeline's own research


def dossier(family: Family) -> str:
    """The family's collapsed facts, exactly what the handle covers."""
    return json.dumps(family.facts.to_payload(), ensure_ascii=False, sort_keys=True)


def handle(facts: SynthesizedFacts) -> str:
    """The research key: the family's collapsed facts and the prompt version. Never the parent id, so a
    family whose facts are unchanged is never researched twice, and review finds the card by the same key."""
    payload: str = json.dumps({"facts": facts.to_payload(), "prompt": PROMPT_VERSION},
                              ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def guided_subject(parent_id: str, facts: SynthesizedFacts, guidance: str) -> ResearchSubject:
    """The review page's re-research: the same question with the reviewer's words beside the dossier. Its
    handle covers the guidance too, so the family's own research card is untouched and the same words are
    never paid for twice."""
    payload: str = json.dumps({"facts": facts.to_payload(), "guidance": guidance, "prompt": PROMPT_VERSION},
                              ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return ResearchSubject(parent_id, hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                           json.dumps(facts.to_payload(), ensure_ascii=False, sort_keys=True), guidance)


def subjects(families: list[Family], matches: PreMatch, done: dict[str, Research]) -> list[ResearchSubject]:
    """The worth-yes p: families with no pre-match, no research at their handle and no human LinkedIn verdict."""
    todo: list[ResearchSubject] = []
    handles: set[str] = set()
    for family in families:
        if not enters(family) or family.parent_id in matches.matched:
            continue
        family_handle: str = handle(family.facts)
        # Two families with identical facts are one question: one run, both read the row by its handle.
        if family_handle in done or family_handle in handles:
            continue
        handles.add(family_handle)
        todo.append(ResearchSubject(family.parent_id, family_handle, dossier(family)))
    return todo


def research_url(result: Research) -> str:
    """The LinkedIn URL a complete research row proposes."""
    assert result.result_json is not None
    content: dict[str, Any] = json.loads(result.result_json)["content"]
    return normalize_linkedin_url(content["linkedin_url"])


def row_from_output(subject: ResearchSubject, output: dict[str, Any], now: str) -> ResearchRow:
    """complete with the URL found; else no_match, keeping the result only when it is a usable card."""
    content: dict[str, Any] = output["content"]
    result_json: str = json.dumps(output, ensure_ascii=False, sort_keys=True)
    if content.get("linkedin_url"):
        return (subject.handle, subject.parent_id, ResearchStatus.COMPLETE.value, result_json, now)
    usable: bool = bool(content.get("real_name") and (content.get("work_experience") or content.get("location_city")
                                                      or content.get("location_country")))
    if usable:
        return (subject.handle, subject.parent_id, ResearchStatus.NO_MATCH.value, result_json, now)
    return (subject.handle, subject.parent_id, ResearchStatus.NO_MATCH.value, None, now)


class ResearchStep(Node):
    """The paid step alone: submit every subject to Parallel and write each answer as it arrives."""

    name = "enrich-research"
    reads = ("current_parent", "current_worth", "candidates", "candidate_identifiers", "facts", "bundles",
             "connections", "current_linkedins", "research")
    writes = ("research",)

    def __init__(self, conn: sqlite3.Connection, data_root: Path, *, limit: int | None) -> None:
        super().__init__(conn, data_root)
        self.limit = limit

    def subjects(self) -> list[ResearchSubject]:
        """The worth-yes p: families with no pre-match, no research at their handle and no human LinkedIn verdict, one per handle."""
        families: list[Family] = load_families(self.conn)
        matches: PreMatch = pre_match_families(families, queries_worth.all_connections(self.conn),
                                               queries_enrich.connection_emails(self.conn))
        todo: list[ResearchSubject] = subjects(families, matches, queries_enrich.research_by_handle(self.conn))
        if self.limit is not None:
            todo = todo[: self.limit]
        return todo

    def estimate(self) -> dict[str, object]:
        """The dry run: how many families would be researched and the price."""
        todo: list[ResearchSubject] = self.subjects()
        return {"families_to_research": len(todo), "processor": PARALLEL_PROCESSOR,
                "estimated_cost_usd": round(len(todo) * PARALLEL_PRICE_PER_RUN_USD, 2)}

    def execute(self) -> dict[str, int]:
        """Submit every subject to Parallel and write each answer as it arrives."""
        return submit(self.conn, self.subjects())


def submit(conn: sqlite3.Connection, todo: list[ResearchSubject]) -> dict[str, int]:
    """One Parallel task group for every subject; each answer is written and committed as it arrives."""
    by_handle: dict[str, ResearchSubject] = {}
    for subject in todo:
        by_handle[subject.handle] = subject
    load_env()
    client = Parallel(api_key=os.environ["PARALLEL_API_KEY"], base_url=PARALLEL_BASE_URL,
                      default_headers={"parallel-beta": PARALLEL_BETA_HEADER}, max_retries=0)
    # Submit every run, then read the event stream until the group is done.
    group_id: str = str(client.task_group.create(metadata={"source": "powerpacks"}).task_group_id)
    inputs: list[RunInputParam] = []
    for subject in todo:
        run_input: dict[str, object] = {"dossier": subject.dossier}
        if subject.guidance:
            run_input["guidance"] = subject.guidance
        inputs.append({"input": run_input, "metadata": {"handle": subject.handle}, "processor": PARALLEL_PROCESSOR})
    for start in range(0, len(inputs), PARALLEL_BATCH_SIZE):
        client.task_group.add_runs(group_id, inputs=inputs[start:start + PARALLEL_BATCH_SIZE], default_task_spec=PARALLEL_TASK_SPEC)
    counts: dict[str, int] = {"research_submitted": len(todo), "complete": 0, "no_match": 0, "failed": 0}
    with client.task_group.events(group_id, api_timeout=PARALLEL_STREAM_TIMEOUT, timeout=PARALLEL_STREAM_TIMEOUT + 30) as events:
        for event in events:
            if isinstance(event, TaskGroupStatusEvent) and not event.status.is_active:
                break
            if not isinstance(event, TaskRunEvent) or event.run.is_active:
                continue
            assert event.run.metadata is not None
            subject = by_handle[str(event.run.metadata["handle"])]
            row: ResearchRow = (subject.handle, subject.parent_id, ResearchStatus.FAILED.value, None, now_iso())
            if event.run.status == "completed":
                output = event.output
                if output is None:
                    output = client.task_run.result(event.run.run_id, timeout=PARALLEL_STREAM_TIMEOUT + 30).output
                row = row_from_output(subject, output.model_dump(mode="json", exclude_none=True), now_iso())
            queries_enrich.upsert_research(conn, row)
            conn.commit()  # each answer is kept the moment it is paid for
            counts[row[2]] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="07 Enrich step 2: Parallel research for families the connections miss.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--dry-run", action="store_true", help="count families and price the runs; no Parallel call")
    parser.add_argument("--limit", type=int, default=None, help="research only the first N families")
    args = parser.parse_args(argv)
    node = ResearchStep(open_store(store_path(args.data_root)), args.data_root, limit=args.limit)
    if args.dry_run:
        print(json.dumps(node.estimate(), indent=2))
        return 0
    manifest = node.run()
    print(manifest.status, manifest.counts, manifest.error or "")
    if manifest.status == "completed":
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
