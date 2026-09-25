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
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.db import identity_queries, identity_views, merge_queries, queries, worth_views
from packs.ingestion.primitives.deep_context.db._view_rows import _hydrate_parents
from packs.ingestion.primitives.deep_context.db._view_sql import LINKEDIN_CTE, PARENT_SELECT
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
from packs.ingestion.primitives.deep_context.merge_candidates import judge
from packs.ingestion.primitives.deep_context.merge_candidates.models import MergePairCandidate, MergePerson
from packs.ingestion.primitives.deep_context.review import server as review_server
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence

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
                MergePerson(f"a-{i}", f"a-{i}", f"A {i}", f"a {i}", parent_id=f"parent-a{i}"),
                MergePerson(f"b-{i}", f"b-{i}", f"B {i}", f"b {i}", parent_id=f"parent-b{i}"),
                f"sig-{i}",
            )
            for i in range(judge.MERGE_JUDGE_CHUNK * 2 + 200)
        ]
        built: list[int] = []
        seen_at_first_answer: list[int] = []
        real_request = judge.judge_request

        def counting_request(*args, **kwargs):
            built.append(1)
            return real_request(*args, **kwargs)

        async def answer_requests(requests, *, output_dir, api_key, client, concurrency, request_version, question_version):
            if not seen_at_first_answer:
                seen_at_first_answer.append(len(built))
            ((digest, _),) = requests.items()
            response = {"answers": {"same_person": {"type": "choice", "probabilities": {"yes": 0.2, "no": 0.8}},
                                    "tone_consistent": {"type": "noul", "noul": 0.5}},
                        "usage": {"input_tokens": 1, "output_tokens": 1}}
            return {digest: mock.Mock(response=response, cached=False, attempts=1)}

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(judge, "judge_request", counting_request), \
                mock.patch.object(judge, "answer_requests", answer_requests):
            verdicts, _, errors = judge.judge_pairs(pairs, owner_name="Owner", output_dir=Path(directory))

        self.assertEqual((len(verdicts), errors), (len(pairs), 0))
        self.assertLessEqual(seen_at_first_answer[0], judge.MERGE_JUDGE_CHUNK)


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
        # The server binds the one-row read, not the whole worth table.
        self.assertIs(review_server.worth_row, worth_views.worth_row)
        self.assertFalse(hasattr(review_server, "worth_rows"))


if __name__ == "__main__":
    unittest.main()
