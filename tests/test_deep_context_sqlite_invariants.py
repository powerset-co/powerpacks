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
            "review_web/bad_consumer.py",
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
            """from packs.ingestion.primitives.deep_context.common import load_owner

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

    def test_allows_only_named_static_asset_reads(self) -> None:
        allowed = self.audit_source(
            "review_web/rendering.py",
            """from pathlib import Path

REVIEW_HTML = Path(__file__).with_name("reconcile_review.html")

def render() -> str:
    return REVIEW_HTML.read_text(encoding="utf-8")
""",
        )
        banned = self.audit_source(
            "review_web/rendering.py",
            """from pathlib import Path

DOSSIER = Path("person.md")

def render() -> str:
    return DOSSIER.read_text(encoding="utf-8")
""",
        )
        disguised_artifact = self.audit_source(
            "review_web/rendering.py",
            """from pathlib import Path

REVIEW_HTML = Path("person.md")

def render() -> str:
    return REVIEW_HTML.read_text(encoding="utf-8")
""",
        )
        self.assertEqual(allowed, [])
        self.assertEqual([item.rule for item in banned], ["artifact-file-read"])
        self.assertEqual(
            [item.rule for item in disguised_artifact],
            ["artifact-file-read"],
        )

    def test_csv_parser_exists_only_at_legacy_or_import_input_boundary(self) -> None:
        source = """import csv

def rows(values: list[str]) -> object:
    return csv.DictReader(values)
"""
        banned = self.audit_source("consumer.py", source)
        allowed = self.audit_source("db/legacy.py", source)
        imported = self.audit_source("imported_people.py", source)
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

    def test_allows_legacy_and_projector_boundaries(self) -> None:
        source = """from pathlib import Path

def project(path: Path) -> bytes:
    return path.read_bytes()
"""
        self.assertEqual(self.audit_source("db/legacy.py", source), [])
        self.assertEqual(self.audit_source("db/projectors.py", source), [])

    def test_writer_boundary_allows_hash_but_not_rehydration(self) -> None:
        allowed = self.audit_source(
            "parallel_research/driver.py",
            """import hashlib
from pathlib import Path

def research_artifact_inventory(result_path: Path) -> str:
    return hashlib.sha256(result_path.read_bytes()).hexdigest()

def report_progress(db: object, root: Path, rows: list[dict[str, object]]) -> None:
    project_artifacts(db, root, rows, stage="enrich")
""",
        )
        banned = self.audit_source(
            "parallel_research/driver.py",
            """import json
from pathlib import Path

def research_artifact_inventory(result_path: Path) -> object:
    return json.loads(result_path.read_text(encoding="utf-8"))
""",
        )
        self.assertEqual(allowed, [])
        self.assertEqual([item.rule for item in banned], ["artifact-file-read"])

    def test_retired_writer_readback_is_not_allowlisted(self) -> None:
        banned = self.audit_source(
            "collect_person_context.py",
            """import json
from pathlib import Path

def _load_bundle(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))
""",
        )
        self.assertEqual([item.rule for item in banned], ["artifact-file-read"])

    def test_projector_calls_are_limited_to_writer_boundaries(self) -> None:
        violations = self.audit_source(
            "review_web/bad_consumer.py",
            """from packs.ingestion.primitives.deep_context.db.projectors import project_artifacts

def hydrate(db: object, root: object, rows: list[dict[str, object]]) -> object:
    return project_artifacts(db, root, rows, stage="review")
""",
        )
        self.assertEqual([item.rule for item in violations], ["projector-boundary"])

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
