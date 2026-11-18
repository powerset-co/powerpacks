import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NoDirectCloudAccessTests(unittest.TestCase):
    """Cloud data comes only through the Powerset API: no install reads Postgres or TurboPuffer."""

    FORBIDDEN = re.compile(r"^\s*(import|from)\s+(psycopg2|psycopg|turbopuffer|postgres_client)\b", re.MULTILINE)

    def test_no_python_imports_a_database_or_turbopuffer_client(self) -> None:
        offenders = []
        for folder in ("packs", "scripts", "adapters", "automations", "bin", "desktop"):
            for path in (ROOT / folder).rglob("*.py"):
                relative = path.relative_to(ROOT).as_posix()
                if "/node_modules/" in relative or "/target/" in relative:
                    continue
                if self.FORBIDDEN.search(path.read_text(encoding="utf-8", errors="ignore")):
                    offenders.append(relative)
        self.assertEqual(offenders, [])
