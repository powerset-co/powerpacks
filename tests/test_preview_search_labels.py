from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from scripts.preview_search_labels import create_preview


class PreviewSearchLabelsTests(unittest.TestCase):
    def test_copies_artifacts_and_clears_judgments_without_changing_sources(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source = root / ".powerpacks/deep-search/original"
            source.mkdir(parents=True)
            (source / "jd.txt").write_text("Build search software.")
            (source / "epoch0").mkdir()
            (source / "epoch0/plan.json").write_text(json.dumps({"traits": ["Search"]}))
            rows_path = root / ".powerpacks/rows.jsonl"
            rows_path.write_text(json.dumps({"person_id": "p1", "final_score": .91,
                                            "trait_scores": {"Engineer": .9}}) + "\n")
            profiles_path = root / "profiles.jsonl.gz"
            with gzip.open(profiles_path, "wt") as stream:
                stream.write(json.dumps({"person_id": "p1", "name": "Jordan Bravo"}) + "\n")
            candidate = {
                "person": "p1", "score": .91, "trait_scores": {"Engineer": .9},
                "fit_experts": {"role_fit": {"label": "strong-fit"}},
                "jd_fit": {"coverage": 1, "traits": [{"trait": "Search"}]},
                "group": "send_worthy", "why": "Model explanation",
                "fit_annotation_source": "luna", "applied_precedent_ids": ["p"],
                "applied_fit_precedents": [{"id": "p"}],
            }
            results = {
                "brief": {"defining_capability": "Search"},
                "raw_model_responses": [{"kind": "company_fit"}],
                "iterations": [{"shortlist_grades": [candidate], "arm": {
                    "traits": ["Engineer"], "ledger": "original-ledger",
                    "payload_json": "original-payload", "artifacts": {
                        "jsonl": str(rows_path.relative_to(root)),
                        "profiles_path": str(profiles_path), "manifest": "not-needed",
                    }}}],
                "summary": {"deduped_candidate_count": 2, "groups": {"send_worthy": [candidate]},
                            "counts": {"send_worthy": 1}, "jd_fit_order": [candidate],
                            "pond_chain": [{"run": "original", "pond_n": 1},
                                           {"run": "related", "pond_n": 1}]},
            }
            (source / "results.json").write_text(json.dumps(results))
            original_bytes = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            destination = root / ".powerpacks/deep-search/preview"
            saved = json.loads(create_preview(source, destination).read_text())
            self.assertTrue(all(path.read_bytes() == data for path, data in original_bytes.items()))
            arm = saved["iterations"][0]["arm"]
            for field, original in (("jsonl", rows_path), ("profiles_path", profiles_path)):
                copied = Path(arm["artifacts"][field])
                self.assertTrue(copied.is_relative_to(destination))
                self.assertNotEqual(copied.stat().st_ino, original.stat().st_ino)
                self.assertEqual(copied.read_bytes(), original.read_bytes())
            self.assertEqual(set(arm["artifacts"]), {"jsonl", "profiles_path"})
            self.assertEqual(arm["traits"], ["Engineer"])
            self.assertEqual(saved["iterations"][0]["shortlist_grades"], [{
                **candidate, "fit_experts": {}, "jd_fit": {"coverage": 0.0, "traits": []},
                "group": "", "why": "", "fit_annotation_source": "",
                "applied_precedent_ids": [], "applied_fit_precedents": [],
            }])
            self.assertEqual(saved["summary"], {
                "deduped_candidate_count": 1, "groups": {}, "counts": {}, "jd_fit_order": [],
                "pond_chain": [{"run": "preview", "pond_n": 1}],
            })
            self.assertIsNone(saved["brief"]["defining_capability"])
            self.assertEqual(saved["raw_model_responses"], [])
            self.assertEqual(json.loads((destination / "epoch0/plan.json").read_text())["traits"], [])
            before_retry = (destination / "results.json").read_bytes()
            with self.assertRaises(FileExistsError):
                create_preview(source, destination)
            self.assertEqual((destination / "results.json").read_bytes(), before_retry)
