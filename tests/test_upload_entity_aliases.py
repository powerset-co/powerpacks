import unittest

import duckdb

from packs.indexing.lib.artifacts import stable_education_edge_uuid
from packs.indexing.primitives.upload_powerset import local_index


class EntityAliasTests(unittest.TestCase):
    def test_education_edge_uses_own_school_and_original_degree_dates(self):
        row = {"id": "old-edge", "base_id": "person", "education_id": "child", "canonical_education_id": "parent",
               "degree": "Original Degree", "degree_normalized": "normalized", "field_of_study": "Field", "start_year": 2001, "end_year": 2005}
        result = local_index.remap_document("education", row, {"schools": {"child": "new-child", "parent": "new-parent"}})
        self.assertEqual(result["id"], stable_education_edge_uuid("person", "new-child", "Original Degree", "Field", 2001, 2005))
        self.assertEqual(result["canonical_education_id"], "new-parent")

    def test_context_remaps_only_explicit_references_and_leaves_provenance(self):
        context = {"positions": [{"company_id": "local-company", "raw": {"company_id": "local-company"}}],
                   "education": [{"school_id": "local-school"}], "provider_response": {"school_id": "local-school"}}
        result = local_index.remap_context(context, {"companies": {"local-company": "company"}, "schools": {"local-school": "school"}})
        self.assertEqual(result["positions"][0]["company_id"], "company")
        self.assertEqual(result["education"][0]["school_id"], "school")
        self.assertEqual(result["positions"][0]["raw"]["company_id"], "local-company")
        self.assertEqual(result["provider_response"]["school_id"], "local-school")

    def test_alias_lookup_includes_child_parent_and_context_references(self):
        con = duckdb.connect()
        con.execute("CREATE TABLE local_people_positions(base_id VARCHAR,company_id VARCHAR)")
        con.execute("INSERT INTO local_people_positions VALUES ('person','company')")
        con.execute("CREATE TABLE local_people_education(base_id VARCHAR,education_id VARCHAR,canonical_education_id VARCHAR)")
        con.execute("INSERT INTO local_people_education VALUES ('person','child','parent')")
        con.execute("CREATE TABLE local_person_profiles(person_id VARCHAR,hydrated_context JSON)")
        con.execute("INSERT INTO local_person_profiles VALUES ('person', ?)", ['{"positions":[{"company_urn":"legacy-company"}]}'])
        self.assertEqual(local_index.referenced_alias_ids(con, ["person"]), {"companies": ["company", "legacy-company"], "schools": ["child", "parent"]})
        con.close()

    def test_many_aliases_never_send_duplicate_entity_ids(self):
        con = duckdb.connect()
        con.execute("CREATE TABLE local_education(id VARCHAR,school_name VARCHAR)")
        con.execute("INSERT INTO local_education VALUES ('a','School'),('b','School')")
        aliases = {"schools": {"a": "canonical", "b": "canonical"}}
        docs = local_index.namespace_rows(con, "schools", ("a", "b"), {}, "operator", frozenset(), aliases)
        self.assertEqual(docs, [{"id": "canonical", "school_name": "School"}])
        con.execute("UPDATE local_education SET school_name='Other' WHERE id='b'")
        with self.assertRaisesRegex(ValueError, "Conflicting local documents"):
            local_index.namespace_rows(con, "schools", ("a", "b"), {}, "operator", frozenset(), aliases)
        con.close()
