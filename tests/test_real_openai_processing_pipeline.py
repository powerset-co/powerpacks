import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from packs.shared.csv_io import CsvIO

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PEOPLE = ROOT / "tests/fixtures/indexing/people.csv"
DUCKDB_SHIM = ROOT / "scripts/build-local-duckdb-shim.py"
SEARCH_PRIMITIVES = ROOT / "packs/search/primitives"
SEARCH_LIB = SEARCH_PRIMITIVES / "lib"
SEARCH_SHARED = SEARCH_PRIMITIVES / "shared"
SEARCH_LOCAL = SEARCH_PRIMITIVES / "local"
SEARCH_TURBOPUFFER = SEARCH_PRIMITIVES / "turbopuffer"

from packs.indexing.primitives.build_processing_pipeline import build_processing_pipeline as pipeline
from packs.indexing.primitives.embed_records_checkpointed import embed_records_checkpointed
from packs.indexing.primitives.enrich_companies_checkpointed import enrich_companies_checkpointed
from packs.indexing.primitives.enrich_roles_checkpointed import enrich_roles_checkpointed


def fixture_title_hash(title: str) -> str:
    normalized = "".join(ch for ch in title.lower() if ch.isalnum())
    return (f"fixture{normalized}")[:16].ljust(16, "0")


def write_fixture_with_title_hashes(source: Path, dest: Path) -> Path:
    with source.open(newline="", encoding="utf-8") as handle:
        reader = CsvIO.dict_reader(handle)
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    for row in rows:
        try:
            experiences = json.loads(row.get("work_experiences") or "[]")
        except json.JSONDecodeError:
            experiences = []
        if not isinstance(experiences, list):
            continue
        changed = False
        for exp in experiences:
            if isinstance(exp, dict) and exp.get("title") and not exp.get("title_hash"):
                exp["title_hash"] = fixture_title_hash(str(exp["title"]))
                changed = True
        if changed:
            row["work_experiences"] = json.dumps(experiences)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return dest


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def parse_last_json(stdout: str) -> dict:
    decoder = json.JSONDecoder()
    idx = 0
    last: dict = {}
    text = stdout.strip()
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


class OpenAIProcessingPipelineTests(unittest.TestCase):
    def test_pipeline_default_chat_models_are_gpt_5_1(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            input_csv = write_fixture_with_title_hashes(FIXTURE_PEOPLE, Path(td) / "people_with_hashes.csv")
            args = Namespace(
                input=str(input_csv),
                output_dir=str(Path(td) / "pipeline"),
                default_operator_id="operator:test",
                limit=1,
                checkpoint_every=1000,
                role_openai_model=None,
                company_openai_model=None,
                embedding_openai_model=None,
                dry_run=True,
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                payload = pipeline.estimate_run(args)
        self.assertEqual(payload["estimated_costs"]["effective_models"]["roles"], "gpt-5.1")
        self.assertEqual(payload["estimated_costs"]["effective_models"]["companies"], "gpt-5.1")

    def test_user_facing_fake_embedding_provider_is_rejected(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "packs/indexing/primitives/embed_records_checkpointed/embed_records_checkpointed.py"),
                "run",
                "--input",
                str(FIXTURE_PEOPLE),
                "--output",
                "/tmp/unused.jsonl",
                "--output-dir",
                "/tmp/unused-embeddings",
                "--text-fields",
                "full_name",
                "--provider",
                "local-fake",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid choice", proc.stderr + proc.stdout)

    def test_pipeline_dry_run_estimates_without_provider_calls_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            input_csv = write_fixture_with_title_hashes(FIXTURE_PEOPLE, Path(td) / "people_with_hashes.csv")
            output = Path(td) / "pipeline"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"),
                    "run",
                    "--input",
                    str(input_csv),
                    "--output-dir",
                    str(output),
                    "--dry-run",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = parse_last_json(proc.stdout)
            self.assertIn(payload["status"], {"dry-run", "dry_run"})
            self.assertEqual(payload["paid_calls_made"], 0)
            self.assertEqual(payload["writes_made"], 0)
            self.assertGreater(payload["estimated_paid_calls"]["role_enrichment"], 0)
            self.assertGreater(payload["estimated_paid_calls"]["company_enrichment"], 0)
            self.assertIsInstance(payload["estimated_cost_usd"], float)
            self.assertGreater(payload["estimated_cost_usd"], 0)
            self.assertGreater(payload["estimated_costs"]["stages"]["role_enrichment"]["estimated_usd"], 0)
            self.assertGreater(payload["estimated_costs"]["stages"]["summary_embeddings"]["estimated_tokens"], 0)
            self.assertFalse(output.exists(), "dry-run must not create run directories or pretend artifacts")

    def test_pipeline_dry_run_auto_reuses_output_dir_artifacts_by_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            input_csv = write_fixture_with_title_hashes(FIXTURE_PEOPLE, tmp / "people_with_hashes.csv")
            output = tmp / "pipeline"
            operator_id = "operator:test"
            people = pipeline.flatten_people(input_csv)
            roles = pipeline._role_inputs_for_estimate(people)
            companies = pipeline.build_company_corpus(people, operator_id)
            summaries = pipeline.build_summary_records(pipeline.build_unified_profiles(people), operator_id)["internal_text"]

            self.assertGreater(len(roles), 1)
            self.assertGreater(len(companies), 1)
            self.assertGreater(len(summaries), 1)
            write_jsonl(output / "unified/roles/roles_with_dense_text_remapped.jsonl", [roles[0]])
            write_jsonl(output / "unified/roles/roles_with_embeddings.jsonl", [{**roles[0], "dense_embedding": [0.01] * 1536}])
            write_jsonl(output / "company/companies_corpus_v3.jsonl", [{"company_name": companies[0]["company_name"]}])
            write_jsonl(output / "company/company_embeddings_v3.jsonl", [{"company_urn": companies[0]["company_urn"], "embedding": [0.02] * 1536}])
            write_jsonl(output / "unified/summary_embeddings.jsonl", [{"person_id": summaries[0]["person_id"], "embedding": [0.03] * 1536}])

            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"),
                    "run",
                    "--input",
                    str(input_csv),
                    "--output-dir",
                    str(output),
                    "--default-operator-id",
                    operator_id,
                    "--dry-run",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = parse_last_json(proc.stdout)
            stages = payload["estimated_costs"]["stages"]

            role_coverage = stages["role_enrichment"]["artifact_coverage"]
            self.assertEqual(role_coverage["artifact"], str(output / "unified/roles/roles_with_dense_text_remapped.jsonl"))
            self.assertEqual(role_coverage["reused"], 1)
            self.assertEqual(stages["role_enrichment"]["calls"], role_coverage["missing"])
            self.assertEqual(stages["role_embeddings"]["calls"], stages["role_embeddings"]["artifact_coverage"]["missing"])

            company_coverage = stages["company_enrichment"]["artifact_coverage"]
            self.assertEqual(company_coverage["reused"], 1)
            self.assertLess(stages["company_enrichment"]["calls"], payload["counts"]["companies"])
            self.assertEqual(stages["company_embeddings"]["artifact_coverage"]["reused"], 1)
            self.assertEqual(stages["summary_embeddings"]["artifact_coverage"]["reused"], 1)
            self.assertFalse(stages["summary_embeddings"]["artifact_coverage"]["complete"])
            self.assertEqual(payload["paid_calls_made"], 0)

    def test_pipeline_dry_run_excludes_skipped_unresolved_company_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            company_classes = tmp / "company_classes.jsonl"
            company_embeddings = tmp / "company_embeddings.jsonl"
            resolved = {
                "company_urn": "company:resolved",
                "company_name": "Resolved Company",
                "linkedin_url": "https://www.linkedin.com/company/resolved-company",
                "semantic_text": "Resolved Company",
            }
            unresolved = {
                "company_urn": "company:unresolved",
                "company_name": "Unresolved Employer",
                "semantic_text": "Unresolved Employer",
            }
            write_jsonl(company_classes, [resolved])
            write_jsonl(company_embeddings, [{**resolved, "embedding": [0.02] * 1536}])
            args = Namespace(
                output_dir=str(tmp / "output"),
                default_operator_id="operator:test",
                role_input_classifications=None,
                role_input_embeddings=None,
                company_input_classifications=str(company_classes),
                company_input_embeddings=str(company_embeddings),
                summary_input_embeddings=None,
                skip_unresolved_companies=True,
            )

            stages = pipeline.estimate_costs(args, [], [resolved, unresolved])["stages"]

            self.assertEqual(stages["company_enrichment"]["artifact_coverage"]["skipped_unresolved"], 1)
            self.assertEqual(stages["company_embeddings"]["artifact_coverage"]["required"], 1)
            self.assertEqual(stages["company_embeddings"]["artifact_coverage"]["reused"], 1)
            self.assertEqual(stages["company_embeddings"]["artifact_coverage"]["missing"], 0)
            self.assertEqual(stages["company_embeddings"]["artifact_coverage"]["skipped_unresolved"], 1)
            self.assertEqual(stages["company_embeddings"]["calls"], 0)

    def test_completed_ledger_does_not_gate_incremental_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            input_csv = write_fixture_with_title_hashes(FIXTURE_PEOPLE, tmp / "people_with_hashes.csv")
            output = tmp / "pipeline"
            operator_id = "operator:test"
            people = pipeline.flatten_people(input_csv)
            roles = []
            for role in pipeline._role_inputs_for_estimate(people):
                roles.append(enrich_roles_checkpointed.merge_role(role, {"role_ids": ["software_engineer"], "seniority_band": "senior-ic", "role_track": "engineering", "role_type": "engineering", "cluster": "engineering", "doc2query": ["engineering"], "inferred_skills": ["software"]}))
            companies = pipeline.build_company_corpus(people, operator_id)
            company_rows = []
            for company in companies:
                company_rows.append({**company, "entity_types": ["venture_backed_startup"], "sector_types": ["saas"], "technology_types": [], "customer_type": "Business (B2B)", "funding_stage": "SEED", "company_type": "STARTUP", "ownership_status": "PRIVATE", "stage": "Seed", "accelerators": [], "yc_batches": [], "doc2query": ["software company"], "d2q_text": "software company", "word_text": "software", "semantic_text": company.get("semantic_text") or company["company_name"], "confidence_score": 0.9})
            summaries = pipeline.build_summary_records(pipeline.build_unified_profiles(people), operator_id)["internal_text"]
            write_jsonl(output / "unified/roles/roles_with_dense_text_remapped.jsonl", roles)
            write_jsonl(output / "unified/roles/roles_with_embeddings.jsonl", [{**row, "dense_embedding": [0.01] * 1536} for row in roles])
            write_jsonl(output / "company/companies_corpus_v3.jsonl", company_rows)
            write_jsonl(output / "company/company_embeddings_v3.jsonl", [{"company_urn": row["company_urn"], "company_name": row["company_name"], "semantic_text": row.get("semantic_text", ""), "embedding": [0.02] * 1536} for row in companies])
            write_jsonl(output / "unified/summary_embeddings.jsonl", [{"person_id": row["person_id"], "embedding": [0.03] * 1536} for row in summaries])
            (output / "ledger.json").write_text(json.dumps({"status": "restored", "run_dir": str(output), "steps": []}), encoding="utf-8")

            proc = subprocess.run([
                sys.executable,
                str(ROOT / "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"),
                "run",
                "--input",
                str(input_csv),
                "--output-dir",
                str(output),
                "--default-operator-id",
                operator_id,
            ], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = parse_last_json(proc.stdout)
            self.assertEqual(payload["status"], "completed")
            counts = payload["counts"]
            self.assertEqual(counts["build_roles"]["paid_calls"], 0)
            self.assertGreater(counts["build_roles"]["artifact_hits"], 0)
            self.assertEqual(counts["build_company_corpus"]["paid_calls"], 0)
            self.assertGreater(counts["embed_role_positions"]["artifact_hits"], 0)

    def test_embed_records_openai_boundary_checkpoint_resume(self) -> None:
        calls: list[list[str]] = []
        original = embed_records_checkpointed.openai_embedding_batches

        def fake_openai_embedding_batches(text_groups: list[list[str]], **kwargs):
            dim = int(kwargs.get("dimension") or 1536)
            grouped = []
            for texts in text_groups:
                calls.append(list(texts))
                out = []
                for idx, _text in enumerate(texts):
                    vector = [0.0] * dim
                    vector[idx % dim] = 1.0
                    out.append(vector)
                grouped.append(out)
            return grouped

        embed_records_checkpointed.openai_embedding_batches = fake_openai_embedding_batches
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                input_path = tmp / "records.jsonl"
                output = tmp / "embeddings.jsonl"
                checkpoint = tmp / "checkpoint"
                write_jsonl(input_path, [
                    {"id": "a", "text": "alpha"},
                    {"id": "b", "text": "beta"},
                    {"id": "c", "text": "gamma"},
                ])
                base = dict(
                    input=str(input_path),
                    output=str(output),
                    output_dir=str(checkpoint),
                    id_field="id",
                    text_fields="text",
                    copy_fields="text",
                    checkpoint_every=2,
                    provider="openai",
                    input_embeddings=None,
                    input_id_field=None,
                    input_embedding_field="embedding",
                    allow_paid=True,
                    api_key="test-key",
                    base_url="https://example.invalid/v1",
                    model="text-embedding-test",
                    dimension=1536,
                    api_batch_size=128,
                    cost_per_1k_tokens=0.0,
                    dry_run=False,
                    force=True,
                    stop_after_chunks=1,
                )
                partial = embed_records_checkpointed.run(Namespace(**base))
                self.assertEqual(partial["status"], "partial")
                self.assertTrue((checkpoint / "checkpoint.json").exists())
                resumed_args = dict(base)
                resumed_args["force"] = False
                resumed_args["stop_after_chunks"] = None
                completed = embed_records_checkpointed.run(Namespace(**resumed_args))
                self.assertEqual(completed["status"], "completed")
                rows = read_jsonl(output)
                self.assertEqual([row["id"] for row in rows], ["a", "b", "c"])
                self.assertTrue(all(len(row["embedding"]) == 1536 for row in rows))
                self.assertGreaterEqual(len(calls), 2)
        finally:
            embed_records_checkpointed.openai_embedding_batches = original

    def test_embed_records_replays_existing_embeddings_and_pays_for_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "records.jsonl"
            output = root / "embeddings.jsonl"
            checkpoint = root / "checkpoint"
            cached = root / "cached.jsonl"
            write_jsonl(input_path, [{"id": "a", "text": "alpha"}, {"id": "b", "text": "beta"}])
            write_jsonl(cached, [{"id": "a", "embedding": [0.01] * 1536}])

            def fake_openai_embedding_batches(text_groups: list[list[str]], **_kwargs):
                self.assertEqual(text_groups, [["beta"]])
                return [[[0.02] * 1536]]

            with mock.patch.object(embed_records_checkpointed, "openai_embedding_batches", side_effect=fake_openai_embedding_batches) as mocked:
                manifest = embed_records_checkpointed.run(Namespace(
                    input=str(input_path),
                    output=str(output),
                    output_dir=str(checkpoint),
                    id_field="id",
                    text_fields="text",
                    copy_fields="text",
                    checkpoint_every=100,
                    provider="openai",
                    input_embeddings=str(cached),
                    input_id_field="id",
                    input_embedding_field="embedding",
                    allow_paid=True,
                    api_key="test-key",
                    base_url="https://example.invalid/v1",
                    model="text-embedding-test",
                    dimension=1536,
                    api_batch_size=128,
                    cost_per_1k_tokens=0.0,
                    dry_run=False,
                    force=False,
                    stop_after_chunks=None,
                ))
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(manifest["counts"]["artifact_hits"], 1)
            self.assertEqual(manifest["counts"]["artifact_misses"], 1)
            self.assertEqual(manifest["counts"]["paid_calls"], 1)

    def test_mocked_openai_pipeline_writes_aleph_artifacts_resumes_and_filters_company_classification(self) -> None:
        try:
            import duckdb  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("duckdb is not installed")

        role_calls: list[str] = []
        company_calls: list[str] = []
        embedding_calls: list[int] = []
        orig_role = enrich_roles_checkpointed.call_openai_role_enrichments
        orig_company = enrich_companies_checkpointed.call_openai_company_classifiers
        orig_embed = embed_records_checkpointed.openai_embedding_batches

        def fake_role(role: dict, **_kwargs) -> dict:
            role_calls.append(role.get("raw_title", ""))
            title = str(role.get("raw_title") or "").lower()
            if "engineer" in title:
                track = "engineering"
                role_ids = ["software_engineer"]
            elif "founder" in title or "ceo" in title:
                track = "founder"
                role_ids = ["founder"]
            else:
                track = "product"
                role_ids = ["product_manager"]
            return {"cluster": track, "role_ids": role_ids, "seniority_band": "senior", "role_type": track, "role_track": track, "specialization": "", "doc2query": [track], "inferred_skills": ["python"]}

        def fake_company(company: dict, **_kwargs) -> dict:
            name = str(company.get("company_name") or "")
            company_calls.append(name)
            return {
                "entity_types": ["venture_backed_startup"],
                "sector_types": ["saas"],
                "technology_types": ["workflow"],
                "customer_type": "Business (B2B)",
                "funding_stage": "VENTURE_UNKNOWN",
                "company_type": "STARTUP",
                "ownership_status": "PRIVATE",
                "stage": "",
                "accelerators": [],
                "yc_batches": [],
                "word_text": "venture backed startup saas workflow",
                "d2q_text": f"{name} software company",
                "doc2query": [f"{name} software company"],
                "semantic_text": f"{name} builds software for businesses.",
                "confidence_score": 0.99,
            }

        def fake_embeddings(texts: list[str], **kwargs) -> list[list[float]]:
            embedding_calls.append(len(texts))
            dim = int(kwargs.get("dimension") or 1536)
            vectors = []
            for idx, text in enumerate(texts):
                vector = [0.0] * dim
                vector[(len(text) + idx) % dim] = 1.0
                vectors.append(vector)
            return vectors

        def fake_roles(roles: list[dict], **_kwargs) -> list[dict]:
            return [fake_role(role) for role in roles]

        def fake_companies(companies: list[dict], **_kwargs) -> list[dict]:
            return [fake_company(company) for company in companies]

        def fake_embedding_batches(text_groups: list[list[str]], **kwargs) -> list[list[list[float]]]:
            return [fake_embeddings(texts, **kwargs) for texts in text_groups]

        enrich_roles_checkpointed.call_openai_role_enrichments = fake_roles
        enrich_companies_checkpointed.call_openai_company_classifiers = fake_companies
        embed_records_checkpointed.openai_embedding_batches = fake_embedding_batches
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                input_csv = write_fixture_with_title_hashes(FIXTURE_PEOPLE, tmp / "people_with_hashes.csv")
                run_dir = tmp / "pipeline"
                ledger = pipeline.default_ledger(
                    input_csv,
                    run_dir,
                    "op-openai-test",
                    None,
                    checkpoint_every=2,
                    role_provider="openai",
                    allow_paid_role_provider=True,
                    role_openai_api_key="test-key",
                    embedding_provider="openai",
                    allow_paid_embeddings=True,
                    embedding_openai_api_key="test-key",
                    company_provider="openai",
                    allow_paid_company_provider=True,
                    company_openai_api_key="test-key",
                )
                run_dir.mkdir(parents=True)
                ledger_path = pipeline.paths(run_dir)["ledger"]
                pipeline.save_ledger(ledger_path, ledger)
                partial = pipeline.execute(ledger_path, {"stop_after_company_chunks": 1})
                self.assertEqual(partial["status"], "partial")
                self.assertTrue((run_dir / "company/enrichment_checkpoints/checkpoint.json").exists())
                completed = pipeline.execute(ledger_path)
                self.assertEqual(completed["status"], "completed")
                self.assertTrue(role_calls)
                self.assertTrue(company_calls)
                self.assertTrue(embedding_calls)

                appco = read_jsonl(run_dir / "company/companies_corpus_v3.jsonl")[0]
                self.assertEqual(appco["sector_types"], ["saas"])
                self.assertEqual(set(appco), set(enrich_companies_checkpointed.ALEPH_COMPANY_FIELDS))
                company_embedding = read_jsonl(run_dir / "company/company_embeddings_v3.jsonl")[0]
                self.assertEqual(len(company_embedding["embedding"]), 1536)

                proc = subprocess.run(
                    [
                        sys.executable,
                        str(DUCKDB_SHIM),
                        "--records-dir",
                        str(run_dir),
                        "--operator-id",
                        "op-openai-test",
                        "--operator-email",
                        "openai-test@example.com",
                        "--output-dir",
                        str(tmp / "duckdb"),
                        "--force",
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                db_payload = parse_last_json(proc.stdout)
                for _path in [SEARCH_LIB, SEARCH_SHARED, SEARCH_LOCAL, SEARCH_TURBOPUFFER]:
                    sys.path.insert(0, str(_path))
                import local_search_backend as local_backend  # type: ignore

                old_db = os.environ.get("POWERPACKS_LOCAL_SEARCH_DB")
                os.environ["POWERPACKS_LOCAL_SEARCH_DB"] = db_payload["duckdb"]
                local_backend._local_store_for_path.cache_clear()
                try:
                    rows = local_backend.namespace("companies").query(
                        filters=("sector_types", "ContainsAny", ["saas"]),
                        top_k=5,
                        include_attributes=["company_name", "sector_types", "entity_types", "customer_type"],
                    ).rows
                    self.assertTrue(rows)
                    self.assertTrue(all("saas" in row.sector_types for row in rows))
                finally:
                    local_backend._local_store_for_path.cache_clear()
                    if old_db is None:
                        os.environ.pop("POWERPACKS_LOCAL_SEARCH_DB", None)
                    else:
                        os.environ["POWERPACKS_LOCAL_SEARCH_DB"] = old_db
        finally:
            enrich_roles_checkpointed.call_openai_role_enrichments = orig_role
            enrich_companies_checkpointed.call_openai_company_classifiers = orig_company
            embed_records_checkpointed.openai_embedding_batches = orig_embed


if __name__ == "__main__":
    unittest.main()
