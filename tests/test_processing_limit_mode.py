import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from packs.indexing.lib.artifact_io import write_parquet_rows
from packs.indexing.lib.io import read_json, read_jsonl, write_json, write_jsonl
from packs.indexing.primitives.build_processing_pipeline import build_processing_pipeline as pipeline
from packs.indexing.primitives.embed_records_checkpointed import embed_records_checkpointed
from packs.indexing.primitives.enrich_companies_checkpointed import enrich_companies_checkpointed
from packs.indexing.primitives.enrich_roles_checkpointed import enrich_roles_checkpointed
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
    write_record_parquet_with_hashes,
    reset_processing_checkpoints,
    upsert_people_jsonl,
)


class ProcessingLimitModeTest(unittest.TestCase):
    def company_artifact_row(self, name: str = "Acme") -> dict:
        return {
            "company_urn": "company:acme",
            "company_name": name,
            "original_name": name,
            "name_aliases": [name],
            "description": "AI tools",
            "entity_types": ["venture_backed_startup"],
            "sector_types": ["ai_ml"],
            "technology_types": ["agents"],
            "customer_type": "Business (B2B)",
            "funding_stage": "SEED",
            "company_type": "STARTUP",
            "ownership_status": "PRIVATE",
            "stage": "early",
            "accelerators": [],
            "yc_batches": [],
            "confidence_score": 0.9,
            "doc2query": [f"{name} AI company"],
            "d2q_text": f"{name} AI company",
            "word_text": "AI tools",
            "semantic_text": f"{name} builds AI tools",
        }

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
            records = Path(tmp) / "records/companies.records.parquet"
            hashes = Path(tmp) / "records/companies.records.hashes.json"
            rows = [{"company_name": "No Key"}]
            write_parquet_rows(records, rows, schema={"id": "VARCHAR"})
            first_mtime = records.stat().st_mtime_ns
            time.sleep(0.01)

            stats = write_record_parquet_with_hashes(records, rows, hashes, id_fields=("id", "company_urn"))
            same_mtime = records.stat().st_mtime_ns
            time.sleep(0.01)
            changed = write_record_parquet_with_hashes(records, [{"company_name": "No Key Changed"}], hashes, id_fields=("id", "company_urn"))

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
            write_parquet_rows(output / "records/summaries.records.parquet", [{"id": current["id"], "person_id": current["id"], "vector": [0.1]}], float_array_fields=("vector",))
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
            write_parquet_rows(output / "records/summaries.records.parquet", [{"id": current["id"], "person_id": current["id"], "vector": [0.1]}], float_array_fields=("vector",))
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
            write_parquet_rows(output / "records/summaries.records.parquet", [{"id": current["id"], "person_id": current["id"], "vector": [0.1]}], float_array_fields=("vector",))
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
            write_parquet_rows(output / "records/summaries.records.parquet", [{"id": current["id"], "person_id": current["id"], "vector": [0.1]}], float_array_fields=("vector",))

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
            write_parquet_rows(output / "records/summaries.records.parquet", [{"id": current["id"], "person_id": current["id"], "vector": [0.1]}], float_array_fields=("vector",))
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

    def test_record_hash_adoption_does_not_rewrite_existing_parquet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "records/people.records.parquet"
            hashes = Path(tmp) / "records/people.records.hashes.json"
            rows = [{"id": "person-1", "name": "Person One", "vector": [0.1, 0.2]}]
            write_parquet_rows(records, rows, float_array_fields=("vector",))
            first_mtime = records.stat().st_mtime_ns
            time.sleep(0.01)

            adoption = write_record_parquet_with_hashes(records, rows, hashes)
            adopted_mtime = records.stat().st_mtime_ns
            time.sleep(0.01)
            unchanged = write_record_parquet_with_hashes(records, rows, hashes)

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
            write_parquet_rows(output / "records/summaries.records.parquet", [{"id": current["id"], "person_id": current["id"], "vector": [0.1]}], float_array_fields=("vector",))
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
                root / "roles/roles_with_embeddings.parquet",
                root / "company/companies_corpus_v3.jsonl",
                root / "company/company_embeddings_v3.parquet",
                root / "unified/summary_embeddings.parquet",
            ]
            for path in keep_files:
                if path.suffix == ".parquet":
                    write_parquet_rows(path, [{"id": "cached", "embedding": [0.1]}], float_array_fields=("embedding",))
                else:
                    write_jsonl(path, [{"id": "cached"}])
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

    def test_execute_records_per_stage_timing_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.json"
            write_json(ledger_path, {"run_dir": str(root), "status": "pending", "steps": [{"id": "timed_step", "status": "pending"}]})

            old_steps = pipeline.STEPS
            old_functions = pipeline.STEP_FUNCTIONS
            old_commit = pipeline.commit_processed_person_hashes
            try:
                pipeline.STEPS = ["timed_step"]
                pipeline.STEP_FUNCTIONS = {"timed_step": lambda ledger, ps: ({"artifact": "path"}, {"rows": 1})}
                pipeline.commit_processed_person_hashes = lambda ledger, ps: {"hashes_written": False}

                ledger = pipeline.execute(ledger_path)
            finally:
                pipeline.STEPS = old_steps
                pipeline.STEP_FUNCTIONS = old_functions
                pipeline.commit_processed_person_hashes = old_commit

        step = ledger["steps"][0]
        timing = step["stats"]["timing"]
        self.assertEqual(ledger["status"], "completed")
        self.assertEqual(step["status"], "completed")
        self.assertIn("started_at", timing)
        self.assertIn("completed_at", timing)
        self.assertIsInstance(timing["duration_seconds"], float)
        self.assertIn("timed_step", ledger["timings"]["stages"])
        self.assertGreaterEqual(ledger["timings"]["total_duration_seconds"], 0.0)

    def test_company_enrichment_manifest_contains_subtimings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "companies.raw.jsonl"
            output_path = root / "companies_corpus_v3.jsonl"
            artifact_path = root / "artifact.jsonl"
            write_jsonl(input_path, [{"company_urn": "company:acme", "company_name": "Acme"}])
            write_jsonl(artifact_path, [self.company_artifact_row()])

            manifest = enrich_companies_checkpointed.run(SimpleNamespace(
                input=str(input_path),
                output=str(output_path),
                output_dir=str(root / "checkpoints"),
                checkpoint_every=1,
                provider="artifact",
                artifact_path=str(artifact_path),
                artifact_missing_policy="error",
                dry_run=False,
                estimate=False,
                allow_paid=False,
                model=None,
                concurrency=None,
                api_key=None,
                base_url=None,
                force=True,
                stop_after_chunks=None,
            ))

        self.assertEqual(manifest["status"], "completed")
        self.assertIn("timings", manifest)
        self.assertIn("shape_cache_prepare_seconds", manifest["timings"])
        self.assertIn("checkpoint_chunk_write_seconds", manifest["timings"])
        self.assertIn("checkpoint_state_write_seconds", manifest["timings"])
        self.assertIn("finalize_merge_write_seconds", manifest["timings"])
        self.assertIn("merge_normalize_seconds", manifest["timings"])

    def test_embedding_manifest_contains_subtimings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "records.jsonl"
            output_path = root / "embeddings.parquet"
            embeddings_path = root / "input_embeddings.parquet"
            write_jsonl(input_path, [{"id": "one", "text": "hello world"}])
            write_parquet_rows(embeddings_path, [{"id": "one", "embedding": [0.1, 0.2]}], float_array_fields=("embedding",))

            manifest = embed_records_checkpointed.run(SimpleNamespace(
                input=str(input_path),
                output=str(output_path),
                output_dir=str(root / "checkpoints"),
                id_field="id",
                text_fields="text",
                copy_fields="id,text",
                checkpoint_every=1,
                provider="openai",
                input_embeddings=str(embeddings_path),
                input_id_field="id",
                input_embedding_field="embedding",
                allow_paid=False,
                api_key=None,
                base_url=None,
                model=None,
                concurrency=None,
                dimension=2,
                api_batch_size=128,
                dry_run=False,
                force=True,
                stop_after_chunks=None,
            ))

        self.assertEqual(manifest["status"], "completed")
        self.assertIn("timings", manifest)
        self.assertIn("local_input_prepare_seconds", manifest["timings"])
        self.assertIn("artifact_replay_seconds", manifest["timings"])
        self.assertIn("checkpoint_chunk_write_seconds", manifest["timings"])
        self.assertIn("checkpoint_state_write_seconds", manifest["timings"])
        self.assertIn("finalize_merge_write_seconds", manifest["timings"])

    def test_partial_checkpoints_persist_state_write_subtiming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            flattened = root / "flattened_people.jsonl"
            write_jsonl(flattened, [{"id": "person-1", "work_experiences": [{"title_hash": "title:founder", "title": "Founder", "company_name": "Acme"}]}])
            role_base = enrich_roles_checkpointed.collect_role_inputs(flattened)[0]
            role_artifact = root / "role_artifact.jsonl"
            write_jsonl(role_artifact, [{
                **role_base,
                "role_ids": ["founder"],
                "seniority_band": "owner",
                "role_track": "founder",
                "role_type": "founder",
                "cluster": "founder",
                "doc2query": ["founder"],
                "inferred_skills": ["fundraising"],
            }])
            enrich_roles_checkpointed.run(SimpleNamespace(
                flattened=str(flattened),
                output_dir=str(root / "roles"),
                checkpoint_every=1,
                provider="openai",
                input_classifications=str(role_artifact),
                api_key=None,
                base_url=None,
                model=None,
                concurrency=None,
                openai_usage_tier=None,
                allow_paid=False,
                dry_run=False,
                force=True,
                stop_after_chunks=1,
            ))
            role_state = read_json(root / "roles/checkpoint.json")

            company_input = root / "companies.raw.jsonl"
            company_output = root / "companies_corpus_v3.jsonl"
            company_artifact = root / "company_artifact.jsonl"
            write_jsonl(company_input, [{"company_urn": "company:acme", "company_name": "Acme"}])
            write_jsonl(company_artifact, [self.company_artifact_row()])
            enrich_companies_checkpointed.run(SimpleNamespace(
                input=str(company_input),
                output=str(company_output),
                output_dir=str(root / "company_checkpoints"),
                checkpoint_every=1,
                provider="artifact",
                artifact_path=str(company_artifact),
                artifact_missing_policy="error",
                dry_run=False,
                estimate=False,
                allow_paid=False,
                model=None,
                concurrency=None,
                openai_usage_tier=None,
                api_key=None,
                base_url=None,
                force=True,
                stop_after_chunks=1,
            ))
            company_state = read_json(root / "company_checkpoints/checkpoint.json")

            embedding_input = root / "records.jsonl"
            embedding_output = root / "embeddings.parquet"
            embedding_artifact = root / "input_embeddings.parquet"
            write_jsonl(embedding_input, [{"id": "one", "text": "hello world"}])
            write_parquet_rows(embedding_artifact, [{"id": "one", "embedding": [0.1, 0.2]}], float_array_fields=("embedding",))
            embed_records_checkpointed.run(SimpleNamespace(
                input=str(embedding_input),
                output=str(embedding_output),
                output_dir=str(root / "embedding_checkpoints"),
                id_field="id",
                text_fields="text",
                copy_fields="id,text",
                checkpoint_every=1,
                provider="openai",
                input_embeddings=str(embedding_artifact),
                input_id_field="id",
                input_embedding_field="embedding",
                allow_paid=False,
                api_key=None,
                base_url=None,
                model=None,
                concurrency=None,
                openai_usage_tier=None,
                dimension=2,
                api_batch_size=128,
                dry_run=False,
                force=True,
                stop_after_chunks=1,
            ))
            embedding_state = read_json(root / "embedding_checkpoints/checkpoint.json")

        self.assertIn("checkpoint_state_write_seconds", role_state["timings"])
        self.assertIn("checkpoint_state_write_seconds", company_state["timings"])
        self.assertIn("checkpoint_state_write_seconds", embedding_state["timings"])

    def test_processing_company_step_surfaces_primitive_subtimings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ps = paths(root / "out")
            artifact_path = root / "artifact.jsonl"
            flattened = {
                "id": "person-1",
                "work_experiences": [{"company_name": "Acme", "company": "Acme"}],
                "allowed_operator_ids": ["op-test"],
            }
            write_jsonl(ps["flattened"], [flattened])
            write_jsonl(artifact_path, [self.company_artifact_row()])

            _artifacts, stats = pipeline.step_company(
                {
                    "run_dir": str(root / "out"),
                    "default_operator_id": "op-test",
                    "checkpoint_every": 1,
                    "company_input_classifications": str(artifact_path),
                },
                ps,
            )

        self.assertEqual(stats["status"], "completed")
        self.assertIn("subtimings", stats)
        self.assertIn("raw_corpus_seconds", stats["subtimings"])
        self.assertIn("enrichment_stage_seconds", stats["subtimings"])
        self.assertIn("postprocess_contract_hash_seconds", stats["subtimings"])
        self.assertIn("shape_cache_prepare_seconds", stats["subtimings"]["enrichment_manifest"])


if __name__ == "__main__":
    unittest.main()
