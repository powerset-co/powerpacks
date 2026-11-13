import importlib.util
import tempfile
import unittest
import urllib.error
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packs/indexing/primitives/fetch_taste_scores/fetch_taste_scores.py"


def load_module():
    spec = importlib.util.spec_from_file_location("fetch_taste_scores", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


fts = load_module()
A = "https://www.linkedin.com/in/a"
B = "https://www.linkedin.com/in/b"


def build_db(path: Path, urls: list[str | None]) -> None:
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE local_person_profiles (id VARCHAR, linkedin_url VARCHAR)")
        con.executemany("INSERT INTO local_person_profiles VALUES (?, ?)", [(str(i), url) for i, url in enumerate(urls)])


def taste_rows(path: Path) -> list[tuple]:
    with duckdb.connect(str(path)) as con:
        return con.execute(f"SELECT linkedin_url, scoring_mode, score FROM {fts.TABLE} ORDER BY 1, 2").fetchall()


class FetchTasteScoresTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "local-search.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def test_saves_scored_people_for_both_modes(self):
        build_db(self.db, [A, B, None, ""])
        calls = []

        def post(body):
            calls.append(body)
            scores = {"employee": [7.5, None], "founder": [None, 4.0]}[body["scoring_mode"]]
            return {"model_id": "m1", "results": [{"score": s, "scored_at": "2026-11-01"} for s in scores]}

        result = fts.run(Path(self.tmp.name), self.db, post=post)
        self.assertEqual(result, {"status": "completed", "people": 2, "scores": 2})
        self.assertEqual([c["linkedin_urls"] for c in calls], [[A, B], [A, B]])
        self.assertEqual(taste_rows(self.db), [(A, "employee", 7.5), (B, "founder", 4.0)])

    def test_batches_of_100(self):
        urls = [f"https://www.linkedin.com/in/p{i:03d}" for i in range(150)]
        build_db(self.db, urls)
        sizes = []

        def post(body):
            sizes.append(len(body["linkedin_urls"]))
            return {"results": [{"score": None}] * len(body["linkedin_urls"])}

        fts.run(Path(self.tmp.name), self.db, post=post)
        self.assertEqual(sizes, [100, 50, 100, 50])

    def test_api_refusal_keeps_previous_scores(self):
        build_db(self.db, [A])
        fts.run(Path(self.tmp.name), self.db, post=lambda body: {"results": [{"score": 6.0}]})

        def refuse(body):
            raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

        result = fts.run(Path(self.tmp.name), self.db, post=refuse)
        self.assertEqual(result, {"status": "skipped", "reason": "api_403"})
        self.assertEqual(taste_rows(self.db), [(A, "employee", 6.0), (A, "founder", 6.0)])

    def test_missing_db_skips(self):
        result = fts.run(Path(self.tmp.name), self.db, post=lambda body: {})
        self.assertEqual(result["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
