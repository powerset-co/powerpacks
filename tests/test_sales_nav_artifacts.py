import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVE = ROOT / "packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py"


class SalesNavArtifactsTests(unittest.TestCase):
    def run_json(self, args: list[str], *, cwd: Optional[Path] = None) -> dict:
        proc = subprocess.run(
            [sys.executable, str(PRIMITIVE), *args],
            cwd=str(cwd or ROOT),
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        return json.loads(proc.stdout)

    def test_ingest_pagination_urls_export_and_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            out_dir = td_path / "sales-nav-run"
            init = self.run_json([
                "init",
                "--query", "VP engineering at Stripe",
                "--set-id", "set-123",
                "--conversation-id", "conv-123",
                "--out-dir", str(out_dir),
                "--run-id", "run-123",
            ])
            state_path = Path(init["state"])

            page1 = {
                "artifact_id": "artifact-1",
                "total_count": 100,
                "results_returned": 2,
                "has_more": True,
                "next_start_offset": 25,
                "filters_used": {"title": "VP Engineering"},
                "leads": [
                    {
                        "member_id": 101,
                        "profile_id": "profile-101",
                        "name": "Ada Lovelace",
                        "title": "VP Engineering",
                        "headline": "VP Engineering at Stripe",
                        "summary": "Builds financial infrastructure teams.",
                        "company": "Stripe",
                        "location": "San Francisco",
                        "linkedin_url": "https://www.linkedin.com/in/ada",
                        "source_account_ids": ["acct-a"],
                        "mutual_count": 2,
                        "total_mutual_count": 2,
                        "mutual_member_ids": [201, 202],
                        "mutuals": [
                            {
                                "member_id": 201,
                                "name": "Mallory Mutual",
                                "person_id": "person-201",
                                "operators": [
                                    {"operator_id": "op-1", "operator_name": "Op One", "source_channels": ["sales_nav"]}
                                ],
                            },
                            {"member_id": 202, "first_name": "Pat"},
                        ],
                    },
                    {
                        "member_id": 102,
                        "name": "Grace Hopper",
                        "title": "Head of Engineering",
                        "company": "Stripe",
                        "location": "New York",
                        "source_account_ids": ["acct-b"],
                        "mutual_member_ids": [],
                        "mutuals": [],
                    },
                ],
            }
            page1_path = td_path / "page1.json"
            page1_path.write_text(json.dumps(page1))
            ingest1 = self.run_json(["ingest-page", "--state", str(state_path), "--response", str(page1_path)])
            self.assertEqual(ingest1["lead_count"], 2)
            self.assertEqual(ingest1["mutual_edge_count"], 2)

            # get_artifact compact page shape for pagination. Lead 101 appears
            # again with an added source account/mutual; lead 103 is new.
            page2 = {
                "id": "artifact-1",
                "conversation_id": "conv-123",
                "page": {
                    "kind": "sales_nav_leads",
                    "offset": 25,
                    "limit": 25,
                    "returned": 2,
                    "total": 100,
                    "has_more": True,
                    "next_offset": 50,
                    "results": [
                        {
                            "member_id": 101,
                            "name": "Ada Lovelace",
                            "title": "VP Engineering",
                            "company": "Stripe",
                            "location": "San Francisco",
                            "operators": [
                                {"operator_id": "op-2", "operator_name": "Op Two", "source_channels": ["sales_nav"]}
                            ],
                            "mutuals": [
                                {"member_id": 203, "name": "Quinn Mutual", "linkedin_url": "https://www.linkedin.com/in/quinn"}
                            ],
                        },
                        {
                            "member_id": 103,
                            "name": "Barbara Liskov",
                            "title": "Engineering Leader",
                            "company": "Stripe",
                            "location": "Boston",
                            "mutuals": [{"member_id": 204, "name": "Riley Mutual"}],
                        },
                    ],
                },
            }
            page2_path = td_path / "page2.json"
            page2_path.write_text(json.dumps(page2))
            ingest2 = self.run_json(["ingest-page", "--state", str(state_path), "--response", str(page2_path)])
            self.assertEqual(ingest2["lead_count"], 3)
            self.assertEqual(ingest2["mutual_edge_count"], 4)

            pending = self.run_json(["pending-mutual-ids", "--state", str(state_path)])
            self.assertEqual(pending["member_ids"], [201, 202, 204])

            urls = {"resolved": {"201": "https://www.linkedin.com/in/mallory", "202": "https://www.linkedin.com/in/pat"}, "unresolved": [204], "cache_only": True}
            urls_path = td_path / "urls.json"
            urls_path.write_text(json.dumps(urls))
            url_out = self.run_json(["ingest-member-urls", "--state", str(state_path), "--response", str(urls_path)])
            self.assertEqual(url_out["resolved_count"], 2)

            enriched_artifact = {
                "id": "artifact-1",
                "conversation_id": "conv-123",
                "content": {
                    "extended_results": {
                        "leads": [
                            {
                                "member_id": 101,
                                "name": "Ada Lovelace",
                                "title": "VP Engineering",
                                "company": "Stripe",
                                "headline": "VP Engineering, Payments Platform",
                                "summary": "Led real estate finance and payments platform engineering.",
                                "enriched": True,
                                "experiences": [
                                    {"company_name": "Stripe", "title": "VP Engineering", "start_year": 2021, "end_year": None, "is_current": True},
                                    {"company_name": "Brookfield", "title": "Engineering Advisor", "start_year": 2018, "end_year": 2020, "is_current": False},
                                ],
                                "education": [
                                    {"school": "MIT", "degree": "BS", "field_of_study": "Computer Science", "end_year": 2010}
                                ],
                            }
                        ]
                    }
                },
            }
            enriched_path = td_path / "enriched.json"
            enriched_path.write_text(json.dumps(enriched_artifact))
            ingest_enriched = self.run_json(["ingest-page", "--state", str(state_path), "--response", str(enriched_path), "--prefer-content"])
            self.assertEqual(ingest_enriched["lead_count"], 3)

            exported = self.run_json(["export", "--state", str(state_path)])
            leads_csv = Path(exported["leads_csv"])
            mutuals_csv = Path(exported["mutuals_csv"])
            self.assertEqual(leads_csv.parent.name, "exports")
            self.assertEqual(mutuals_csv.parent.name, "exports")
            self.assertTrue(leads_csv.exists())
            self.assertTrue(mutuals_csv.exists())
            with leads_csv.open(newline="") as handle:
                lead_rows = list(csv.DictReader(handle))
            with mutuals_csv.open(newline="") as handle:
                mutual_rows = list(csv.DictReader(handle))
            self.assertEqual(len(lead_rows), 3)
            self.assertEqual(len(mutual_rows), 4)
            ada = next(row for row in lead_rows if row["member_id"] == "101")
            self.assertNotIn("source_account_id", ada)
            self.assertEqual(json.loads(ada["source_account_ids"]), ["acct-a"])
            self.assertEqual(ada["enriched"], "True")
            self.assertIn("real estate finance", ada["summary"])
            self.assertIn("Brookfield", ada["experiences"])
            self.assertIn("MIT", ada["education"])
            self.assertIn("203", ada["mutual_member_ids"])
            mallory = next(row for row in mutual_rows if row["mutual_member_id"] == "201")
            self.assertEqual(mallory["mutual_linkedin_url"], "https://www.linkedin.com/in/mallory")

            lookup = self.run_json(["lookup", "--state", str(state_path), "--query", "ada"])
            self.assertEqual(lookup["count"], 1)
            self.assertEqual(lookup["results"][0]["member_id"], "101")
            self.assertEqual(len(lookup["results"][0]["mutuals"]), 3)

            state = json.loads(state_path.read_text())
            self.assertEqual(state["counts"], {"leads": 3, "member_urls": 2, "mutual_edges": 4})
            self.assertEqual(state["artifact_ids"], ["artifact-1"])


if __name__ == "__main__":
    unittest.main()
