import unittest
from unittest import mock

import duckdb

from packs.search.primitives.hydrate_people import hydrate_people
from packs.search.primitives.hydrate_people.hydrate_people import attach_company_logos, attach_remote_company_logos, local_position
from packs.search.primitives.lib.powerpacks_contracts import assert_columns_in_contract


class AttachCompanyLogosTest(unittest.TestCase):
    def test_copies_logo_from_local_companies_by_company_id(self):
        conn = duckdb.connect(":memory:")
        conn.execute("create table local_companies(id varchar, company_name varchar, logo_url varchar)")
        conn.execute("insert into local_companies values ('c1', 'Roblox', 'https://x/roblox.png'), ('c2', 'Blank', '')")
        rows = [
            {"person_id": "p1", "company_id": "c1", "company_name": "Roblox"},
            {"person_id": "p1", "company_id": "c2", "company_name": "Blank"},
            {"person_id": "p1", "company_name": "No id"},
        ]
        attach_company_logos(conn, rows)
        self.assertEqual([row.get("company_logo_url") for row in rows], ["https://x/roblox.png", None, None])
        self.assertEqual(local_position(rows[0])["company_logo_url"], "https://x/roblox.png")

    def test_no_companies_table_leaves_rows_alone(self):
        conn = duckdb.connect(":memory:")
        rows = [{"person_id": "p1", "company_id": "c1"}]
        attach_company_logos(conn, rows)
        self.assertNotIn("company_logo_url", rows[0])


class AttachRemoteCompanyLogosTest(unittest.TestCase):
    def test_fills_positions_without_a_logo_from_the_companies_table(self):
        rows = [
            {"id": "p1", "hydrated_context": {"positions": [
                {"company_id": "c1", "company_name": "Roblox"},
                {"company_id": "c2", "company_name": "Kept", "company_logo_url": "https://x/kept.png"},
                {"company_name": "No id"},
            ]}},
            {"id": "p2", "hydrated_context": None},
        ]
        with mock.patch.object(hydrate_people, "fetch_company_logos", return_value={"c1": "https://x/roblox.png"}) as fetch:
            attach_remote_company_logos(rows)
        fetch.assert_called_once_with(["c1"], env_file=None)
        positions = rows[0]["hydrated_context"]["positions"]
        self.assertEqual([p.get("company_logo_url") for p in positions], ["https://x/roblox.png", "https://x/kept.png", None])


class CompanyLogoContractTest(unittest.TestCase):
    def test_remote_logo_query_columns_are_in_the_companies_contract(self):
        assert_columns_in_contract("companies", ["id", "logo_url"])


if __name__ == "__main__":
    unittest.main()
