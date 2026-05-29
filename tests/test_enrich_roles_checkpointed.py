import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.indexing.primitives.enrich_roles_checkpointed import enrich_roles_checkpointed as stage


class EnrichRolesCheckpointedTests(unittest.TestCase):
    def _write_flattened(self, path: Path, count: int = 3) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        titles = ["Founder", "Staff Software Engineer", "Product Manager"]
        with path.open("w", encoding="utf-8") as handle:
            for idx in range(count):
                handle.write(json.dumps({"id": f"person-{idx}", "headline": "Canary", "summary": "Builds software", "work_experiences": [{"id": f"pos-{idx}", "title_hash": f"upstream-title-hash-{idx}", "title": titles[idx], "company_name": f"Company {idx}", "description": f"Role description {idx}"}]}) + "\n")

    def _args(self, flattened: Path, out: Path, **kwargs):
        defaults = dict(flattened=str(flattened), output_dir=str(out), checkpoint_every=1, provider="openai", input_classifications=None, api_key="test", base_url="https://example.invalid/v1", model="gpt-test", allow_paid=True, dry_run=False, force=True, stop_after_chunks=None)
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_openai_provider_checkpoint_resume_with_monkeypatched_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flattened = root / "flattened_people.jsonl"
            self._write_flattened(flattened, count=3)

            def fake_openai(role, **_kwargs):
                return {"role_ids": ["software_engineer"], "seniority_band": "senior-ic", "role_track": "engineering", "role_type": "engineering", "cluster": "engineering", "doc2query": [role["raw_title"] + " search"], "inferred_skills": ["software engineering"]}

            def fake_openai_batch(roles, **_kwargs):
                return [fake_openai(role) for role in roles]

            with mock.patch.object(stage, "call_openai_role_enrichments", side_effect=fake_openai_batch):
                partial = stage.run(self._args(flattened, root / "roles", stop_after_chunks=1))
                self.assertEqual(partial["status"], "partial")
                final = stage.run(self._args(flattened, root / "roles", force=False, stop_after_chunks=None))

            self.assertEqual(final["status"], "completed")
            rows = [json.loads(line) for line in (root / "roles/roles_with_dense_text_remapped.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row["role_ids"] for row in rows))
            self.assertTrue((root / "roles/chunks/roles.000001.jsonl").exists())
            self.assertTrue((root / "roles/chunks/roles.000003.jsonl").exists())

    def test_input_classifications_replay_without_paid_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flattened = root / "flattened_people.jsonl"
            self._write_flattened(flattened, count=1)
            base = stage.collect_role_inputs(flattened)[0]
            input_file = root / "roles_input.jsonl"
            input_file.write_text(json.dumps({**base, "role_ids": ["founder"], "seniority_band": "owner", "role_track": "founder", "role_type": "founder", "cluster": "founder", "doc2query": ["founder search"], "inferred_skills": ["fundraising"]}) + "\n")
            result = stage.run(self._args(flattened, root / "roles", input_classifications=str(input_file), api_key=None, allow_paid=False))
            self.assertEqual(result["status"], "completed")
            row = json.loads((root / "roles/roles_with_dense_text_remapped.jsonl").read_text().strip())
            self.assertEqual(row["role_ids"], ["founder"])

    def test_input_classifications_can_pay_for_missing_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flattened = root / "flattened_people.jsonl"
            self._write_flattened(flattened, count=2)
            base = stage.collect_role_inputs(flattened)[0]
            input_file = root / "roles_input.jsonl"
            input_file.write_text(json.dumps({**base, "role_ids": ["founder"], "seniority_band": "owner", "role_track": "founder", "role_type": "founder", "cluster": "founder", "doc2query": ["founder search"], "inferred_skills": ["fundraising"]}) + "\n")

            def fake_openai(role, **_kwargs):
                return {"role_ids": ["software_engineer"], "seniority_band": "senior-ic", "role_track": "engineering", "role_type": "engineering", "cluster": "engineering", "doc2query": [role["raw_title"]], "inferred_skills": ["software engineering"]}

            def fake_openai_batch(roles, **_kwargs):
                return [fake_openai(role) for role in roles]

            with mock.patch.object(stage, "call_openai_role_enrichments", side_effect=fake_openai_batch) as mocked:
                result = stage.run(self._args(flattened, root / "roles", input_classifications=str(input_file), allow_paid=True))
            self.assertEqual(result["status"], "completed")
            self.assertEqual(mocked.call_count, 1)
            counts = result["counts"]
            self.assertEqual(counts["artifact_hits"], 1)
            self.assertEqual(counts["artifact_misses"], 1)
            self.assertEqual(counts["paid_calls"], 1)

    def test_openai_provider_is_blocked_without_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flattened = root / "flattened_people.jsonl"
            self._write_flattened(flattened, count=1)
            with self.assertRaises(SystemExit):
                stage.run(self._args(flattened, root / "roles", allow_paid=False, api_key=None))


if __name__ == "__main__":
    unittest.main()
