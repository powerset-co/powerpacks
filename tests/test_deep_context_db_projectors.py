from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from parallel.types import TaskRunJsonOutput

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
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
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
from deep_context_sqlite_test_helpers import query


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
        research_payload_json = research_bytes.decode()
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
                    # The provider slug is the identifier — the queue row no
                    # longer carries a source-side one (production never did).
                    public_identifier="jordan-bravo",
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


if __name__ == "__main__":
    unittest.main()
