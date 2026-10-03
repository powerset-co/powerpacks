"""Architecture gate for the Deep Context SQLite projection boundary."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from scripts import audit_deep_context_sqlite as invariant

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/audit_deep_context_sqlite.py"
PACKAGE = ROOT / "packs/ingestion/primitives/deep_context"


class DeepContextSqliteInvariantTests(unittest.TestCase):
    def audit_source(self, relative: str, source: str) -> list[invariant.Violation]:
        return invariant.audit_source(PACKAGE / relative, source)

    def test_bans_downstream_artifact_reads(self) -> None:
        violations = self.audit_source(
            "review/bad_consumer.py",
            """from __future__ import annotations
import json
from pathlib import Path

def dossier(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)

def avatar(path: Path) -> bytes:
    return path.read_bytes()
""",
        )
        self.assertEqual(
            [item.rule for item in violations],
            ["artifact-file-read", "artifact-file-read", "artifact-file-read"],
        )
        self.assertIn("path.open in dossier", violations[0].detail)
        self.assertIn("json.load in dossier", violations[1].detail)
        self.assertIn("path.read_bytes in avatar", violations[2].detail)

    def test_bans_known_indirect_artifact_reader(self) -> None:
        violations = self.audit_source(
            "synthesis/selection_example.py",
            """from packs.ingestion.primitives.deep_context.shared.common import load_owner

def select() -> object:
    return load_owner()
""",
        )
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule, "artifact-reader-call")
        self.assertEqual(violations[0].detail, "load_owner")

    def test_allows_sqlite_payload_parsing(self) -> None:
        violations = self.audit_source(
            "consumer.py",
            """import json

def hydrate(row: object) -> object:
    return json.loads(row.payload_json or "{}")
""",
        )
        self.assertEqual(violations, [])

    def test_review_server_reads_no_files(self) -> None:
        # The review page is the React app; nothing under review/ reads a file from disk,
        # whatever its path is called.
        for name in ("DOSSIER", "REVIEW_HTML"):
            with self.subTest(name=name):
                violations = self.audit_source(
                    "review/server.py",
                    f"""from pathlib import Path

{name} = Path("person.md")

def render() -> str:
    return {name}.read_text(encoding="utf-8")
""",
                )
                self.assertEqual([item.rule for item in violations], ["artifact-file-read"])

    def test_csv_parser_exists_only_at_seed_or_import_input_boundary(self) -> None:
        source = """import csv

def rows(values: list[str]) -> object:
    return csv.DictReader(values)
"""
        banned = self.audit_source("consumer.py", source)
        allowed = self.audit_source("migration/seed.py", source)
        imported = self.audit_source("ensure_parents/imported_people.py", source)
        self.assertEqual([item.rule for item in banned], ["csv-input-boundary"])
        self.assertEqual(allowed, [])
        self.assertEqual(imported, [])

    def test_bans_aliased_low_level_file_readers(self) -> None:
        violations = self.audit_source(
            "consumer.py",
            """from builtins import open as open_file
from json import load as hydrate
from packs.shared.csv_io import CsvIO as Files

csv_rows = Files.read_dict_rows

def rows(path):
    direct = path.read_text
    stream = open_file(path)
    parsed = hydrate(stream)
    return direct(), parsed, csv_rows(path)
""",
        )
        self.assertEqual(
            [item.rule for item in violations],
            [
                "artifact-file-read",
                "artifact-file-read",
                "artifact-file-read",
                "csv-input-boundary",
            ],
        )
        self.assertIn("open in rows", violations[0].detail)
        self.assertIn("json.load in rows", violations[1].detail)
        self.assertIn("path.read_text in rows", violations[2].detail)

    def test_source_import_boundary_reads_only_its_fan_in_manifest_and_csvs(self) -> None:
        source = """from packs.shared.csv_io import CsvIO

def read_source_people(people_csv, path):
    manifest_path = people_csv.parent / 'manifest.json'
    manifest = manifest_path.read_text()
    return manifest, CsvIO.read_dict_rows(path)
"""
        relative = "ensure_parents/source_people.py"
        self.assertEqual(self.audit_source(relative, source), [])
        other_scope = source.replace("def read_source_people(", "def consumer(")
        self.assertEqual([row.rule for row in self.audit_source(relative, other_scope)],
                         ["artifact-file-read", "csv-input-boundary"])
        other_artifact = source.replace("'manifest.json'", "'dossier.json'")
        self.assertEqual([row.rule for row in self.audit_source(relative, other_artifact)],
                         ["artifact-file-read"])
        other_read = source.replace("manifest_path.read_text()", "path.read_text()")
        self.assertEqual([row.rule for row in self.audit_source(relative, other_read)],
                         ["artifact-file-read"])

    def test_alias_audit_ignores_reassigned_local_names(self) -> None:
        violations = self.audit_source(
            "consumer.py",
            """def one(profile):
    confidence = profile.confidence
    return confidence

def two(verdict):
    confidence = verdict.confidence
    return confidence
""",
        )
        self.assertEqual(violations, [])

    def test_allows_seed_and_projector_boundaries(self) -> None:
        source = """from pathlib import Path

def project(path: Path) -> bytes:
    return path.read_bytes()
"""
        self.assertEqual(self.audit_source("migration/seed.py", source), [])
        self.assertEqual(self.audit_source("db/projectors.py", source), [])

    def test_provider_projection_never_rehydrates_artifact_files(self) -> None:
        first = self.audit_source(
            "enrich/parallel_research/projection.py",
            """import json
from pathlib import Path

def research_artifact_projections(result_path: Path) -> object:
    return json.loads(result_path.read_bytes())
""",
        )
        banned = self.audit_source(
            "enrich/parallel_research/projection.py",
            """import json
from pathlib import Path

def research_artifact_inventory(result_path: Path) -> object:
    return json.loads(result_path.read_text(encoding="utf-8"))
""",
        )
        self.assertEqual([item.rule for item in first], ["artifact-file-read"])
        self.assertEqual([item.rule for item in banned], ["artifact-file-read"])

    def test_parent_writer_may_hash_its_own_output_for_healing(self) -> None:
        allowed = self.audit_source(
            "merge_candidates/build_parents.py",
            """import hashlib

class BuildParents:
    def execute(self, path, db, rows):
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
        db.project_rows(rows)
        return fingerprint
""",
        )
        self.assertEqual(allowed, [])

    def test_retired_writer_readback_is_not_allowlisted(self) -> None:
        banned = self.audit_source(
            "collection/collect_person_context.py",
            """import json
from pathlib import Path

def _load_bundle(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))
""",
        )
        self.assertEqual([item.rule for item in banned], ["artifact-file-read"])

    def test_untyped_projector_door_is_retired(self) -> None:
        violations = self.audit_source(
            "review/bad_consumer.py",
            """from packs.ingestion.primitives.deep_context.db.projectors import project_artifacts

def hydrate(db: object, root: object, rows: list[dict[str, object]]) -> object:
    return project_artifacts(db, root, rows, stage="review")
""",
        )
        self.assertEqual([item.rule for item in violations], ["untyped-projector"])

    def test_identity_model_assets_are_static_but_candidate_files_are_not(self) -> None:
        source = """from pathlib import Path
MODEL = Path(__file__).with_name('jev_model.json').read_bytes()
QUESTIONS = Path(__file__).with_name('jev_questions.json').read_text()
"""
        self.assertEqual(self.audit_source("enrich/identity_reconcile/jev_judge.py", source), [])
        source = """from pathlib import Path

def load_candidate(path):
    return path.read_text()
"""
        violations = self.audit_source("enrich/identity_reconcile/jev_judge.py", source)
        self.assertEqual([row.rule for row in violations], ["artifact-file-read"])

    def test_parallel_reads_only_its_provider_group_receipt(self) -> None:
        source = """class ParallelClient:
    def execute(self, params):
        manifest_path = params.output_dir / 'manifest.json'
        return manifest_path.read_text()
"""
        relative = "enrich/parallel_research/parallel_client.py"
        self.assertEqual(self.audit_source(relative, source), [])
        for changed in (
            source.replace("'manifest.json'", "'00_parallel_result.json'"),
            source.replace("manifest_path.read_text()", "params.artifact_path.read_text()"),
        ):
            self.assertEqual([row.rule for row in self.audit_source(relative, changed)],
                             ["artifact-file-read"])

    def test_enrichment_preserves_only_its_validated_provider_receipt(self) -> None:
        source = """class EnrichmentReceipt:
    def __post_init__(self):
        if self.path.name != 'manifest.json':
            raise ValueError('invalid receipt path')
    def write(self, payload):
        return self.path.read_text()
"""
        relative = "manifests/enrichment_receipt.py"
        self.assertEqual(self.audit_source(relative, source), [])
        for changed in (
            source.replace("self.path.read_text()", "self.artifact_path.read_text()"),
            source.replace("'manifest.json'", "'result.json'"),
        ):
            self.assertEqual([row.rule for row in self.audit_source(relative, changed)],
                             ["artifact-file-read"])

    def test_runtime_respects_sqlite_projection_boundary(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        payload = json.loads(result.stdout)
        violations = payload["violations"]
        self.assertEqual(
            violations,
            [],
            "\n".join(
                f"{item['path']}:{item['line']} [{item['rule']}] {item['detail']}"
                for item in violations
            ),
        )


if __name__ == "__main__":
    unittest.main()
