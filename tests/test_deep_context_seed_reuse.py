"""Carried facts remain reusable until their carried evidence changes."""

from __future__ import annotations

import json

from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact, project_parent_source_bundle
from packs.ingestion.primitives.deep_context.synthesis import selection
from tests.test_deep_context_seed import SeedFixture
from deep_context_sqlite_test_helpers import message_payload


class SeedReuseTests(SeedFixture):
    def test_carried_facts_skip_until_messages_change(self) -> None:
        path = self.legacy / "deep-context/raw/person-jordan.json"
        payload = json.loads(path.read_text())
        payload["messages"] = [message_payload("Original evidence"), message_payload("More evidence")]
        path.write_text(json.dumps(payload))
        db = self.cold_store()
        self.seed(db)
        parent_id = next(row.parent_id for row in queries.people(db) if row.person_id == "person-jordan")

        def pending(*, force=False, model_changed=False):
            return [bundle.person_id for bundle in selection.pending_target_bundles(
                db, system_prompt="Synthetic owner context", chunk_chars=9000,
                max_batches=20, force=force, model_changed=model_changed,
            )]

        self.assertEqual(pending(), [])
        self.assertEqual(pending(model_changed=True), [])
        self.assertEqual(pending(force=True), [parent_id])
        carried = self.deep_context / f"raw/{parent_id}.json"
        payload = json.loads(carried.read_text())
        payload["collected_at"] = "2026-09-26T00:00:00Z"
        payload["messages"].reverse()
        carried.write_text(json.dumps(payload))
        project_parent_source_bundle(db, carried, parent_id)
        self.assertEqual(pending(), [])
        payload["messages"].append(message_payload("New evidence"))
        carried.write_text(json.dumps(payload))
        project_parent_source_bundle(db, carried, parent_id)
        self.assertEqual(pending(), [parent_id])

    def test_seed_preserves_fact_history_version_and_reprojection(self) -> None:
        path = self.legacy / "deep-context/facts/person-jordan.jsonl"
        original = json.loads(path.read_text())
        original["synthesis_version"] = "legacy-version"
        path.write_text(json.dumps(original) + "\n" + json.dumps(original) + "\n")
        db = self.cold_store()
        self.seed(db)
        parent_id = next(row.parent_id for row in queries.people(db) if row.person_id == "person-jordan")
        target = self.deep_context / f"facts/{parent_id}.jsonl"
        records = [json.loads(line) for line in target.read_text().splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], original)
        fingerprint = records[-1].pop("input_evidence_fingerprint")
        self.assertTrue(fingerprint.startswith("seed:"))
        self.assertEqual(records[-1], original)
        project_parent_fact(db, target, parent_id)
        self.assertEqual(selection.pending_target_bundles(
            db, system_prompt="Changed prompt", chunk_chars=1, max_batches=1, force=False,
        ), [])

    def test_facts_without_bundles_do_not_get_a_seed_fingerprint(self) -> None:
        db = self.cold_store()
        self.seed(db)
        parent_id = next(row.parent_id for row in queries.people(db) if row.person_id == "candidate:phone:+15550100")
        artifact = queries.artifacts(db, kind="facts", parent_id=parent_id)[0]
        self.assertFalse(artifact.input_fingerprint)
