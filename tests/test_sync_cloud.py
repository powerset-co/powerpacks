"""Tests for packs/indexing/primitives/sync_cloud: what the app sends the cloud, and when."""

from __future__ import annotations

import gzip
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from packs.indexing.lib.identity import position_uuid, stable_person_id
from packs.indexing.primitives.sync_cloud import sync_cloud
from packs.ingestion.primitives.deep_context_v2.db.store import STORE_RELATIVE_PATH

P1 = stable_person_id(public_identifier="ada-l")
P2 = stable_person_id(public_identifier="bob-k")
EXPERIENCES = [{"title": "CTO", "company_name": "Acme"}, "not a dict", {"title": "Engineer", "company_name": "Beta"}]


def build_root(tmp: Path, *, headline: str = "Builder", warmth: float = 2.4) -> Path:
    index = tmp / sync_cloud.INDEX_RELATIVE
    index.parent.mkdir(parents=True)
    con = duckdb.connect(str(index))
    con.execute("""CREATE TABLE local_person_profiles (person_id VARCHAR, public_identifier VARCHAR, full_name VARCHAR,
        headline VARCHAR, linkedin_url VARCHAR, work_experiences VARCHAR, source_channels VARCHAR,
        interaction_counts VARCHAR, last_interaction VARCHAR, primary_email VARCHAR, primary_phone VARCHAR,
        hydrated_context VARCHAR)""")
    con.execute("CREATE TABLE local_people_positions (id VARCHAR, base_id VARCHAR, person_id VARCHAR, "
                "position_title VARCHAR, company_id VARCHAR, vector FLOAT[])")
    con.execute("CREATE TABLE local_summaries (id VARCHAR, base_id VARCHAR, person_id VARCHAR, summary VARCHAR, vector FLOAT[])")
    con.execute("CREATE TABLE local_people_education (id VARCHAR, base_id VARCHAR, person_id VARCHAR, "
                "canonical_education_id VARCHAR, school_name VARCHAR)")
    con.execute("CREATE TABLE local_companies (id VARCHAR, company_name VARCHAR, vector FLOAT[])")
    con.execute("CREATE TABLE local_education (id VARCHAR, school_name VARCHAR)")
    con.execute("INSERT INTO local_person_profiles VALUES (?, 'ada-l', 'Ada L', ?, 'https://www.linkedin.com/in/ada-l', ?, "
                "'linkedin_csv,gmail_msgvault', '{\"gmail\": 7}', '2026-11-01', 'Ada@X.com', '', NULL)",
                [P1, headline, json.dumps(EXPERIENCES)])
    con.execute("INSERT INTO local_person_profiles VALUES (?, 'bob-k', 'Bob K', 'Other', '', '[]', 'linkedin_csv', "
                "'{}', '', '', '', NULL)", [P2])
    con.execute("INSERT INTO local_person_profiles VALUES ('no-slug', '', 'No Slug', '', '', '[]', 'imessage', '{}', '', '', '+1', NULL)")
    for idx, exp in ((0, EXPERIENCES[0]), (1, EXPERIENCES[2])):
        con.execute("INSERT INTO local_people_positions VALUES (?, ?, ?, ?, 'co-1', [0.1, 0.2])",
                    [position_uuid(P1, idx), P1, P1, exp["title"]])
    con.execute("INSERT INTO local_summaries VALUES (?, ?, ?, 'about ada', [0.3, 0.4])", [P1, P1, P1])
    con.execute("INSERT INTO local_people_education VALUES ('edu-1', ?, ?, 'school-1', 'State U')", [P1, P1])
    con.execute("INSERT INTO local_companies VALUES ('co-1', 'Acme', [1.0, 0.0])")
    con.execute("INSERT INTO local_education VALUES ('school-1', 'State U')")
    con.close()
    store = tmp / ".powerpacks" / STORE_RELATIVE_PATH
    store.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(store) as conn:
        conn.execute("CREATE TABLE families (public_identifier TEXT, warmth REAL)")
        conn.execute("INSERT INTO families VALUES ('ada-l', ?), ('bob-k', NULL)", (warmth,))
    return tmp


class FakeCloud:
    """The diff endpoint's contract: stored hashes in, upsert/remove out; apply stores hashes."""

    def __init__(self):
        self.stored: dict[str, str] = {}
        self.applied: list[dict] = []

    def __call__(self, path, body):
        body = json.loads(gzip.decompress(gzip.compress(json.dumps(body, default=str).encode())))
        if path.endswith("/diff"):
            sent = body["hashes"]
            return {"upsert": sorted(p for p, h in sent.items() if self.stored.get(p) != h),
                    "remove": sorted(set(self.stored) - set(sent))}
        self.applied.append(body)
        for person in body["people"]:
            self.stored[person["person_id"]] = person["hash"]
        for pid in body["removed"]:
            self.stored.pop(pid, None)
        return {"upserted": len(body["people"]), "removed": len(body["removed"])}


class SyncCloudTest(unittest.TestCase):
    def test_first_sync_sends_people_with_slugs_their_docs_and_entities(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(Path(tmp))
            cloud = FakeCloud()
            result = sync_cloud.sync(root, cloud, log=lambda _: None)
            self.assertEqual((result["people"], result["upsert"], result["remove"]), (2, 2, 0))
            body = cloud.applied[0]
            ada = next(p for p in body["people"] if p["person_id"] == P1)
            self.assertEqual(ada["warmth"], 2.4)
            self.assertEqual(ada["profile"]["headline"], "Builder")
            self.assertEqual(sorted(s["source_channel"] for s in ada["sources"]), ["gmail", "linkedin"])
            self.assertEqual(ada["emails"], [{"primary_email": "ada@x.com", "display_name": "Ada L", "total_messages": 7}])
            # Positions carry their index among the profile's experiences, as build_roles counts them.
            self.assertEqual(sorted(d["ordinal"] for d in ada["docs"]["people"]), [0, 1])
            self.assertEqual([d["id"] for d in ada["docs"]["summaries"]], [P1])
            self.assertEqual(len(ada["docs"]["people_education"]), 1)
            self.assertEqual([c["id"] for c in body["companies"]], ["co-1"])
            self.assertEqual([s["id"] for s in body["schools"]], ["school-1"])

    def test_no_change_is_one_diff_and_no_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(Path(tmp))
            cloud = FakeCloud()
            sync_cloud.sync(root, cloud, log=lambda _: None)
            cloud.applied.clear()
            result = sync_cloud.sync(root, cloud, log=lambda _: None)
            self.assertEqual((result["upsert"], result["remove"], cloud.applied), (0, 0, []))

    def test_edits_and_warmth_resend_only_that_person_and_removals_are_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cloud = FakeCloud()
            sync_cloud.sync(build_root(Path(tmp) / "a"), cloud, log=lambda _: None)
            cloud.applied.clear()
            root = build_root(Path(tmp) / "b", warmth=3.1)
            con = duckdb.connect(str(root / sync_cloud.INDEX_RELATIVE))
            con.execute("DELETE FROM local_person_profiles WHERE person_id = ?", [P2])
            con.close()
            result = sync_cloud.sync(root, cloud, log=lambda _: None)
            self.assertEqual((result["upsert"], result["remove"]), (1, 1))
            self.assertEqual([p["person_id"] for p in cloud.applied[0]["people"]], [P1])
            self.assertEqual(cloud.applied[0]["removed"], [P2])

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cloud = FakeCloud()
            result = sync_cloud.sync(build_root(Path(tmp)), cloud, dry_run=True, log=lambda _: None)
            self.assertEqual((result["upsert"], cloud.applied), (2, []))

    def test_batches_send_each_company_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(Path(tmp))
            cloud = FakeCloud()
            with mock.patch.object(sync_cloud, "BATCH_PEOPLE", 1):
                sync_cloud.sync(root, cloud, log=lambda _: None)
            self.assertEqual(sum(len(b["companies"]) for b in cloud.applied), 1)


if __name__ == "__main__":
    unittest.main()
