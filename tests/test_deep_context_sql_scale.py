"""Store reads and writes must not scale their SQL variables or memory with the install.

Created: 2026-09-25
Changelog:
- 2026-09-25: created after a 27k-parent install hit SQLite's 32,766-variable
  limit in the cluster survey; covers every id-set query, the batched merge
  survey, the chunked pair judge, and the single-row worth read.
"""

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.db import context_queries, identity_queries, identity_views, merge_queries, queries, worth_views
from packs.ingestion.primitives.deep_context.db._view_rows import _hydrate_parents
from packs.ingestion.primitives.deep_context.db._view_sql import CANDIDATE_SELECT, LINKEDIN_CTE, PARENT_SELECT
from packs.ingestion.primitives.deep_context.db.identity_policy import IdentityPolicy
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    FactRow,
    LinkRow,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db, DbMaintenance
from packs.ingestion.primitives.deep_context.enrich.research_reconcile import selection
from packs.ingestion.primitives.deep_context.merge_candidates import judge
from packs.ingestion.primitives.deep_context.merge_candidates.models import MergePairCandidate, MergePerson
from packs.ingestion.primitives.deep_context.review import api as review_api
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.pipeline.contract import PeopleRow

# One id bound once must clear SQLite's 32,766-variable limit; bound twice, half that.
ONCE = 33_000
TWICE = 17_000


def _ids(count: int) -> list[str]:
    return [f"parent-{index:012x}" for index in range(count)]


def _facts_payload(index: int) -> dict:
    return {
        "canonical_name": f"Jordan {index}",
        "relationship_to_owner": "friend",
        "employers": [{"name": "Example", "status": "current"}],
        "topics": ["golf", "work"],
        "owned_identifiers": {"emails": [], "phones": [], "urls": []},
    }


def _store(root: Path, count: int, *, facts: bool = False, bundles: bool = False, identifiers: bool = False) -> Db:
    """A cold store of `count` single-person parents with optional facts, bundles, identifiers."""
    db = Db(root / "deep-context.sqlite")
    ids = _ids(count)
    db.project_rows(tuple(ParentRow(pid, f"jordan-{i}", f"Jordan {i}", f"jordan-{i}") for i, pid in enumerate(ids)))
    db.project_rows(tuple(
        PersonRow(f"person-{i}", pid, child_slug=f"jordan-{i}", display_name=f"Jordan {i}") for i, pid in enumerate(ids)
    ))
    if identifiers:
        db.project_rows(tuple(
            PersonIdentifiersProjection(f"person-{i}", (
                PersonIdentifierRow(f"person-{i}", "email", f"jordan{i}@example.com"),
                PersonIdentifierRow(f"person-{i}", "phone", f"1555{i:07d}", f"+1555{i:07d}"),
            ))
            for i in range(count)
        ))
    if facts:
        db.project_rows(tuple(
            ArtifactRow(f"facts:{pid}", "facts", pid, str(root / f"{pid}.jsonl"), "0" * 64, "projected",
                        payload_json=json.dumps({"facts": _facts_payload(i)}))
            for i, pid in enumerate(ids)
        ))
        db.project_rows(tuple(
            FactRow(pid, pid, f"facts:{pid}", machine_worth="yes", facts_json=json.dumps(_facts_payload(i)))
            for i, pid in enumerate(ids)
        ))
    if bundles:
        db.project_rows(tuple(
            ArtifactRow(f"source-bundle:{pid}", "source_bundle", pid, str(root / f"{pid}.json"), "1" * 64, "projected",
                        payload_json=json.dumps({
                            "person_id": pid, "full_name": f"Jordan {i}", "source_channels": ["imessage"],
                            "messages_available": 2,
                            "messages": [
                                {"channel": "imessage", "direction": "from_me", "at": "2026-01-01T00:00:00Z", "text": f"hi {i}"},
                                {"channel": "imessage", "direction": "from_them", "at": "2026-01-02T00:00:00Z", "text": "hello"},
                            ],
                        }))
            for i, pid in enumerate(ids)
        ))
    return db


class CandidateHydrationPlanTest(unittest.TestCase):
    """The candidate select reaches identifiers through the candidate's person.

    Left to itself the planner walks identifiers_by_value(kind) for every
    candidate row, which is quadratic in the store: a 5k-candidate review queue
    took a minute per LinkedIn card. The join order is pinned, and this test
    pins the plan."""

    def test_identifier_lookups_go_through_the_person(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "deep-context.sqlite"
            Db(path)
            with sqlite3.connect(path) as conn:
                plan = [
                    row[3] for row in conn.execute(
                        "EXPLAIN QUERY PLAN " + LINKEDIN_CTE + CANDIDATE_SELECT.format(pending=""),
                        (json.dumps(["parent-1"]),),
                    )
                ]
        identifier_steps = [step for step in plan if "pi USING" in step]
        self.assertEqual(len(identifier_steps), 2, plan)
        for step in identifier_steps:
            self.assertIn("(person_id=?", step, plan)
            self.assertNotIn("identifiers_by_value", step, plan)


class _QueryRecorder:
    """Stands in for a Db and keeps the SQL each read ran."""

    def __init__(self, db: Db) -> None:
        self.db = db
        self.sql: list[str] = []

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        self.sql.append(sql)
        return self.db.query(sql, params)


class EnrichmentQueuePlanTest(unittest.TestCase):
    """The enrichment queue leaves research reuse to the exact fingerprint check.

    It runs on every review page load and status poll. A correlated scan of
    `research` per worth-Yes parent and the planner's kind-first identifier walk
    made it 530ms for 57 queued people on a 7.7k-parent store."""

    def _plan(self) -> list[sqlite3.Row]:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "deep-context.sqlite"
            recorder = _QueryRecorder(Db(path))
            identity_views.enrichment_queue(recorder)
            self.assertEqual(len(recorder.sql), 4)
            sql, source_sql, facts_sql, review_sql = recorder.sql
            self.assertIn("LEFT JOIN imported_people", source_sql)
            self.assertEqual(facts_sql, "SELECT * FROM facts ORDER BY subject_key")
            self.assertEqual(review_sql, "SELECT DISTINCT parent_id FROM links WHERE source=? "
                             "AND machine_action='review' AND decision_action IS NULL")
            with sqlite3.connect(path) as conn:
                return list(conn.execute("EXPLAIN QUERY PLAN " + sql))

    def test_research_is_not_filtered_before_fingerprint_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            recorder = _QueryRecorder(Db(Path(temp) / "deep-context.sqlite"))
            identity_views.enrichment_queue(recorder)
        self.assertEqual(len(recorder.sql), 4)
        sql, source_sql, facts_sql, review_sql = recorder.sql
        self.assertIn("LEFT JOIN imported_people", source_sql)
        self.assertEqual(facts_sql, "SELECT * FROM facts ORDER BY subject_key")
        self.assertEqual(review_sql, "SELECT DISTINCT parent_id FROM links WHERE source=? "
                         "AND machine_action='review' AND decision_action IS NULL")
        self.assertNotRegex(sql, r"\b(?:FROM|JOIN)\s+research\b")

    def test_identifier_lookups_go_through_the_person(self) -> None:
        steps = [row[3] for row in self._plan()]
        identifier_steps = [step for step in steps if " i USING" in step]
        self.assertEqual(len(identifier_steps), 2, steps)
        for step in identifier_steps:
            self.assertIn("(person_id=?", step, steps)
            self.assertNotIn("identifiers_by_value", step, steps)


class ResearchQueueEvidenceTest(unittest.TestCase):
    def test_build_queue_reads_evidence_once_for_the_whole_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _store(Path(directory), 5, facts=True, bundles=True, identifiers=True)
            db.replace_imported_people(tuple(
                PeopleRow(id=f"person-{index}", full_name=f"Jordan {index}") for index in range(5)
            ))
            eligible = identity_views.enrichment_queue(db)
            self.assertEqual(len(eligible), 5)
            expected = [DossierEvidence.from_db(db, row.person_ids).research_bio() for row in eligible]
            with mock.patch.object(
                context_queries, "dossier_evidence_rows", wraps=context_queries.dossier_evidence_rows,
            ) as evidence_rows:
                queue = selection.build_queue(eligible, db)
            self.assertEqual(evidence_rows.call_count, 1)
            self.assertEqual([row.bio for row in queue], expected)
            self.assertTrue(all(queue_row.bio for queue_row in queue))


class IdSetQueryTests(unittest.TestCase):
    def test_artifacts_by_candidate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _store(Path(directory), 1, facts=True)
            parent_id = _ids(1)[0]
            key = "candidate:email:person0@example.com"
            db.project_rows((LinkRow(key, parent_id, key, "candidate_email", candidate_origin=True, source=WriterSource.SYNTHESIS.value),))
            db.project_rows((ArtifactRow(
                f"research:{key}", "research", parent_id, str(Path(directory) / "r.json"), "2" * 64, "projected",
                candidate_key=key, payload_json="{}",
            ),))
            keys = [f"candidate:email:person{i}@example.com" for i in range(ONCE)]
            # One of the 33k keys exists: the big set finds exactly it, the empty set nothing.
            self.assertEqual([row.artifact_key for row in queries.artifacts(db, candidate_keys=keys)], [f"research:{key}"])
            self.assertEqual(queries.artifacts(db, candidate_keys=keys[1:]), ())
            self.assertEqual(queries.artifacts(db, candidate_keys=()), ())

    def test_links_by_row_keys_and_parent_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _store(Path(directory), 1)
            parent_id = _ids(1)[0]
            db.project_rows((LinkRow("jordan-0", parent_id, "jordan-0", "pub", source=WriterSource.SYNTHESIS.value),))
            row_keys = ["jordan-0", *_ids(TWICE)]
            found = identity_queries.links(db, row_keys=row_keys, parent_ids=_ids(TWICE))
            self.assertEqual([row.row_key for row in found], ["jordan-0"])
            self.assertEqual(identity_queries.links(db, row_keys=_ids(TWICE), parent_ids=_ids(TWICE)[1:]), ())

    def test_approved_family_rows_for_every_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _store(Path(directory), 1, identifiers=True)
            rows = identity_views._family_rows(db, _ids(ONCE))
            self.assertEqual([row["person_id"] for row in rows], ["person-0", "person-0"])

    def test_settle_human_families_over_every_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _store(Path(directory), 1)
            with db.transaction() as conn:
                IdentityPolicy.settle_human_families(conn, _ids(ONCE))

    def test_hydrated_parents_bind_one_id_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _store(Path(directory), ONCE, facts=True)
            rows = db.query(LINKEDIN_CTE + PARENT_SELECT.format(where=""))
            self.assertEqual(len(rows), ONCE)
            self.assertEqual(len(_hydrate_parents(db, rows, pending_only=False)), ONCE)

    def test_prune_synthetic_candidates_with_every_key_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _store(Path(directory), 1)
            self.assertEqual(db.prune_synthetic_candidates(tuple(f"synthetic:{i}" for i in range(ONCE))), 0)

    def test_reset_scrubbed_artifacts_over_every_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = _store(root, ONCE)
            db.project_rows(tuple(
                ArtifactRow(f"dossier:{pid}", "dossier", pid, str(root / "dossiers" / f"{pid}.md"), "2" * 64, "projected")
                for pid in _ids(ONCE)
            ))
            counts = DbMaintenance(db).reset_scrubbed_artifacts((root / "dossiers",))
            self.assertEqual(counts.artifacts, ONCE)
            self.assertEqual(db.query("SELECT count(*) AS n FROM artifacts")[0]["n"], 0)


class MergeSurveyTests(unittest.TestCase):
    def test_batches_match_the_narrow_per_parent_read(self) -> None:
        count = merge_queries.MERGE_SURVEY_BATCH * 2 + 100
        with tempfile.TemporaryDirectory() as directory:
            db = _store(Path(directory), count, facts=True, bundles=True, identifiers=True)
            # Every seventh parent is a two-person family, so a misgrouped identifier
            # or person would land on the wrong parent's packet.
            ids = _ids(count)
            db.project_rows(tuple(
                PersonRow(f"sibling-{i}", pid, child_slug=f"sibling-{i}", display_name=f"Jordan {i}")
                for i, pid in enumerate(ids) if i % 7 == 0
            ))
            db.project_rows(tuple(
                PersonIdentifiersProjection(f"sibling-{i}", (
                    PersonIdentifierRow(f"sibling-{i}", "email", f"sibling{i}@example.com"),
                ))
                for i in range(count) if i % 7 == 0
            ))
            db.replace_imported_people(tuple(
                PeopleRow(id=person_id, full_name=f"Jordan {i}")
                for i in range(count)
                for person_id in ([f"person-{i}", f"sibling-{i}"] if i % 7 == 0 else [f"person-{i}"])
            ))
            batches: list[int] = []
            narrow = merge_queries.dossier_evidence_rows

            def recording(store, subject_ids):
                batches.append(len(tuple(subject_ids)))
                return narrow(store, subject_ids)

            with mock.patch.object(merge_queries, "dossier_evidence_rows", recording):
                people = merge_queries.merge_people(db)

            self.assertEqual(len(people), count)
            self.assertEqual(len(batches), math.ceil(count / merge_queries.MERGE_SURVEY_BATCH))
            self.assertLessEqual(max(batches), merge_queries.MERGE_SURVEY_BATCH)
            # Every parent, so batch boundaries (499/500, 999/1000) are covered.
            for person in people:
                index = int(person.parent_id.removeprefix("parent-"), 16)
                self.assertEqual(person.evidence, DossierEvidence.from_parent_db(db, person.parent_id))
                expected = {f"jordan{index}@example.com"} | ({f"sibling{index}@example.com"} if index % 7 == 0 else set())
                self.assertEqual(set(person.emails), expected)
                self.assertEqual(len(person.member_person_ids), 2 if index % 7 == 0 else 1)


class JudgeChunkTests(unittest.TestCase):
    def test_pair_requests_are_built_one_chunk_at_a_time(self) -> None:
        pairs = [
            MergePairCandidate(
                MergePerson(f"a-{i}", f"a-{i}", f"A {i}", f"a {i}", parent_id=f"parent-a{i}", source_names=(f"A {i}",)),
                MergePerson(f"b-{i}", f"b-{i}", f"B {i}", f"b {i}", parent_id=f"parent-b{i}", source_names=(f"B {i}",)),
                f"sig-{i}",
            )
            for i in range(judge.MERGE_JUDGE_CHUNK * 2 + 200)
        ]
        import asyncio
        from types import SimpleNamespace
        from packs.ingestion.primitives.deep_context.shared.openai_responses import (
            OpenAIResponsesCaller, OpenAIResponsesConfig,
        )

        built: list[int] = []
        completed = []
        completed_at_chunk_start = []
        seen_at_first_answer: list[int] = []
        real_request = judge.judge_request

        def counting_request(*args, **kwargs):
            if len(built) % judge.MERGE_JUDGE_CHUNK == 0:
                completed_at_chunk_start.append(len(completed))
            built.append(1)
            return real_request(*args, **kwargs)

        async def create_response(**request):
            await asyncio.sleep(0)
            if not seen_at_first_answer:
                seen_at_first_answer.append(len(built))
            return SimpleNamespace(
                status="completed",
                output_text=json.dumps({"decision": "uncertain", "confidence": .8,
                    "reason": "Synthetic records lack an individual identity tie",
                    "identity_evidence": "", "tone_consistent": False}),
                usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                      output_tokens_details=None),
            )

        client = SimpleNamespace(responses=SimpleNamespace(create=create_response),
                                 close=mock.AsyncMock())
        config = OpenAIResponsesConfig(judge.MODEL_ID, judge.REASONING_EFFORT, 64, 120, 0)
        caller = OpenAIResponsesCaller(config, client=client)
        with mock.patch.object(judge, "judge_request", counting_request), \
                mock.patch.object(judge, "OpenAIResponsesCaller", return_value=caller):
            verdicts, usage, errors = judge.judge_pairs(
                pairs, owner_name="Owner", config=config, on_verdict=completed.append,
            )

        self.assertEqual((len(verdicts), errors), (len(pairs), 0))
        self.assertLessEqual(seen_at_first_answer[0], judge.MERGE_JUDGE_CHUNK)
        self.assertEqual(completed_at_chunk_start, [0, judge.MERGE_JUDGE_CHUNK, judge.MERGE_JUDGE_CHUNK * 2])
        self.assertEqual(len(completed), len(pairs))
        self.assertEqual((usage.input_tokens, usage.output_tokens), (len(pairs), len(pairs)))
        client.close.assert_awaited_once()


class WorthRowTests(unittest.TestCase):
    def test_one_worth_row_is_read_by_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _store(Path(directory), 3, facts=True)
            key = f"parent-worth:{_ids(3)[1]}"
            row = worth_views.worth_row(db, key)
            self.assertIsNotNone(row)
            self.assertEqual((row.key, row.effective), (key, "yes"))
            self.assertIsNone(worth_views.worth_row(db, "parent-worth:parent-000000000fff"))

    def test_a_worth_decision_reads_its_row_by_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = _store(Path(directory), 3, facts=True)
            parent_id = _ids(3)[2]
            with mock.patch.object(worth_views, "_worth_rows", wraps=worth_views._worth_rows) as read:
                row = worth_views.worth_row(db, f"parent-worth:{parent_id}")
            self.assertEqual(row.key, f"parent-worth:{parent_id}")
            read.assert_called_once()
            self.assertEqual(read.call_args.kwargs.get("parent_id"), parent_id)
        # The /worth route binds the one-row read, not the whole worth table.
        self.assertIs(review_api.worth_row, worth_views.worth_row)
        self.assertFalse(hasattr(review_api, "worth_rows"))


if __name__ == "__main__":
    unittest.main()
