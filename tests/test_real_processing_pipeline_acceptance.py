import asyncio
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PEOPLE = ROOT / "tests/fixtures/indexing/people.csv"
ROLE_STAGE = ROOT / "packs/indexing/primitives/enrich_roles_checkpointed/enrich_roles_checkpointed.py"
PIPELINE = ROOT / "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"
DUCKDB_SHIM = ROOT / "scripts/build-local-duckdb-shim.py"
SEARCH_LIB = ROOT / "packs/search/primitives/lib"


def parse_last_json(stdout: str) -> dict:
    text = stdout.strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    idx = 0
    last: dict = {}
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        value, end = decoder.raw_decode(text, idx)
        if isinstance(value, dict):
            last = value
        idx = end
    return last


def run_json(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, dict, str]:
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=180,
    )
    payload = parse_last_json(proc.stdout)
    return proc.returncode, payload, proc.stderr


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def first_jsonl(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                return json.loads(line)
    raise AssertionError(f"empty jsonl: {path}")


def pipeline_has_unregistered_embedding_steps() -> bool:
    source = PIPELINE.read_text(encoding="utf-8")
    return '"embed_role_positions"' in source and '"embed_role_positions":' not in source


def role_fixture_rows() -> list[dict]:
    return [
        {"id": "p1", "headline": "Founder", "work_experiences": [{"title": "Founder and CEO", "company_name": "BuildCo", "description": "fundraising and company building"}]},
        {"id": "p2", "headline": "Engineer", "work_experiences": [{"title": "Staff Software Engineer", "company_name": "ScaleCo", "description": "python kubernetes backend systems"}]},
        {"id": "p3", "headline": "Product", "work_experiences": [{"title": "Product Manager", "company_name": "AppCo", "description": "roadmaps and user research"}]},
        {"id": "p4", "headline": "Revenue", "work_experiences": [{"title": "VP Sales", "company_name": "SalesCo", "description": "go to market and customer development"}]},
        {"id": "p5", "headline": "Data", "work_experiences": [{"title": "Data Scientist", "company_name": "DataCo", "description": "machine learning analytics"}]},
    ]


def write_five_person_csv(path: Path) -> None:
    with FIXTURE_PEOPLE.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    extra = {field: "" for field in fieldnames}
    extra.update(
        {
            "id": "person-product",
            "public_identifier": "product-example",
            "linkedin_url": "https://www.linkedin.com/in/product-example",
            "first_name": "Priya",
            "last_name": "Product",
            "full_name": "Priya Product",
            "headline": "Product manager at AppCo",
            "city": "San Francisco",
            "state": "CA",
            "country": "US",
            "work_experiences": json.dumps([
                {
                    "title": "Product Manager",
                    "company_name": "AppCo",
                    "company_public_identifier": "appco",
                    "start_date": "2022-01",
                    "is_current_position": True,
                    "description": "builds product roadmaps",
                }
            ]),
            "education": json.dumps([{"school_name": "UC Berkeley", "degree": "BA", "field_of_study": "Economics", "end_year": 2018}]),
            "primary_email": "priya@example.com",
            "source_channels": "fixture",
            "source_artifacts": json.dumps(["fixture/acceptance/people.csv"]),
            "merge_key": "linkedin:product-example",
            "merge_confidence": "1.0",
            "merge_sources": "fixture",
            "merged_row_count": "1",
            "needs_review": "false",
        }
    )
    rows.append(extra)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class RealProcessingPipelineAcceptanceTests(unittest.TestCase):
    def test_checkpointed_role_stage_resume_matches_clean_full_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            flattened = tmp / "flattened_people.jsonl"
            write_jsonl(flattened, role_fixture_rows())
            full_dir = tmp / "full"
            partial_dir = tmp / "partial"

            code, full, err = run_json([
                sys.executable,
                str(ROLE_STAGE),
                "run",
                "--flattened",
                str(flattened),
                "--output-dir",
                str(full_dir),
                "--checkpoint-every",
                "2",
                "--force",
            ])
            self.assertEqual(code, 0, err)
            self.assertEqual(full["status"], "completed")
            self.assertEqual(full["provider"], "local_deterministic_no_spend")

            code, partial, err = run_json([
                sys.executable,
                str(ROLE_STAGE),
                "run",
                "--flattened",
                str(flattened),
                "--output-dir",
                str(partial_dir),
                "--checkpoint-every",
                "2",
                "--stop-after-chunks",
                "1",
                "--force",
            ])
            self.assertEqual(code, 0, err)
            self.assertEqual(partial["status"], "partial")
            self.assertEqual(partial["chunks_written_total"], 1)
            self.assertEqual(partial["input_rows_processed"], 2)

            code, resumed, err = run_json([
                sys.executable,
                str(ROLE_STAGE),
                "run",
                "--flattened",
                str(flattened),
                "--output-dir",
                str(partial_dir),
                "--checkpoint-every",
                "2",
            ])
            self.assertEqual(code, 0, err)
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(resumed["counts"]["input_rows_processed"], 5)
            for rel in ["roles_with_dense_text_remapped.jsonl", "raw_titles.jsonl", "role_mapping.csv"]:
                self.assertEqual((partial_dir / rel).read_text(), (full_dir / rel).read_text(), rel)

    def test_build_processing_pipeline_resumes_checkpointed_roles_equal_clean_run(self) -> None:
        if pipeline_has_unregistered_embedding_steps():
            self.skipTest("processing orchestrator has STEPS entry embed_role_positions without STEP_FUNCTIONS handler")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            clean_base = tmp / "clean"
            resume_base = tmp / "resume"

            code, clean, err = run_json([
                sys.executable,
                str(PIPELINE),
                "run",
                "--input",
                str(FIXTURE_PEOPLE),
                "--output-dir",
                str(clean_base),
                "--run-id",
                "candidate",
                "--checkpoint-every",
                "2",
                "--force",
            ])
            self.assertEqual(code, 0, err)
            self.assertEqual(clean["status"], "completed")
            clean_dir = clean_base / "candidate"
            self.assertTrue((clean_dir / "roles/checkpoint.json").exists())

            code, partial, err = run_json([
                sys.executable,
                str(PIPELINE),
                "run",
                "--input",
                str(FIXTURE_PEOPLE),
                "--output-dir",
                str(resume_base),
                "--run-id",
                "candidate",
                "--checkpoint-every",
                "2",
                "--stop-after-role-chunks",
                "1",
                "--force",
            ])
            self.assertEqual(code, 0, err)
            self.assertEqual(partial["status"], "partial")
            self.assertEqual(partial["counts"]["build_roles"]["status"], "partial")
            resume_dir = resume_base / "candidate"
            self.assertFalse((resume_dir / "records/people.records.jsonl").exists())

            code, resumed, err = run_json([
                sys.executable,
                str(PIPELINE),
                "continue",
                "--ledger",
                str(resume_dir / "ledger.json"),
            ])
            self.assertEqual(code, 0, err)
            self.assertEqual(resumed["status"], "completed")
            for rel in [
                "roles/roles_with_dense_text.jsonl",
                "roles/raw_titles.jsonl",
                "roles/role_mapping.csv",
                "records/people.records.jsonl",
                "records/companies.records.jsonl",
                "records/summaries.records.jsonl",
            ]:
                self.assertEqual((resume_dir / rel).read_text(), (clean_dir / rel).read_text(), rel)

    def test_pipeline_vectors_duckdb_knn_self_match_and_role_search(self) -> None:
        if pipeline_has_unregistered_embedding_steps():
            self.skipTest("processing orchestrator has STEPS entry embed_role_positions without STEP_FUNCTIONS handler")
        try:
            import duckdb  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("duckdb is not installed")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            source = tmp / "people.csv"
            write_five_person_csv(source)
            pipeline_out = tmp / "pipeline"
            code, payload, err = run_json([
                sys.executable,
                str(PIPELINE),
                "run",
                "--input",
                str(source),
                "--output-dir",
                str(pipeline_out),
                "--run-id",
                "candidate",
                "--default-operator-id",
                "op-vector",
                "--checkpoint-every",
                "2",
                "--force",
            ])
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["counts"]["embed_role_positions"]["provider"], "local-fake")
            self.assertEqual(payload["counts"]["embed_companies"]["provider"], "local-fake")
            self.assertEqual(payload["counts"]["embed_summaries"]["provider"], "local-fake")
            run_dir = pipeline_out / "candidate"
            self.assertTrue((run_dir / "roles/embedding_checkpoints/checkpoint.json").exists())
            self.assertTrue((run_dir / "company/embedding_checkpoints/checkpoint.json").exists())
            self.assertTrue((run_dir / "summaries/embedding_checkpoints/checkpoint.json").exists())
            role_embedding = read_jsonl(run_dir / "roles/roles_with_embeddings.jsonl")[0]
            self.assertIn("title_hash", role_embedding)
            self.assertIn("dense_embedding", role_embedding)
            self.assertNotIn("embedding", role_embedding)
            summary_embedding = read_jsonl(run_dir / "unified/summary_embeddings.jsonl")[0]
            self.assertEqual(set(summary_embedding), {"person_id", "embedding"})
            company_embedding = read_jsonl(run_dir / "company/company_embeddings_v3.jsonl")[0]
            self.assertEqual(set(company_embedding), {"company_urn", "company_name", "semantic_text", "embedding"})
            self.assertTrue((run_dir / "company/companies_corpus_v3.jsonl").exists())
            self.assertTrue((run_dir / "unified/person_tech_skills.jsonl").exists())

            product_row = next(row for row in read_jsonl(run_dir / "records/people.records.jsonl") if row.get("position_title") == "Product Manager")
            self.assertEqual(len(product_row["vector"]), 1536)
            self.assertTrue(any(value != 0.0 for value in product_row["vector"]))
            summary_row = read_jsonl(run_dir / "records/summaries.records.jsonl")[0]
            company_row = read_jsonl(run_dir / "records/companies.records.jsonl")[0]
            self.assertEqual(len(summary_row["vector"]), 1536)
            self.assertEqual(len(company_row["vector"]), 1536)

            code, db_payload, err = run_json([
                sys.executable,
                str(DUCKDB_SHIM),
                "--records-dir",
                str(run_dir),
                "--operator-id",
                "op-vector",
                "--operator-email",
                "vector@example.com",
                "--output-dir",
                str(tmp / "duckdb"),
                "--flavor",
                "candidate",
                "--force",
            ])
            self.assertEqual(code, 0, err)
            self.assertEqual(db_payload["status"], "ok")

            sys.path.insert(0, str(SEARCH_LIB))
            import turbopuffer_client  # type: ignore

            old_db = os.environ.get("POWERPACKS_LOCAL_SEARCH_DB")
            os.environ["POWERPACKS_LOCAL_SEARCH_DB"] = db_payload["duckdb"]
            turbopuffer_client._local_store_for_path.cache_clear()
            try:
                knn = turbopuffer_client.namespace("people").query(
                    rank_by=("vector", "kNN", product_row["vector"]),
                    top_k=3,
                    include_attributes=["position_title", "base_id"],
                )
                self.assertTrue(knn.rows)
                self.assertEqual(knn.rows[0].position_title, "Product Manager")
                self.assertGreater(knn.rows[0].score, 0.99)

                role_rows = asyncio.run(turbopuffer_client.hybrid_role_rows(
                    {
                        "semantic_query": "Product manager building roadmaps and product strategy for users across a software company",
                        "bm25_queries": ["Product Manager"],
                        "query_embedding": product_row["vector"],
                        "is_current_role": True,
                    },
                    turbopuffer_client.filters_from_role_payload({"is_current_role": True}),
                    top_k=5,
                    include_attributes=["position_title", "base_id"],
                ))
                self.assertTrue(role_rows)
                self.assertEqual(role_rows[0]["position_title"], "Product Manager")
                self.assertIn("hybrid", role_rows[0]["retrieval_mode"])
            finally:
                turbopuffer_client._local_store_for_path.cache_clear()
                if old_db is None:
                    os.environ.pop("POWERPACKS_LOCAL_SEARCH_DB", None)
                else:
                    os.environ["POWERPACKS_LOCAL_SEARCH_DB"] = old_db

    def test_pipeline_artifacts_match_copied_aleph_seed_field_shapes(self) -> None:
        seed = ROOT / ".powerpacks/aleph-seed/2026-05-08/pipeline_output"
        if not (seed / "unified/roles/roles_with_embeddings.jsonl").exists():
            self.skipTest("copied Aleph seed artifacts are not present")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            source = tmp / "people.csv"
            write_five_person_csv(source)
            code, payload, err = run_json([
                sys.executable,
                str(PIPELINE),
                "run",
                "--input",
                str(source),
                "--output-dir",
                str(tmp / "pipeline"),
                "--run-id",
                "candidate",
                "--default-operator-id",
                "op-shape",
                "--checkpoint-every",
                "2",
                "--force",
            ])
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["status"], "completed")
            run_dir = tmp / "pipeline/candidate"

            comparisons = [
                (run_dir / "unified/roles/roles_with_dense_text_remapped.jsonl", seed / "unified/roles/roles_with_dense_text_remapped.jsonl"),
                (run_dir / "unified/roles/roles_with_embeddings.jsonl", seed / "unified/roles/roles_with_embeddings.jsonl"),
                (run_dir / "company/companies_corpus_v3.jsonl", seed / "company/companies_corpus_v3.jsonl"),
                (run_dir / "company/company_embeddings_v3.jsonl", seed / "company/company_embeddings_v3.jsonl"),
                (run_dir / "unified/summary_embeddings.jsonl", seed / "unified/summary_embeddings.jsonl"),
                (run_dir / "unified/person_tech_skills.jsonl", seed / "unified/person_tech_skills.jsonl"),
                (run_dir / "education/people_education.jsonl", seed / "education/people_education.jsonl"),
                (run_dir / "education/schools_corpus.jsonl", seed / "education/schools_corpus.jsonl"),
            ]
            for produced, reference in comparisons:
                produced_first = first_jsonl(produced)
                reference_keys = set(first_jsonl(reference))
                self.assertEqual(set(produced_first), reference_keys, produced)

            role_embedding = first_jsonl(run_dir / "unified/roles/roles_with_embeddings.jsonl")
            company_embedding = first_jsonl(run_dir / "company/company_embeddings_v3.jsonl")
            summary_embedding = first_jsonl(run_dir / "unified/summary_embeddings.jsonl")
            self.assertEqual(len(role_embedding["title_hash"]), 16)
            self.assertEqual(len(role_embedding["dense_embedding"]), 1536)
            self.assertEqual(len(company_embedding["embedding"]), 1536)
            self.assertEqual(len(summary_embedding["embedding"]), 1536)

    def test_checkpointed_role_stage_rejects_paid_provider_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            flattened = tmp / "flattened_people.jsonl"
            write_jsonl(flattened, role_fixture_rows()[:1])
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROLE_STAGE),
                    "run",
                    "--flattened",
                    str(flattened),
                    "--output-dir",
                    str(tmp / "out"),
                    "--provider",
                    "tlm",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Only --provider local", proc.stderr + proc.stdout)
            self.assertFalse((tmp / "out/chunks").exists())

    def test_records_dir_duckdb_build_vector_knn_smoke(self) -> None:
        try:
            import duckdb  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("duckdb is not installed")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            records = tmp / "normal-run" / "records"
            write_jsonl(records / "people.records.jsonl", [
                {
                    "id": "pos-vector-backend",
                    "base_id": "person-vector",
                    "position_id": "pos-vector-backend",
                    "person_id": "person-vector",
                    "position_title": "Backend Engineer",
                    "role_ids": ["backend_engineer"],
                    "role_track": "engineering",
                    "allowed_operator_ids": ["op-vector"],
                    "word_tokens": ["backend", "engineer"],
                    "char_tokens": ["bac"],
                    "d2q_tokens": ["database"],
                    "phrase_tokens": ["backend engin"],
                    "vector": [0.0, 1.0, 0.0],
                }
            ])
            write_jsonl(records / "summaries.records.jsonl", [
                {"id": "person-vector", "base_id": "person-vector", "tech_skills": ["DuckDB"], "allowed_operator_ids": ["op-vector"], "vector": [0.0, 1.0, 0.0]}
            ])
            write_jsonl(records / "companies.records.jsonl", [
                {
                    "id": "company-vector-db",
                    "company_urn": "company-vector-db",
                    "company_name": "VectorDB",
                    "name_aliases_text": "VectorDB database infrastructure",
                    "semantic_text": "Database infrastructure developer tools.",
                    "doc2query_text": "database infrastructure developer tools",
                    "entity_sector_text": "database developer tools",
                    "entity_types": ["developer_tool"],
                    "sector_types": ["developer_tools"],
                    "technology_types": ["database"],
                    "allowed_operator_ids": ["op-vector"],
                    "vector": [0.0, 1.0, 0.0],
                }
            ])
            write_jsonl(records / "education.records.jsonl", [])
            write_jsonl(records / "schools.records.jsonl", [])

            code, payload, err = run_json([
                sys.executable,
                str(DUCKDB_SHIM),
                "--records-dir",
                str(records),
                "--operator-id",
                "op-vector",
                "--operator-email",
                "vector@example.com",
                "--output-dir",
                str(tmp / ".powerpacks/search-index"),
                "--flavor",
                "candidate",
                "--force",
            ])
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["tables"]["local_people_positions"], 1)
            self.assertEqual(payload["tables"]["local_companies"], 1)

            sys.path.insert(0, str(SEARCH_LIB))
            import turbopuffer_client  # type: ignore

            old_db = os.environ.get("POWERPACKS_LOCAL_SEARCH_DB")
            os.environ["POWERPACKS_LOCAL_SEARCH_DB"] = payload["duckdb"]
            turbopuffer_client._local_store_for_path.cache_clear()
            try:
                people = turbopuffer_client.namespace("people").query(
                    rank_by=("vector", "kNN", [0.0, 1.0, 0.0]),
                    top_k=1,
                    include_attributes=["base_id", "position_title", "role_ids"],
                )
                companies = turbopuffer_client.namespace("companies").query(
                    rank_by=("vector", "kNN", [0.0, 1.0, 0.0]),
                    top_k=1,
                    include_attributes=["company_name", "entity_types", "sector_types"],
                )
                self.assertEqual(people.rows[0].id, "pos-vector-backend")
                self.assertEqual(people.rows[0].role_ids, ["backend_engineer"])
                self.assertEqual(companies.rows[0].id, "company-vector-db")
                self.assertEqual(companies.rows[0].entity_types, ["developer_tool"])
            finally:
                turbopuffer_client._local_store_for_path.cache_clear()
                if old_db is None:
                    os.environ.pop("POWERPACKS_LOCAL_SEARCH_DB", None)
                else:
                    os.environ["POWERPACKS_LOCAL_SEARCH_DB"] = old_db

    def test_copied_aleph_seed_artifacts_build_duckdb_contract(self) -> None:
        aleph = ROOT / ".powerpacks/aleph-seed/2026-05-08/pipeline_output"
        if not (aleph / "company/companies_corpus_v3.jsonl").exists():
            self.skipTest("copied Aleph seed artifacts are not present")
        try:
            import duckdb  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("duckdb is not installed")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            code, payload, err = run_json([
                sys.executable,
                str(DUCKDB_SHIM),
                "--aleph-output-dir",
                str(aleph),
                "--operator-id",
                "op-aleph-seed",
                "--operator-email",
                "aleph-seed@example.com",
                "--output-dir",
                str(tmp / ".powerpacks/search-index"),
                "--flavor",
                "candidate",
                "--limit",
                "3",
                "--force",
            ])
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["tables"]["local_companies"], 3)
            self.assertEqual(payload["tables"]["local_summaries"], 3)
            self.assertEqual(payload["tables"]["local_people_education"], 3)
            self.assertEqual(payload["tables"]["local_education"], 3)

            sys.path.insert(0, str(SEARCH_LIB))
            import turbopuffer_client  # type: ignore

            old_db = os.environ.get("POWERPACKS_LOCAL_SEARCH_DB")
            os.environ["POWERPACKS_LOCAL_SEARCH_DB"] = payload["duckdb"]
            turbopuffer_client._local_store_for_path.cache_clear()
            try:
                company = turbopuffer_client.namespace("companies").query(
                    rank_by=["id", "asc"],
                    top_k=1,
                    include_attributes=["company_name", "semantic_text", "doc2query_text", "entity_sector_text"],
                ).rows[0]
                self.assertTrue(company.company_name)
                self.assertTrue(company.semantic_text or company.doc2query_text or company.entity_sector_text)
                self.assertTrue(turbopuffer_client.local_namespace_has_vectors("companies"))
                self.assertTrue(turbopuffer_client.local_namespace_has_vectors("summaries"))

                env = os.environ.copy()
                env["POWERPACKS_LOCAL_SEARCH_DB"] = payload["duckdb"]
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "packs/search/primitives/resolve_companies/resolve_companies.py"),
                        "--payload-json",
                        json.dumps({"company_names": [company.company_name], "operator_ids": ["op-aleph-seed"]}),
                        "--no-ce",
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=env,
                    timeout=60,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                resolved = parse_last_json(proc.stdout)
                self.assertEqual(resolved["namespace"], "local_companies")
                self.assertIn(company.id, resolved["company_ids"])
            finally:
                turbopuffer_client._local_store_for_path.cache_clear()
                if old_db is None:
                    os.environ.pop("POWERPACKS_LOCAL_SEARCH_DB", None)
                else:
                    os.environ["POWERPACKS_LOCAL_SEARCH_DB"] = old_db

    def test_five_person_csv_builds_duckdb_and_local_search_smoke(self) -> None:
        try:
            import duckdb  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("duckdb is not installed")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            source = tmp / "people.csv"
            write_five_person_csv(source)
            output_dir = tmp / "search-index"
            code, payload, err = run_json([
                sys.executable,
                str(DUCKDB_SHIM),
                "--source",
                str(source),
                "--operator-id",
                "op-acceptance",
                "--operator-email",
                "acceptance@example.com",
                "--output-dir",
                str(output_dir),
                "--flavor",
                "candidate",
                "--force",
            ])
            self.assertEqual(code, 0, err)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["tables"]["local_summaries"], 5)
            self.assertGreaterEqual(payload["tables"]["local_people_positions"], 4)

            sys.path.insert(0, str(SEARCH_LIB))
            import turbopuffer_client  # type: ignore

            old_db = os.environ.get("POWERPACKS_LOCAL_SEARCH_DB")
            os.environ["POWERPACKS_LOCAL_SEARCH_DB"] = payload["duckdb"]
            turbopuffer_client._local_store_for_path.cache_clear()
            try:
                response = turbopuffer_client.namespace("people").query(
                    filters=("position_title", "IGlob", "*Product*"),
                    top_k=5,
                    include_attributes=["base_id", "position_title", "allowed_operator_ids"],
                )
                self.assertTrue(response.rows)
                self.assertEqual(response.rows[0].position_title, "Product Manager")
                self.assertIn("op-acceptance", response.rows[0].allowed_operator_ids)
            finally:
                turbopuffer_client._local_store_for_path.cache_clear()
                if old_db is None:
                    os.environ.pop("POWERPACKS_LOCAL_SEARCH_DB", None)
                else:
                    os.environ["POWERPACKS_LOCAL_SEARCH_DB"] = old_db


if __name__ == "__main__":
    unittest.main()
