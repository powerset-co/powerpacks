import unittest

import duckdb

from packs.search.primitives.hydrate_people.hydrate_people import attach_company_logos, local_position


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


if __name__ == "__main__":
    unittest.main()
