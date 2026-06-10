from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / "packs/search/primitives"
LIB = PRIMITIVES / "lib"
SHARED = PRIMITIVES / "shared"
TURBOPUFFER = PRIMITIVES / "turbopuffer"
for _path in [LIB, SHARED, TURBOPUFFER]:
    sys.path.insert(0, str(_path))

SCRIPT = TURBOPUFFER / "turbopuffer_resolve_education.py"


def load_module():
    spec = importlib.util.spec_from_file_location("turbopuffer_resolve_education", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


resolve_education = load_module()


class ResolveEducationTests(unittest.TestCase):
    def test_affiliated_school_queries_for_root_university(self) -> None:
        self.assertEqual(resolve_education.affiliated_school_queries("Stanford University"), ["stanford"])
        self.assertEqual(resolve_education.affiliated_school_queries("Harvard University"), ["harvard"])

    def test_affiliated_school_queries_keeps_specific_school_specific(self) -> None:
        self.assertEqual(resolve_education.affiliated_school_queries("Stanford Graduate School of Business"), [])
        self.assertEqual(resolve_education.affiliated_school_queries("University of Pennsylvania"), [])

    def test_affiliated_candidate_requires_same_leading_token(self) -> None:
        self.assertTrue(resolve_education.is_affiliated_candidate(["stanford"], "Stanford Graduate School of Business"))
        self.assertTrue(resolve_education.is_affiliated_candidate(["stanford"], "Stanford Continuing Studies"))
        self.assertFalse(resolve_education.is_affiliated_candidate(["stanford"], "Samford University"))


if __name__ == "__main__":
    unittest.main()
