import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PEOPLE = ROOT / "tests/fixtures/indexing/people.csv"
DUCKDB_SHIM = ROOT / "scripts/build-local-duckdb-shim.py"
SEARCH_LIB = ROOT / "packs/search/primitives/lib"

from packs.indexing.primitives.build_processing_pipeline import build_processing_pipeline as pipeline
from packs.indexing.primitives.embed_records_checkpointed import embed_records_checkpointed
from packs.indexing.primitives.enrich_companies_checkpointed import enrich_companies_checkpointed
from packs.indexing.primitives.enrich_roles_checkpointed import enrich_roles_checkpointed


def fixture_title_hash(title: str) -> str:
    normalized = "".join(ch for ch in title.lower() if ch.isalnum())
    return (f"fixture{normalized}")[:16].ljust(16, "0")


def write_fixture_with_title_hashes(source: Path, dest: Path) -> Path:
    with source.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
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
            self.assertFalse(output.exists(), "dry-run must not create run directories or pretend artifacts")

    def test_embed_records_openai_boundary_checkpoint_resume(self) -> None:
        calls: list[list[str]] = []
        original = embed_records_checkpointed.openai_embeddings

        def fake_openai_embeddings(texts: list[str], **kwargs):
            calls.append(list(texts))
            dim = int(kwargs.get("dimension") or 1536)
            out = []
            for idx, _text in enumerate(texts):
                vector = [0.0] * dim
                vector[idx % dim] = 1.0
                out.append(vector)
            return out

        embed_records_checkpointed.openai_embeddings = fake_openai_embeddings
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
            embed_records_checkpointed.openai_embeddings = original

    def test_mocked_openai_pipeline_writes_aleph_artifacts_resumes_and_filters_company_classification(self) -> None:
        try:
            import duckdb  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("duckdb is not installed")

        role_calls: list[str] = []
        company_calls: list[str] = []
        embedding_calls: list[int] = []
        orig_role = enrich_roles_checkpointed.call_openai_role_enrichment
        orig_company = enrich_companies_checkpointed.call_openai_company_classifier
        orig_embed = embed_records_checkpointed.openai_embeddings

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

        enrich_roles_checkpointed.call_openai_role_enrichment = fake_role
        enrich_companies_checkpointed.call_openai_company_classifier = fake_company
        embed_records_checkpointed.openai_embeddings = fake_embeddings
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
                sys.path.insert(0, str(SEARCH_LIB))
                import turbopuffer_client  # type: ignore

                old_db = os.environ.get("POWERPACKS_LOCAL_SEARCH_DB")
                os.environ["POWERPACKS_LOCAL_SEARCH_DB"] = db_payload["duckdb"]
                turbopuffer_client._local_store_for_path.cache_clear()
                try:
                    rows = turbopuffer_client.namespace("companies").query(
                        filters=("sector_types", "ContainsAny", ["saas"]),
                        top_k=5,
                        include_attributes=["company_name", "sector_types", "entity_types", "customer_type"],
                    ).rows
                    self.assertTrue(rows)
                    self.assertTrue(all("saas" in row.sector_types for row in rows))
                finally:
                    turbopuffer_client._local_store_for_path.cache_clear()
                    if old_db is None:
                        os.environ.pop("POWERPACKS_LOCAL_SEARCH_DB", None)
                    else:
                        os.environ["POWERPACKS_LOCAL_SEARCH_DB"] = old_db
        finally:
            enrich_roles_checkpointed.call_openai_role_enrichment = orig_role
            enrich_companies_checkpointed.call_openai_company_classifier = orig_company
            embed_records_checkpointed.openai_embeddings = orig_embed


if __name__ == "__main__":
    unittest.main()
