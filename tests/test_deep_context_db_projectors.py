from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from parallel.types import TaskRunJsonOutput

from packs.ingestion.primitives.common.legacy import MESSAGE_LINKEDIN_PREFIX
from packs.ingestion.primitives.deep_context.migration.legacy import (
    LEGACY_REVIEW_COLUMNS,
    LegacyImportError,
    import_legacy,
)
from packs.ingestion.primitives.deep_context.db import identity_queries, projectors
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactProjection,
    ArtifactRow,
    CandidatePeopleProjection,
    CandidatePersonRow,
    GuidanceRow,
    LinkRow,
    ParentRow,
    PersonRow,
    SyntheticProfileRow,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_progress
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.worth_views import worth_counts
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import (
    project_imported_people,
    read_imported_people,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research import projection
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import (
    ResearchQueueRow,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import ResearchRunParams
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.schemas.people_schema import generate_person_id, legacy_message_linkedin_id
from deep_context_sqlite_test_helpers import query, write_override_rows


class ProjectorTest(unittest.TestCase):
    NOW = "2026-08-06T00:00:00Z"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.db.project_rows(
            (
                ParentRow(
                    "parent-1",
                    "parent-worth:parent-1",
                    "Jordan Bravo",
                    updated_at=self.NOW,
                ),
                PersonRow(
                    "person-a",
                    "parent-1",
                    display_name="Jordan Bravo",
                    updated_at=self.NOW,
                ),
                PersonRow(
                    "person-b",
                    "parent-1",
                    display_name="Jordan B.",
                    updated_at=self.NOW,
                ),
                LinkRow(
                    "attached-jordan",
                    "parent-1",
                    "attached-jordan",
                    "pub",
                    "https://www.linkedin.com/in/attached-jordan",
                    "Jordan Bravo",
                    source="deep-context-reconcile",
                    updated_at=self.NOW,
                ),
            )
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_snapshot_absence_uses_none_until_the_wire_boundary(self) -> None:
        self.db.project_rows(
            (
                GuidanceRow(
                    "parent-1",
                    "parent-1",
                    "Find Jordan",
                    detail_json=json.dumps(
                        {
                            "slug": "jordan-bravo",
                            "row_key": "attached-jordan",
                            "name": "Jordan Bravo",
                            "guidance": "Find Jordan",
                            "state": "queued",
                            "detail": "",
                        }
                    ),
                ),
            )
        )

        detail = identity_queries.guidance_rows(self.db)[0].detail
        self.assertIsNotNone(detail)
        self.assertIsNone(detail.submitted_at)
        self.assertIsNone(detail.updated_at)
        self.assertIsNone(detail.new_url)
        decision = identity_queries.review_rows(self.db, key="attached-jordan")[0]
        self.assertIsNone(decision.action)
        self.assertIsNone(decision.approved)
        self.assertIsNone(identity_queries.links(self.db, row_key="attached-jordan")[0].machine_proposed_url)

    @staticmethod
    def _artifact(
        key: str,
        kind: str,
        path: str,
        fingerprint: str,
        *,
        candidate: str | None = None,
        input_fingerprint: str | None = None,
        payload: str | None = None,
    ) -> dict[str, object]:
        return {
            "artifact_key": key,
            "kind": kind,
            "parent_id": "parent-1",
            "person_id": None,
            "candidate_key": candidate,
            "path": path,
            "content_fingerprint": fingerprint,
            "input_fingerprint": input_fingerprint,
            "status": "projected",
            "error": None,
            "payload_json": payload,
            "projected_at": ProjectorTest.NOW,
        }

    @staticmethod
    def _link(**values: object) -> dict[str, object]:
        row = {
            "row_key": None,
            "parent_id": "parent-1",
            "public_identifier": None,
            "kind": None,
            "linkedin_url": None,
            "display_name": None,
            "machine_action": None,
            "machine_approved": None,
            "machine_confidence": None,
            "machine_reason": None,
            "machine_judgment": None,
            "machine_proposed_url": None,
            "machine_proposed_public_identifier": None,
            "authoritative_detach": 0,
            "candidate_origin": 0,
            "raw_import": 0,
            "paid_profile": 0,
            "judgment_fingerprint": None,
            "judgment_artifact_path": None,
            "judgment_payload_json": None,
            "decision_action": None,
            "decision_approved": None,
            "decision_source": None,
            "decision_note": None,
            "decided_at": None,
            "replacement_url": None,
            "replacement_public_identifier": None,
            "source": None,
            "updated_at": ProjectorTest.NOW,
        }
        row.update(values)
        return row

    def _state(self) -> dict[str, list[dict[str, object]]]:
        result = {}
        for table, order in (
            ("parents", "parent_id"),
            ("people", "person_id"),
            ("links", "row_key"),
            ("candidate_people", "row_key, person_id"),
            ("artifacts", "artifact_key"),
            ("facts", "subject_key"),
            ("research", "handle"),
            ("synthetic_profiles", "public_identifier"),
        ):
            rows = [dict(row) for row in self.db.query(f"SELECT * FROM {table} ORDER BY {order}")]
            for row in rows:
                if "path" in row:
                    relative = Path(str(row["path"])).relative_to(self.root.resolve())
                    row["path"] = f"$ROOT/{relative.as_posix()}"
            result[table] = rows
        return result

    def test_typed_projection_matches_captured_legacy_rows(self) -> None:
        """The literal expected state was captured from the retired dict projector."""
        research_result = ResearchResult.from_output(TaskRunJsonOutput.model_validate({
            "type": "json",
            "content": {
                "real_name": "Jordan Bravo",
                "work_experience": [{"title": "Founder", "company_name": "Example", "is_current": True}],
                "education": [],
                "location_city": "Oakland",
                "location_country": "US",
                "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
                "summary": "Founder",
            },
            "basis": [{"field": "linkedin_url", "reasoning": "fixture", "citations": []}],
        }))
        research_bytes = (
            json.dumps(
                research_result.output.model_dump(mode="json", exclude_none=True),
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()
        research_payload_json = research_result.output.model_dump_json(exclude_none=True)
        profile_bytes = b'{"headline":"Founder","public_identifier":"attached-jordan"}\n'
        avatar_bytes = b"\x89PNG\r\n\x1a\nfixture-avatar"
        facts_bytes = (
            b'{"confidence":0.88,"facts":{"network_worth":{"decision":"yes",'
            b'"reason":"Known collaborator"}},'
            b'"input_evidence_fingerprint":"facts-input-v1"}\n'
        )
        source_bytes = b'{"messages":[],"person_id":"parent-1"}\n'
        synthetic_bytes = b'{"full_name":"Jordan Synth","linkedin_url":null,"public_identifier":"synth-jordan"}\n'
        changed_synthetic_bytes = (
            b'{"full_name":"Jordan Synth Updated","linkedin_url":null,"public_identifier":"synth-jordan"}\n'
        )
        subject = self.root / "subject"
        subject.mkdir()
        research_path = subject / "00_parallel_result.json"
        research_path.write_bytes(research_bytes)
        profile_path, avatar_path = self.root / "profile.json", self.root / "avatar.bin"
        facts_path, source_path = self.root / "facts.jsonl", self.root / "bundle.json"
        synthetic_path = self.root / "synthetic.json"
        profile_path.write_bytes(profile_bytes)
        avatar_path.write_bytes(avatar_bytes)
        facts_path.write_bytes(facts_bytes)
        source_path.write_bytes(source_bytes)
        synthetic_path.write_bytes(synthetic_bytes)

        queue_row = ResearchQueueRow(
            parent_id="parent-1",
            candidate_exists=False,
            row_key="candidate:email:jordan",
            handle="subject",
            source_person_ids=("person-a", "person-b"),
            source_candidate_public_identifier="candidate:email:jordan",
            display_name="Jordan Bravo",
        )
        params = ResearchRunParams(
            db=self.db,
            output_dir=self.root,
            rows=(queue_row,),
        )
        with mock.patch.object(projection, "now_iso", return_value=self.NOW):
            research = projection.research_artifact_projection(
                params, queue_row, research_result, research_path, research_bytes
            )

        def synthetic_projection(data: bytes, name: str, people: tuple[str, ...]) -> ArtifactProjection:
            payload = json.loads(data)
            return ArtifactProjection(
                artifact=ArtifactRow(
                    "synthetic:synth-jordan",
                    "synthetic",
                    "parent-1",
                    str(synthetic_path.resolve()),
                    hashlib.sha256(data).hexdigest(),
                    "projected",
                    candidate_key="synth-jordan",
                    projected_at=self.NOW,
                ),
                candidate=LinkRow(
                    "synth-jordan",
                    "parent-1",
                    "synth-jordan",
                    "synthetic",
                    display_name=name,
                    machine_action="verify",
                    machine_approved="auto",
                    source="deep-research",
                    updated_at=self.NOW,
                ),
                candidate_people=CandidatePeopleProjection(
                    "synth-jordan",
                    tuple(CandidatePersonRow("synth-jordan", person_id, "parent-1") for person_id in people),
                ),
                synthetic_profile=SyntheticProfileRow(
                    "synth-jordan",
                    "synth-jordan",
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    "synthetic:synth-jordan",
                    None,
                    name,
                    self.NOW,
                ),
            )

        profile_payload = json.loads(profile_bytes)
        typed_rows = (research,) + (
            ArtifactRow(
                "profile:attached-jordan",
                "profile",
                "parent-1",
                str(profile_path.resolve()),
                "c6d1f753c9e248c80ca61ae5f1e2d2a00bdf04807cc01b5b652687003f080e36",
                "projected",
                candidate_key="attached-jordan",
                payload_json=json.dumps(profile_payload, separators=(",", ":")),
                projected_at=self.NOW,
            ),
            ArtifactRow(
                "avatar:attached-jordan",
                "avatar",
                "parent-1",
                str(avatar_path.resolve()),
                "9100fdacba060a36e4ce17c56a376671feab20129160d1f55b3d2ec368d85f6b",
                "projected",
                candidate_key="attached-jordan",
                payload_json=json.dumps(
                    {
                        "content_type": "image/png",
                        "base64": base64.b64encode(avatar_bytes).decode("ascii"),
                    },
                    separators=(",", ":"),
                ),
                projected_at=self.NOW,
            ),
            synthetic_projection(synthetic_bytes, "Jordan Synth", ("person-a", "person-b")),
        )
        self.assertEqual(self.db.project_rows(typed_rows), 4)
        with mock.patch.object(projectors, "now_iso", return_value=self.NOW):
            projectors.project_parent_fact(self.db, facts_path, "parent-1")
            projectors.project_parent_source_bundle(self.db, source_path, "parent-1")

        synthetic_path.write_bytes(changed_synthetic_bytes)
        changed = synthetic_projection(changed_synthetic_bytes, "Jordan Synth Updated", ("person-b",))
        self.assertEqual(self.db.project_rows((changed,)), 1)
        state = self._state()
        self.assertEqual(self.db.project_rows((changed,)), 0)
        self.assertEqual(self._state(), state)

        avatar_payload = '{"content_type":"image/png","base64":"iVBORw0KGgpmaXh0dXJlLWF2YXRhcg=="}'
        expected = {
            "artifacts": [
                self._artifact(
                    "avatar:attached-jordan",
                    "avatar",
                    "$ROOT/avatar.bin",
                    "9100fdacba060a36e4ce17c56a376671feab20129160d1f55b3d2ec368d85f6b",
                    candidate="attached-jordan",
                    payload=avatar_payload,
                ),
                self._artifact(
                    "facts:parent-1",
                    "facts",
                    "$ROOT/facts.jsonl",
                    "4c7bbbfe50480ebef7e923a9179e98e55f7dc5af0a34b77f05e66d5b8bcc471f",
                    input_fingerprint="facts-input-v1",
                    payload=(
                        '{"confidence":0.88,"facts":{"network_worth":'
                        '{"decision":"yes","reason":"Known collaborator"}},'
                        '"input_evidence_fingerprint":"facts-input-v1"}'
                    ),
                ),
                self._artifact(
                    "profile:attached-jordan",
                    "profile",
                    "$ROOT/profile.json",
                    "c6d1f753c9e248c80ca61ae5f1e2d2a00bdf04807cc01b5b652687003f080e36",
                    candidate="attached-jordan",
                    payload='{"headline":"Founder","public_identifier":"attached-jordan"}',
                ),
                self._artifact(
                    "research:subject",
                    "research",
                    "$ROOT/subject/00_parallel_result.json",
                    "bec813940a813c79449d1a1c467809be3d33defcb83f9bea30f897ef38ba729c",
                    candidate="candidate:email:jordan",
                    input_fingerprint="710f5bb77050690c5d78d87277c3071372d8a8fefe04948ec868b58d9d63ba90",
                    payload=research_payload_json,
                ),
                self._artifact(
                    "source-bundle:parent-1",
                    "source_bundle",
                    "$ROOT/bundle.json",
                    "33061721b137de4f311a208dc4d25ea707c1524dc19e6e87a4a21216b0841ad4",
                    payload='{"messages":[],"person_id":"parent-1"}',
                ),
                self._artifact(
                    "synthetic:synth-jordan",
                    "synthetic",
                    "$ROOT/synthetic.json",
                    "6329c3638e1ce6c7fad1616d15bd1c0d38a1a0315d1d6d2dcc16d2e40714e932",
                    candidate="synth-jordan",
                ),
            ],
            "candidate_people": [
                {"row_key": "candidate:email:jordan", "person_id": "person-a", "parent_id": "parent-1"},
                {"row_key": "candidate:email:jordan", "person_id": "person-b", "parent_id": "parent-1"},
                {"row_key": "synth-jordan", "person_id": "person-b", "parent_id": "parent-1"},
            ],
            "parents": [
                {
                    "parent_id": "parent-1",
                    "public_identifier": "parent-worth:parent-1",
                    "display_name": "Jordan Bravo",
                    "display_slug": None,
                    "machine_worth": None,
                    "machine_worth_reason": None,
                    "human_worth": None,
                    "human_worth_note": None,
                    "human_worth_source": None,
                    "human_worth_at": None,
                    "source": None,
                    "updated_at": self.NOW,
                }
            ],
            "people": [
                {
                    "person_id": "person-a",
                    "parent_id": "parent-1",
                    "child_slug": None,
                    "parent_slug": None,
                    "display_name": "Jordan Bravo",
                    "is_owner": 0,
                    "is_ghost": 0,
                    "facts_json": None,
                    "confidence": None,
                    "updated_at": self.NOW,
                },
                {
                    "person_id": "person-b",
                    "parent_id": "parent-1",
                    "child_slug": None,
                    "parent_slug": None,
                    "display_name": "Jordan B.",
                    "is_owner": 0,
                    "is_ghost": 0,
                    "facts_json": None,
                    "confidence": None,
                    "updated_at": self.NOW,
                },
            ],
            "facts": [
                {
                    "subject_key": "parent-1",
                    "parent_id": "parent-1",
                    "person_id": None,
                    "artifact_key": "facts:parent-1",
                    "machine_worth": "yes",
                    "machine_worth_reason": "Known collaborator",
                    "confidence": 0.0,
                    "is_owner": 0,
                    "facts_json": ('{"network_worth":{"decision":"yes","reason":"Known collaborator"}}'),
                    "projected_at": self.NOW,
                }
            ],
            "links": [
                self._link(
                    row_key="attached-jordan",
                    public_identifier="attached-jordan",
                    kind="pub",
                    linkedin_url="https://www.linkedin.com/in/attached-jordan",
                    display_name="Jordan Bravo",
                    source="deep-context-reconcile",
                ),
                self._link(
                    row_key="candidate:email:jordan",
                    public_identifier="candidate:email:jordan",
                    kind="research",
                    display_name="Jordan Bravo",
                    paid_profile=1,
                    source="deep-research",
                ),
                self._link(
                    row_key="synth-jordan",
                    public_identifier="synth-jordan",
                    kind="synthetic",
                    display_name="Jordan Synth Updated",
                    machine_action="verify",
                    machine_approved="auto",
                    source="deep-research",
                ),
            ],
            "research": [
                {
                    "handle": "subject",
                    "parent_id": "parent-1",
                    "status": "complete",
                    "candidate_key": "candidate:email:jordan",
                    "artifact_key": "research:subject",
                    "result_json": research_payload_json,
                    "updated_at": self.NOW,
                }
            ],
            "synthetic_profiles": [
                {
                    "public_identifier": "synth-jordan",
                    "candidate_key": "synth-jordan",
                    "profile_json": changed_synthetic_bytes.decode().strip(),
                    "source_artifact_key": "synthetic:synth-jordan",
                    "linkedin_url": None,
                    "name": "Jordan Synth Updated",
                    "updated_at": self.NOW,
                }
            ],
        }
        self.assertEqual(state, expected)


class LegacyProjectorTest(unittest.TestCase):
    def test_legacy_research_is_converted_to_provider_envelope_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            research_dir = root / "research"
            result_dir = research_dir / "jordan"
            result_dir.mkdir(parents=True)
            (root / "index.json").write_text(json.dumps({
                "slugs": {"jordan": {"person_id": "person-jordan"}},
                "parents": {
                    "jordan": {
                        "parent_id": "parent-jordan",
                        "name": "Jordan Bravo",
                        "children": ["jordan"],
                    },
                },
            }))
            (result_dir / "01_research_parallel.json").write_text(json.dumps({
                "person": {"full_name": "Jordan Bravo"},
                "positions": [{"title": "Founder", "company_name": "Bravo Robotics"}],
                "education": [],
                "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo"},
                "metadata": {"research_notes": "matched employer"},
            }))
            db = Db(root / "canonical.sqlite")

            import_legacy(
                db,
                review_csv=root / "missing-review.csv",
                index_json=root / "index.json",
                research_dir=research_dir,
            )

            payload = json.loads(query(db, "SELECT result_json FROM research")[0][0])
            self.assertEqual(payload["type"], "json")
            self.assertEqual(payload["content"]["work_experience"][0]["title"], "Founder")
            self.assertEqual(ResearchResult.from_payload(payload).person.full_name, "Jordan Bravo")

    def test_metadata_only_stale_review_row_does_not_create_parent_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "review.csv"
            stale = {column: "" for column in LEGACY_REVIEW_COLUMNS}
            stale.update(
                {
                    "public_identifier": "stale-profile",
                    "source": WriterSource.LEGACY_MIGRATION.value,
                    "updated_at": "2026-08-05T01:00:00Z",
                }
            )
            actionable = {column: "" for column in LEGACY_REVIEW_COLUMNS}
            actionable.update(
                {
                    "public_identifier": "reviewed-profile",
                    "action": "detach",
                    "approved": "yes",
                    # This row's "source" is read twice by import_legacy: as
                    # links.source (WriterSource) and, because approved is
                    # yes/no with an action, as the human decision_source
                    # (ReviewSource). "deep-context-review" is a valid member
                    # of both enums, unlike WriterSource.LEGACY_MIGRATION.
                    "source": WriterSource.REVIEW.value,
                    "updated_at": "2026-08-05T02:00:00Z",
                }
            )
            write_override_rows(
                review,
                LEGACY_REVIEW_COLUMNS,
                {"reviewed-profile": actionable, "stale-profile": stale},
            )
            db = Db(root / "canonical.sqlite")

            import_legacy(db, review_csv=review)

            self.assertEqual(query(db, "SELECT count(*) FROM parents")[0][0], 1)
            self.assertEqual(
                [row[0] for row in query(db, "SELECT row_key FROM links")],
                ["reviewed-profile"],
            )

    def test_direct_enveloped_alias_owner_and_latest_child_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts_dir = root / "facts"
            facts_dir.mkdir()
            pub = "jordan-bravo"
            durable = generate_person_id(pub)
            retired = legacy_message_linkedin_id(pub)
            self.assertTrue(retired.startswith(MESSAGE_LINKEDIN_PREFIX))
            parent_id = "parent-jordan"
            (root / "index.json").write_text(
                json.dumps(
                    {
                        "slugs": {"jordan": {"person_id": durable}},
                        "parents": {"jordan": {"parent_id": parent_id, "name": "Jordan Bravo", "children": ["jordan"]}},
                        "by_email": {"jordan@example.com": ["jordan"]},
                        "by_phone": {"+15550100": ["jordan"]},
                    }
                ),
                encoding="utf-8",
            )
            (facts_dir / f"{retired}.jsonl").write_text(
                json.dumps({"canonical_name": "Jordan Bravo", "network_worth": {"decision": "no"}})
                + "\n"
                + json.dumps({"facts": {"network_worth": {"decision": "yes", "reason": "Worked together"}}})
                + "\n",
                encoding="utf-8",
            )
            owner = "owner-person"
            (facts_dir / f"{owner}.jsonl").write_text(
                json.dumps(
                    {
                        "canonical_name": "Mailbox Owner",
                        "is_owner": True,
                        "network_worth": {"decision": "yes"},
                    }
                ),
                encoding="utf-8",
            )
            review = root / "review.csv"
            row = {column: "" for column in LEGACY_REVIEW_COLUMNS}
            row.update(
                {
                    "public_identifier": pub,
                    "person_id": durable,
                    "network_worth": "no",
                    "source": WriterSource.LEGACY_MIGRATION.value,
                    "updated_at": "2026-08-05T01:00:00Z",
                }
            )
            write_override_rows(review, LEGACY_REVIEW_COLUMNS, {pub: row})
            db = Db(root / "canonical.sqlite")
            result = import_legacy(db, review_csv=review, index_json=root / "index.json", facts_dir=facts_dir)
            self.assertEqual(len(query(db, "PRAGMA foreign_key_check")), 0)
            self.assertEqual(query(db, "SELECT parent_id FROM people WHERE person_id=?", (retired,))[0][0], parent_id)
            parent = query(db, "SELECT * FROM parents WHERE parent_id=?", (parent_id,))[0]
            self.assertEqual((parent["machine_worth"], parent["human_worth"]), ("yes", "no"))
            self.assertEqual(result["facts"], 2)
            self.assertEqual(
                query(db, "SELECT count(*) FROM person_identifiers WHERE person_id=?", (durable,))[0][0], 2
            )
            with self.assertRaises(LegacyImportError):
                import_legacy(db, review_csv=review, index_json=root / "index.json", facts_dir=facts_dir)

    def test_sources_unresolved_membership_and_proposed_avatar_are_absorbed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            facts_dir = root / "facts"
            avatar_dir = root / "avatars"
            facts_dir.mkdir()
            avatar_dir.mkdir()
            pub = "jordan-bravo"
            proposed_pub = "jordan-bravo-new"
            person_id = "candidate:email:jordan@example.test"
            parent_id = "parent-jordan"
            (root / "index.json").write_text(
                json.dumps(
                    {
                        "slugs": {"jordan": {"person_id": person_id}},
                        "parents": {"jordan": {"parent_id": parent_id, "name": "Jordan Bravo", "children": ["jordan"]}},
                    }
                ),
                encoding="utf-8",
            )
            (facts_dir / f"{person_id}.jsonl").write_text(
                json.dumps(
                    {
                        "canonical_name": "Jordan Bravo",
                        "network_worth": {"decision": "yes", "reason": "Known collaborator"},
                    }
                ),
                encoding="utf-8",
            )
            merged = root / "people.csv"
            merged.write_text(
                f'id,source_channels\n{person_id},"gmail_msgvault,linkedin_csv"\n',
                encoding="utf-8",
            )
            review = root / "review.csv"
            row = {column: "" for column in LEGACY_REVIEW_COLUMNS}
            row.update(
                {
                    "public_identifier": pub,
                    "person_id": person_id,
                    "action": "retarget",
                    "approved": "auto",
                    "new_linkedin_url": f"https://www.linkedin.com/in/{proposed_pub}",
                    "new_public_identifier": proposed_pub,
                    "source": WriterSource.LEGACY_MIGRATION.value,
                }
            )
            write_override_rows(review, LEGACY_REVIEW_COLUMNS, {pub: row})
            avatar = avatar_dir / (hashlib.sha256(proposed_pub.encode()).hexdigest()[:24] + ".image")
            avatar.write_bytes(b"synthetic-image")

            db = Db(root / "canonical.sqlite")
            import_legacy(
                db,
                review_csv=review,
                index_json=root / "index.json",
                facts_dir=facts_dir,
                avatar_dir=avatar_dir,
            )
            project_imported_people(db, read_imported_people(merged))

            link = query(db, "SELECT * FROM links WHERE row_key=?", (pub,))[0]
            self.assertEqual(link["parent_id"], parent_id)
            self.assertEqual(link["machine_proposed_public_identifier"], proposed_pub)
            self.assertEqual(
                query(db, "SELECT parent_id FROM candidate_people WHERE row_key=?", (pub,))[0][0], parent_id
            )
            self.assertEqual(
                [item["source"] for item in query(db, "SELECT source FROM person_sources ORDER BY source")],
                ["gmail_msgvault", "linkedin_csv"],
            )
            projected_avatar = next(row for row in canonical_snapshot(db).artifacts if row.kind == "avatar")
            self.assertEqual(projected_avatar.path, str(avatar.resolve()))
            self.assertEqual(len(query(db, "PRAGMA foreign_key_check")), 0)

    def test_real_mirror_worth_and_foreign_keys_when_present(self) -> None:
        root = Path("/Users/arthur/workspace/powerpacks-jake-mirror/.powerpacks")
        if not root.exists():
            self.skipTest("diagnostic mirror is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            dc = root / "deep-context"
            review = root / "network-import/overrides/review.csv"
            db = Db(Path(tmp) / "canonical.sqlite")
            import_legacy(
                db,
                review_csv=review,
                synthetic_csv=review.parent / "synthetic-people.csv",
                index_json=dc / "index.json",
                facts_dir=dc / "facts",
                verdicts_jsonl=dc / "reconcile/verdicts.jsonl",
                research_dir=dc / "reconcile/deep-research",
                avatar_dir=dc / "review/avatars",
            )
            project_imported_people(
                db,
                read_imported_people(root / "network-import/merged/people.csv"),
            )
            self.assertEqual(len(query(db, "PRAGMA foreign_key_check")), 0)
            self.assertEqual(
                asdict(worth_counts(db)),
                {
                    "total": 5379,
                    "pending": 61,
                    "yes": 4169,
                    "no": 1149,
                },
            )
            self.assertEqual(
                asdict(linkedin_progress(db)),
                {
                    "total": 756,
                    # Forty-four families held several legacy machine winners.
                    # Canonical sibling arbitration reopens those conflicts
                    # instead of treating an absent llm_reject flag as approval.
                    "pending": 235,
                    "done": 521,
                },
            )


if __name__ == "__main__":
    unittest.main()
