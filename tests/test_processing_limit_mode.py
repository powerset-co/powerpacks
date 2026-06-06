import tempfile
import time
import unittest
from pathlib import Path

from packs.indexing.lib.io import read_jsonl, write_json, write_jsonl
from packs.indexing.primitives.build_processing_pipeline.build_processing_pipeline import (
    compute_record_diff,
    compute_record_hash,
    commit_processed_person_hashes,
    estimate_run,
    flatten_people,
    paths,
    primary_person_key,
    save_hashes,
    step_flatten,
    write_record_jsonl_with_hashes,
    reset_processing_checkpoints,
    upsert_people_jsonl,
)


class ProcessingLimitModeTest(unittest.TestCase):
    def test_flatten_output_upserts_and_preserves_stale_rows_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "unified/flattened_people.jsonl"
            hashes = Path(tmp) / "unified/person_hashes.json"
            write_jsonl(output, [
                {"id": "existing-1", "linkedin_url": "https://linkedin.com/in/old", "name": "Old"},
                {"id": "keep-1", "linkedin_url": "https://linkedin.com/in/keep", "name": "Keep"},
            ])

            stats = upsert_people_jsonl(output, [
                {"id": "existing-1", "linkedin_url": "https://linkedin.com/in/old", "name": "Updated"},
                {"id": "new-1", "linkedin_url": "https://linkedin.com/in/new", "name": "New"},
            ], hashes)
            rows = read_jsonl(output)

        self.assertEqual(stats["existing_rows"], 2)
        self.assertEqual(stats["inserted_rows"], 1)
        self.assertEqual(stats["updated_rows"], 1)
        self.assertEqual(stats["unchanged_rows"], 0)
        self.assertEqual(stats["deleted_rows"], 0)
        self.assertTrue(stats["hashes_written"])
        self.assertEqual([row["id"] for row in rows], ["existing-1", "keep-1", "new-1"])
        self.assertEqual(rows[0]["name"], "Updated")

    def test_flatten_output_hash_diff_tracks_new_changed_unchanged_and_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "unified/flattened_people.jsonl"
            hashes = Path(tmp) / "unified/person_hashes.json"
            initial = [
                {"id": "changed-1", "name": "Old"},
                {"id": "same-1", "name": "Same"},
                {"id": "deleted-1", "name": "Deleted"},
            ]
            stats = upsert_people_jsonl(output, initial, hashes, prune_stale=True)
            self.assertEqual(stats["inserted_rows"], 3)

            second = [
                {"id": "changed-1", "name": "New"},
                {"id": "same-1", "name": "Same"},
                {"id": "new-1", "name": "New Person"},
            ]
            stats = upsert_people_jsonl(output, second, hashes, prune_stale=True)
            rows = read_jsonl(output)

        self.assertEqual(stats["inserted_rows"], 1)
        self.assertEqual(stats["updated_rows"], 1)
        self.assertEqual(stats["unchanged_rows"], 1)
        self.assertEqual(stats["deleted_rows"], 1)
        self.assertEqual([row["id"] for row in rows], ["changed-1", "same-1", "new-1"])

    def test_record_diff_hashes_vectors_and_reports_deleted_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hashes = Path(tmp) / "records/people.records.hashes.json"
            old_records = [
                {"id": "same", "name": "Same", "vector": [0.1, 0.2]},
                {"id": "changed", "name": "Old", "vector": [0.3]},
                {"id": "deleted", "name": "Deleted"},
            ]
            save_hashes({row["id"]: compute_record_hash(row) for row in old_records}, hashes)
            diff = compute_record_diff([
                {"id": "same", "name": "Same", "vector": [0.1, 0.2]},
                {"id": "changed", "name": "New", "vector": [0.3]},
                {"id": "new", "name": "New"},
            ], hashes)

        self.assertEqual([row["id"] for row in diff["new"]], ["new"])
        self.assertEqual([row["id"] for row in diff["changed"]], ["changed"])
        self.assertEqual(diff["unchanged_count"], 1)
        self.assertEqual(diff["deleted_ids"], ["deleted"])
        self.assertEqual(diff["skipped_unkeyed_rows"], 0)

    def test_company_record_diff_uses_company_urn_and_reports_unkeyed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hashes = Path(tmp) / "records/companies.records.hashes.json"
            old_records = [
                {"company_urn": "company:same", "company_name": "Same"},
                {"company_urn": "company:changed", "company_name": "Old"},
                {"company_urn": "company:deleted", "company_name": "Deleted"},
            ]
            save_hashes({row["company_urn"]: compute_record_hash(row) for row in old_records}, hashes)
            diff = compute_record_diff([
                {"company_urn": "company:same", "company_name": "Same"},
                {"company_urn": "company:changed", "company_name": "New"},
                {"company_urn": "company:new", "company_name": "New"},
                {"company_name": "No Key"},
            ], hashes, id_fields=("id", "company_urn"))

        self.assertEqual([row["company_urn"] for row in diff["new"]], ["company:new"])
        self.assertEqual([row["company_urn"] for row in diff["changed"]], ["company:changed"])
        self.assertEqual(diff["unchanged_count"], 1)
        self.assertEqual(diff["deleted_ids"], ["company:deleted"])
        self.assertEqual(diff["skipped_unkeyed_rows"], 1)

    def test_unkeyed_record_rows_force_content_check_and_show_in_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records/companies.records.jsonl"
            hashes = Path(tmp) / "records/companies.records.hashes.json"
            rows = [{"company_name": "No Key"}]
            write_jsonl(records, rows)
            first_mtime = records.stat().st_mtime_ns
            time.sleep(0.01)

            stats = write_record_jsonl_with_hashes(records, rows, hashes, id_fields=("id", "company_urn"))
            same_mtime = records.stat().st_mtime_ns
            time.sleep(0.01)
            changed = write_record_jsonl_with_hashes(records, [{"company_name": "No Key Changed"}], hashes, id_fields=("id", "company_urn"))

        self.assertEqual(same_mtime, first_mtime)
        self.assertFalse(stats["file_written"])
        self.assertEqual(stats["skipped_unkeyed_rows"], 1)
        self.assertEqual(stats["hashes"], 0)
        self.assertTrue(changed["file_written"])

    def test_processing_dry_run_marks_changed_hashed_people_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_csv = root / "people.csv"
            input_csv.write_text(
                "id,public_identifier,linkedin_url,full_name,work_experiences\n"
                'person-1,p1,https://www.linkedin.com/in/p1,Person One,"[]"\n',
                encoding="utf-8",
            )
            output = root / "pipeline"
            current = flatten_people(input_csv)[0]
            old = {**current, "full_name": "Old"}
            write_jsonl(output / "records/summaries.records.jsonl", [{"id": current["id"], "person_id": current["id"], "vector": [0.1]}])
            save_hashes({primary_person_key(old): compute_record_hash(old)}, output / "unified/person_hashes.json")
            write_json(output / "ledger.json", {"status": "completed"})

            class Args:
                input = str(input_csv)
                output_dir = str(output)
                default_operator_id = "operator:test"
                checkpoint_every = 1000
                dry_run = True

            payload = estimate_run(Args())

        self.assertEqual(payload["counts"]["processed_people"], 0)
        self.assertEqual(payload["counts"]["pending_people"], 1)

    def test_processing_dry_run_does_not_trust_hash_sidecar_until_ledger_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_csv = root / "people.csv"
            input_csv.write_text(
                "id,public_identifier,linkedin_url,full_name,work_experiences\n"
                'person-1,p1,https://www.linkedin.com/in/p1,Person One,"[]"\n',
                encoding="utf-8",
            )
            output = root / "pipeline"
            current = flatten_people(input_csv)[0]
            write_jsonl(output / "records/summaries.records.jsonl", [{"id": current["id"], "person_id": current["id"], "vector": [0.1]}])
            save_hashes({primary_person_key(current): compute_record_hash(current)}, output / "unified/person_hashes.json")
            write_json(output / "ledger.json", {"status": "partial"})

            class Args:
                input = str(input_csv)
                output_dir = str(output)
                default_operator_id = "operator:test"
                checkpoint_every = 1000
                dry_run = True

            payload = estimate_run(Args())

        self.assertEqual(payload["counts"]["processed_people"], 0)
        self.assertEqual(payload["counts"]["pending_people"], 1)

    def test_processing_dry_run_does_not_trust_hash_sidecar_without_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_csv = root / "people.csv"
            input_csv.write_text(
                "id,public_identifier,linkedin_url,full_name,work_experiences\n"
                'person-1,p1,https://www.linkedin.com/in/p1,Person One,"[]"\n',
                encoding="utf-8",
            )
            output = root / "pipeline"
            current = flatten_people(input_csv)[0]
            write_jsonl(output / "records/summaries.records.jsonl", [{"id": current["id"], "person_id": current["id"], "vector": [0.1]}])
            save_hashes({primary_person_key(current): compute_record_hash(current)}, output / "unified/person_hashes.json")

            class Args:
                input = str(input_csv)
                output_dir = str(output)
                default_operator_id = "operator:test"
                checkpoint_every = 1000
                dry_run = True

            payload = estimate_run(Args())

        self.assertEqual(payload["counts"]["processed_people"], 0)
        self.assertEqual(payload["counts"]["pending_people"], 1)

    def test_processing_dry_run_adopts_restored_outputs_without_hash_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_csv = root / "people.csv"
            input_csv.write_text(
                "id,public_identifier,linkedin_url,full_name,work_experiences\n"
                'person-1,p1,https://www.linkedin.com/in/p1,Person One,"[]"\n',
                encoding="utf-8",
            )
            output = root / "pipeline"
            current = flatten_people(input_csv)[0]
            write_jsonl(output / "records/summaries.records.jsonl", [{"id": current["id"], "person_id": current["id"], "vector": [0.1]}])

            class Args:
                input = str(input_csv)
                output_dir = str(output)
                default_operator_id = "operator:test"
                checkpoint_every = 1000
                dry_run = True

            payload = estimate_run(Args())

        self.assertEqual(payload["counts"]["processed_people"], 1)
        self.assertEqual(payload["counts"]["pending_people"], 0)

    def test_processing_dry_run_does_not_adopt_missing_hashes_with_incomplete_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_csv = root / "people.csv"
            input_csv.write_text(
                "id,public_identifier,linkedin_url,full_name,work_experiences\n"
                'person-1,p1,https://www.linkedin.com/in/p1,Person One,"[]"\n',
                encoding="utf-8",
            )
            output = root / "pipeline"
            current = flatten_people(input_csv)[0]
            write_jsonl(output / "records/summaries.records.jsonl", [{"id": current["id"], "person_id": current["id"], "vector": [0.1]}])
            write_json(output / "ledger.json", {"status": "partial"})

            class Args:
                input = str(input_csv)
                output_dir = str(output)
                default_operator_id = "operator:test"
                checkpoint_every = 1000
                dry_run = True

            payload = estimate_run(Args())

        self.assertEqual(payload["counts"]["processed_people"], 0)
        self.assertEqual(payload["counts"]["pending_people"], 1)

    def test_record_hash_adoption_does_not_rewrite_existing_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records/people.records.jsonl"
            hashes = Path(tmp) / "records/people.records.hashes.json"
            rows = [{"id": "person-1", "name": "Person One", "vector": [0.1, 0.2]}]
            write_jsonl(records, rows)
            first_mtime = records.stat().st_mtime_ns
            time.sleep(0.01)

            adoption = write_record_jsonl_with_hashes(records, rows, hashes)
            adopted_mtime = records.stat().st_mtime_ns
            time.sleep(0.01)
            unchanged = write_record_jsonl_with_hashes(records, rows, hashes)

        self.assertEqual(adopted_mtime, first_mtime)
        self.assertFalse(adoption["file_written"])
        self.assertTrue(adoption["hashes_written"])
        self.assertFalse(unchanged["file_written"])
        self.assertFalse(unchanged["hashes_written"])
        self.assertEqual(unchanged["unchanged_rows"], 1)

    def test_step_flatten_prunes_canonical_input_without_writing_authoritative_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_csv = root / "people.csv"
            input_csv.write_text(
                "id,public_identifier,linkedin_url,full_name,work_experiences\n"
                'person-1,p1,https://www.linkedin.com/in/p1,Person One,"[]"\n',
                encoding="utf-8",
            )
            output = root / "pipeline"
            ps = paths(output)
            write_jsonl(ps["flattened"], [{"id": "stale-1", "name": "Stale"}])
            expected_id = flatten_people(input_csv)[0]["id"]

            artifacts, stats = step_flatten({"input": str(input_csv), "run_dir": str(output)}, ps)
            rows = read_jsonl(ps["flattened"])

        self.assertEqual(artifacts["flattened_people"], str(ps["flattened"]))
        self.assertEqual(stats["upsert"]["deleted_rows"], 1)
        self.assertEqual([row["id"] for row in rows], [expected_id])
        self.assertFalse(ps["person_hashes"].exists())

    def test_processed_person_hashes_commit_after_success_makes_hash_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_csv = root / "people.csv"
            input_csv.write_text(
                "id,public_identifier,linkedin_url,full_name,work_experiences\n"
                'person-1,p1,https://www.linkedin.com/in/p1,Person One,"[]"\n',
                encoding="utf-8",
            )
            output = root / "pipeline"
            ps = paths(output)
            current = flatten_people(input_csv)[0]
            write_jsonl(ps["flattened"], [current])
            write_jsonl(output / "records/summaries.records.jsonl", [{"id": current["id"], "person_id": current["id"], "vector": [0.1]}])
            write_json(output / "ledger.json", {"status": "completed"})

            stats = commit_processed_person_hashes({"run_dir": str(output)}, ps)

            class Args:
                input = str(input_csv)
                output_dir = str(output)
                default_operator_id = "operator:test"
                checkpoint_every = 1000
                dry_run = True

            payload = estimate_run(Args())

        self.assertTrue(stats["hashes_written"])
        self.assertEqual(payload["counts"]["processed_people"], 1)
        self.assertEqual(payload["counts"]["pending_people"], 0)

    def test_checkpoint_reset_preserves_stage_outputs_as_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keep_files = [
                root / "roles/roles_with_dense_text_remapped.jsonl",
                root / "roles/roles_with_embeddings.jsonl",
                root / "company/companies_corpus_v3.jsonl",
                root / "company/company_embeddings_v3.jsonl",
                root / "unified/summary_embeddings.jsonl",
            ]
            for path in keep_files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            remove_files = [
                root / "roles/checkpoint.json",
                root / "roles/manifest.json",
                root / "roles/chunks/roles.000001.jsonl",
                root / "roles/embedding_checkpoints/checkpoint.json",
                root / "company/enrichment_checkpoints/checkpoint.json",
                root / "summaries/embedding_checkpoints/checkpoint.json",
                root / "vectors/checkpoint.json",
            ]
            for path in remove_files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")

            reset_processing_checkpoints(root)

            for path in keep_files:
                self.assertTrue(path.exists(), path)
            for path in remove_files:
                self.assertFalse(path.exists(), path)


if __name__ == "__main__":
    unittest.main()
