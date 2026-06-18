import importlib.util
import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from packs.shared.csv_io import CsvIO


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packs/search/primitives/agentic_candidate_review" / "agentic_candidate_review.py"


def load_module():
    spec = importlib.util.spec_from_file_location("agentic_candidate_review", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


review = load_module()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


class AgenticCandidateReviewTests(unittest.TestCase):
    def test_prepare_and_reduce_writes_one_sorted_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "run.json"
            write_json(
                state_path,
                {
                    "task_id": "search-network-test",
                    "query": "software engineers in sf",
                    "steps": [
                        {
                            "id": "execute_role_search",
                            "output": {"candidate_ids": ["p1", "p2", "p3"]},
                        },
                        {
                            "id": "hydrate_people",
                            "output": {
                                "profiles": [
                                    {"person_id": "p1", "name": "One", "positions": [], "education": []},
                                    {"person_id": "p2", "name": "Two", "positions": [], "education": []},
                                    {"person_id": "p3", "name": "Three", "positions": [], "education": []},
                                ]
                            },
                        },
                    ],
                },
            )

            args = type("Args", (), {
                "state": str(state_path),
                "out_dir": str(root / "review"),
                "shard_size": 2,
                "limit": None,
                "rubric_file": None,
                "rubric_text": "rank software engineers",
                "write_state": True,
            })()
            with redirect_stdout(io.StringIO()):
                review.cmd_prepare(args)

            manifest_path = root / "review" / "review_manifest.json"
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["shard_count"], 2)
            prepared_state = json.loads(state_path.read_text())
            self.assertEqual(prepared_state["steps"][-1]["id"], "agentic_candidate_review_prepare")
            self.assertEqual(
                prepared_state["artifacts"]["agentic_candidate_review"]["manifest"],
                str(manifest_path),
            )
            self.assertEqual(len(prepared_state["artifacts"]["agentic_candidate_review"]["shards"]), 2)

            write_jsonl(
                Path(manifest["shards"][0]["output_jsonl"]),
                [
                    {"person_id": "p1", "score": 0.2, "decision": "no", "evidence": "weak", "concerns": ""},
                    {"person_id": "p2", "score": 0.9, "decision": "strong_yes", "evidence": "strong", "concerns": ""},
                ],
            )
            write_jsonl(
                Path(manifest["shards"][1]["output_jsonl"]),
                [
                    {"person_id": "p3", "score": 0.7, "decision": "yes", "evidence": "good", "concerns": ""},
                ],
            )

            reduce_args = type("Args", (), {
                "manifest": str(manifest_path),
                "out_dir": None,
                "write_state": True,
            })()
            with redirect_stdout(io.StringIO()):
                review.cmd_reduce(reduce_args)

            ranked_path = root / "review" / "ranked_candidates.jsonl"
            rows = [json.loads(line) for line in ranked_path.read_text().splitlines()]
            self.assertEqual([row["person_id"] for row in rows], ["p2", "p3", "p1"])
            self.assertEqual([row["rank"] for row in rows], [1, 2, 3])

            with (root / "review" / "ranked_candidates.csv").open() as handle:
                csv_rows = list(CsvIO.dict_reader(handle))
            self.assertEqual([row["person_id"] for row in csv_rows], ["p2", "p3", "p1"])

            updated_state = json.loads(state_path.read_text())
            self.assertEqual(updated_state["steps"][-1]["id"], "agentic_candidate_review_reduce")
            review_artifacts = updated_state["artifacts"]["agentic_candidate_review"]
            self.assertEqual(review_artifacts["status"], "completed")
            self.assertEqual(review_artifacts["ranked_csv"], str(root / "review" / "ranked_candidates.csv"))
            self.assertEqual(review_artifacts["ranked_jsonl"], str(root / "review" / "ranked_candidates.jsonl"))


if __name__ == "__main__":
    unittest.main()
