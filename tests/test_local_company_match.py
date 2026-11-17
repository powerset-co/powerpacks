"""Local company resolution keeps a semantic company only when Luna says it matches the description."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / "packs/search/primitives/local"
sys.path.insert(0, str(ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


resolve_companies = load_module("local_resolve_companies_match_test", LOCAL / "local_resolve_companies.py")


class MatchingCompaniesTests(unittest.TestCase):
    def test_keeps_matches_and_refusals_and_asks_with_the_description(self):
        rows = [
            {"id": "driller", "company_name": "Major Drilling", "semantic_text": "Contract drilling for the mining industry."},
            {"id": "school", "company_name": "University College London", "description": "A university."},
            {"id": "refused", "company_name": "Unanswered Co"},
        ]
        probabilities = {"Contract drilling for the mining industry.": 1.0, "A university.": 0.0}
        asked: dict[str, dict] = {}

        async def decide_all(requests, cache_dir, *, cache_only, stage, on_progress, refused):
            asked.update(requests)
            answers = {}
            for digest, request in requests.items():
                text = json.loads(request["input"])["company"]
                answers[digest] = ([{"type": "predicate", "name": "matches", "probability": probabilities[text]}]
                                   if text in probabilities else [{"type": "refusal"}])
            return answers

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(resolve_companies.decisions, "decide_all", decide_all):
            kept = asyncio.run(resolve_companies.matching_companies(
                "mining industry mineral extraction", rows, Path(tmp)))

        self.assertEqual([row["id"] for row in kept], ["driller", "refused"])
        self.assertEqual(len(asked), 3)
        question = next(iter(asked.values()))["questions"][0]
        self.assertEqual(question["type"], "predicate")
        self.assertIn('"mining industry mineral extraction"', question["instructions"])


if __name__ == "__main__":
    unittest.main()
