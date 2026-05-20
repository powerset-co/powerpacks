import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PEOPLE = ROOT / "tests/fixtures/indexing/people.csv"
PIPELINE = ROOT / "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"


def run_cli(*args: str) -> dict:
    proc = subprocess.run([sys.executable, str(PIPELINE), *args], cwd=ROOT, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {proc.stderr}\nstdout={proc.stdout}")
    return json.loads(proc.stdout)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


class IndexingPipelineTests(unittest.TestCase):
    def _precomputed_inputs(self, root: Path) -> dict[str, Path]:
        from packs.indexing.lib.artifacts import build_company_corpus, build_summary_records
        from packs.indexing.lib.people import build_unified_profiles, flatten_people
        from packs.indexing.primitives.enrich_roles_checkpointed import enrich_roles_checkpointed as roles_stage

        people = flatten_people(FIXTURE_PEOPLE)
        roles = []
        seen = set()
        for person in people:
            for position in roles_stage.get_positions(person):
                base = roles_stage.role_input(person, position)
                if base and base["title_hash"] not in seen:
                    seen.add(base["title_hash"])
                    roles.append(roles_stage.merge_role(base, {"role_ids": ["software_engineer"], "seniority_band": "senior-ic", "role_track": "engineering", "role_type": "engineering", "cluster": "engineering", "doc2query": ["engineering leadership"], "inferred_skills": ["software engineering"]}))
        role_classes = root / "roles_with_dense_text_remapped.jsonl"
        write_jsonl(role_classes, roles)
        role_embeddings = root / "roles_with_embeddings.jsonl"
        write_jsonl(role_embeddings, [{**row, "dense_embedding": [0.01] * 1536} for row in roles])

        companies = build_company_corpus(people, "operator:test")
        company_classes = root / "companies_corpus_v3.jsonl"
        write_jsonl(company_classes, [{"company_urn": row["id"], "company_name": row["company_name"], "entity_types": ["venture_backed_startup"], "sector_types": ["saas"], "technology_types": ["developer_tools"], "customer_type": "Business (B2B)", "funding_stage": "SEED", "company_type": "STARTUP", "ownership_status": "PRIVATE", "stage": "Seed", "accelerators": ["YC"], "yc_batches": ["W24"], "doc2query": ["b2b software"], "d2q_text": "b2b software", "word_text": "venture backed startup saas", "semantic_text": row.get("semantic_text") or row["company_name"], "confidence_score": 0.9} for row in companies])
        company_embeddings = root / "company_embeddings_v3.jsonl"
        write_jsonl(company_embeddings, [{"company_urn": row["id"], "company_name": row["company_name"], "semantic_text": row.get("semantic_text", ""), "embedding": [0.02] * 1536} for row in companies])

        summaries = build_summary_records(build_unified_profiles(people), "operator:test")["internal_text"]
        summary_embeddings = root / "summary_embeddings.jsonl"
        write_jsonl(summary_embeddings, [{"person_id": row["person_id"], "embedding": [0.03] * 1536} for row in summaries])
        return {"role_classes": role_classes, "role_embeddings": role_embeddings, "company_classes": company_classes, "company_embeddings": company_embeddings, "summary_embeddings": summary_embeddings}

    def test_dry_run_estimates_paid_stages_without_completed_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / ".powerpacks"
            result = run_cli("run", "--dry-run", "--output-dir", str(base / "search-index"), "--input", str(FIXTURE_PEOPLE))
            self.assertIn(result["status"], {"dry-run", "dry_run"})
            self.assertGreater(result["counts"]["positions_missing_upstream_title_hash"], 0)
            self.assertFalse((base / "search-index/ledger.json").exists())

    def test_run_blocks_before_paid_stage_without_approval_or_precomputed_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / ".powerpacks"
            proc = subprocess.run([sys.executable, str(PIPELINE), "run", "--output-dir", str(base / "search-index"), "--input", str(FIXTURE_PEOPLE), "--force"], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("requires --allow-paid-role-provider or --role-input-classifications", proc.stderr + proc.stdout)
            self.assertFalse((base / "search-index/roles/chunks").exists())

    def test_pipeline_does_not_recompute_missing_title_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / ".powerpacks"
            proc = subprocess.run([sys.executable, str(PIPELINE), "run", "--output-dir", str(base / "search-index"), "--input", str(FIXTURE_PEOPLE), "--role-input-classifications", str(Path(td) / "missing.jsonl"), "--allow-paid-company-provider", "--allow-paid-embeddings", "--force"], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("missing input role classifications", proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()
