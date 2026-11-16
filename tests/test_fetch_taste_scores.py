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


def build_db(path: Path, urls: list[str | None], domains: list[str | None] = ()) -> None:
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE local_person_profiles (id VARCHAR, linkedin_url VARCHAR)")
        for i, url in enumerate(urls):
            con.execute("INSERT INTO local_person_profiles VALUES (?, ?)", [str(i), url])
        con.execute("CREATE TABLE local_companies (id VARCHAR, website_domain VARCHAR)")
        for i, domain in enumerate(domains):
            con.execute("INSERT INTO local_companies VALUES (?, ?)", [str(i), domain])


def rows(path: Path, sql: str) -> list[tuple]:
    with duckdb.connect(str(path)) as con:
        return con.execute(sql).fetchall()


def people(path: Path) -> list[tuple]:
    return rows(path, f"SELECT linkedin_url, scoring_mode, score FROM {fts.TABLE} ORDER BY 1, 2")


class FetchTasteScoresTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "local-search.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, post):
        return fts.run(Path(self.tmp.name), self.db, post=post)

    def test_saves_scored_people_for_both_modes(self):
        build_db(self.db, [A, B, None, ""])
        calls = []

        def post(path, body):
            calls.append((path, body))
            scores = {"employee": [7.5, None], "founder": [None, 4.0]}[body["scoring_mode"]]
            return {"model_id": "m1", "results": [{"score": s, "scored_at": "2026-11-01"} for s in scores]}

        result = self.run_with(post)
        self.assertEqual(result["people"], {"status": "completed", "asked": 2, "scored": 2})
        self.assertEqual(result["companies"]["reason"], "nothing_to_score")
        self.assertEqual([(p, b["linkedin_urls"]) for p, b in calls], [("/v2/taste/people", [A, B])] * 2)
        self.assertEqual(people(self.db), [(A, "employee", 7.5), (B, "founder", 4.0)])

    def test_saves_scored_companies(self):
        build_db(self.db, [], ["acme.com", "nobody.io", None])

        def post(path, body):
            self.assertEqual((path, body), ("/v2/taste/companies", {"websites": ["acme.com", "nobody.io"]}))
            return {"model_id": "c1", "results": [
                {"company_name": "Acme", "scores": {"overall": 7.2, "team": 6.0, "momentum": None}, "scored_at": "2026-11-02"},
                {"status": "not_scored", "scores": None},
            ]}

        result = self.run_with(post)
        self.assertEqual(result["companies"], {"status": "completed", "asked": 2, "scored": 1})
        self.assertEqual(
            rows(self.db, f"SELECT website_domain, company_name, overall, team, momentum, model_id FROM {fts.COMPANY_TABLE}"),
            [("acme.com", "Acme", 7.2, 6.0, None, "c1")],
        )

    def test_batches_of_100(self):
        build_db(self.db, [f"https://www.linkedin.com/in/p{i:03d}" for i in range(150)])
        sizes = []

        def post(path, body):
            sizes.append(len(body["linkedin_urls"]))
            return {"results": [{"score": None}] * len(body["linkedin_urls"])}

        self.run_with(post)
        self.assertEqual(sizes, [100, 50, 100, 50])

    def test_api_refusal_keeps_previous_scores(self):
        build_db(self.db, [A], ["acme.com"])
        self.run_with(lambda path, body: {"results": [{"score": 6.0, "scores": {"overall": 5.0}}]})

        def refuse(path, body):
            raise urllib.error.HTTPError("u", 503, "Unavailable", {}, None)

        result = self.run_with(refuse)
        self.assertEqual(result["people"], {"status": "skipped", "reason": "api_503"})
        self.assertEqual(result["companies"], {"status": "skipped", "reason": "api_503"})
        self.assertEqual(people(self.db), [(A, "employee", 6.0), (A, "founder", 6.0)])
        self.assertEqual(rows(self.db, f"SELECT website_domain, overall FROM {fts.COMPANY_TABLE}"), [("acme.com", 5.0)])

    def test_missing_db_skips(self):
        self.assertEqual(self.run_with(lambda path, body: {})["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
