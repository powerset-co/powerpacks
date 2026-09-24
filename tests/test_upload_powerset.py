"""Tests for packs/indexing/primitives/upload_powerset."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb
import turbopuffer

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from packs.indexing.primitives.upload_powerset import postgres, turbopuffer_writer, upload_powerset
from packs.indexing.primitives.upload_powerset.models import (
    CloudState,
    LocalPerson,
    SourceRow,
    TagRow,
)
from packs.indexing.primitives.upload_powerset.plan import build_plan
from packs.indexing.primitives.upload_powerset.turbopuffer_writer import NAMESPACES
from packs.ingestion.schemas.share_schema import HUMAN_PRIVATE, HUMAN_SHARE, PRIVATE_SUGGESTED, ShareRow

OPERATOR = "00000000-0000-0000-0000-0000000000aa"
OTHER_OPERATOR = "00000000-0000-0000-0000-0000000000bb"
NEW_PERSON = "11111111-1111-5111-8111-111111111111"
CLOUD_PERSON = "22222222-2222-5222-8222-222222222222"
NO_SLUG_PERSON = "33333333-3333-5333-8333-333333333333"
STALE_PERSON = "44444444-4444-5444-8444-444444444444"

NAMESPACE_NAMES = {
    "people": "aleph_people_v1",
    "summaries": "aleph_summaries_v1",
    "education": "aleph_people_education_v1",
    "companies": "aleph_companies_v1",
    "schools": "aleph_education_v1",
}

# Cloud persons upsert columns with a locally sourced value.
CLOUD_PERSONS_COLUMNS = [
    "public_profile_url", "first_name", "last_name", "full_name", "headline", "summary",
    "profile_picture_url", "city", "state", "country", "location_raw", "hydrated_context",
    "x_twitter_handle", "x_twitter_followers", "linkedin_followers", "linkedin_connections",
    "ig_followers", "inferred_birth_year",
]


def share_row(person_id: str, slug: str, *, share: bool = True, reason: str = "") -> ShareRow:
    return ShareRow(person_id, slug, share, reason, (), "machine", "2026-09-24T00:00:00Z")


def local_person(person_id: str, slug: str, **kwargs) -> LocalPerson:
    return LocalPerson(
        person_id=person_id,
        public_identifier=slug,
        channels=kwargs.pop("channels", ("linkedin",)),
        interaction_counts=kwargs.pop("interaction_counts", {}),
        last_interaction=kwargs.pop("last_interaction", ""),
        primary_email=kwargs.pop("primary_email", ""),
        primary_phone=kwargs.pop("primary_phone", ""),
    )


def cloud_state(**kwargs) -> CloudState:
    return CloudState(
        cloud_id_by_person=kwargs.pop("cloud_id_by_person", {}),
        operator_sources=kwargs.pop("operator_sources", ()),
        operator_ids_by_person=kwargs.pop("operator_ids_by_person", {}),
        private_tag_keys=kwargs.pop("private_tag_keys", frozenset()),
        present_entity_ids=kwargs.pop("present_entity_ids", {}),
    )


def plan_for(share_rows, people, cloud, **kwargs):
    return build_plan(
        operator_id=OPERATOR,
        share_rows=tuple(share_rows),
        people={person.person_id: person for person in people},
        cloud=cloud,
        namespace_names=NAMESPACE_NAMES,
        company_ids_by_person=kwargs.pop("company_ids_by_person", {}),
        school_ids_by_person=kwargs.pop("school_ids_by_person", {}),
    )


class FakeCursor:
    """Records every statement; replays canned rows in the order they are asked for."""

    def __init__(self, results: list[list[tuple]] | None = None, rowcounts: list[int] | None = None) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self._results = list(results or [])
        self._rowcounts = list(rowcounts or [])
        self._current: list[tuple] = []
        self.rowcount = 0

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.statements.append((sql, params))
        self._current = self._results.pop(0) if self._results else []
        self.rowcount = self._rowcounts.pop(0) if self._rowcounts else 1

    def fetchone(self):
        return self._current[0] if self._current else None

    def fetchall(self):
        return list(self._current)


class FakeNamespace:
    """Records write calls; answers queries from canned rows."""

    def __init__(self, rows=None) -> None:
        self.writes: list[dict] = []
        self.queries: list[dict] = []
        self._rows = rows or []

    def write(self, **kwargs):
        self.writes.append(kwargs)

    def schema(self):
        return {"id": {}, "base_id": {}, "person_id": {}, "position_title": {}}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return mock.Mock(rows=self._rows)


class PlanBucketTests(unittest.TestCase):
    def test_new_person_upserts_and_cloud_person_patches(self):
        plan = plan_for(
            [share_row(NEW_PERSON, "jordan-bravo"), share_row(CLOUD_PERSON, "casey-lane")],
            [local_person(NEW_PERSON, "jordan-bravo"), local_person(CLOUD_PERSON, "casey-lane")],
            cloud_state(cloud_id_by_person={CLOUD_PERSON: CLOUD_PERSON}),
        )
        people_ns = next(ns for ns in plan.namespaces if ns.logical == "people")
        self.assertEqual(plan.persons_upsert, (NEW_PERSON, CLOUD_PERSON))
        self.assertEqual(people_ns.upsert_ids, (NEW_PERSON,))
        self.assertEqual(people_ns.patch_person_ids, (CLOUD_PERSON,))

    def test_person_without_slug_is_skipped_not_upserted(self):
        plan = plan_for(
            [share_row(NO_SLUG_PERSON, "")],
            [local_person(NO_SLUG_PERSON, "")],
            cloud_state(),
        )
        self.assertEqual(plan.skipped_no_linkedin, (NO_SLUG_PERSON,))
        self.assertEqual(plan.persons_upsert, ())
        self.assertEqual(plan.sources_insert, ())

    def test_unshared_person_deletes_sources_and_joins_the_patch_list(self):
        stale = SourceRow(STALE_PERSON, "gmail", "casey@example.com", 3, "")
        plan = plan_for(
            [share_row(STALE_PERSON, "casey-lane", share=False)],
            [local_person(STALE_PERSON, "casey-lane")],
            cloud_state(operator_sources=(stale,), operator_ids_by_person={STALE_PERSON: (OPERATOR, OTHER_OPERATOR)}),
        )
        summaries = next(ns for ns in plan.namespaces if ns.logical == "summaries")
        self.assertEqual(plan.sources_delete, (stale,))
        self.assertEqual(summaries.patch_person_ids, (STALE_PERSON,))
        self.assertEqual(plan.allowed_operator_ids[STALE_PERSON], (OTHER_OPERATOR,))

    def test_private_person_in_cloud_gets_a_tag_and_an_un_privated_one_loses_it(self):
        plan = plan_for(
            [
                share_row(CLOUD_PERSON, "casey-lane", share=False, reason=HUMAN_PRIVATE),
                share_row(NEW_PERSON, "jordan-bravo"),
            ],
            [local_person(CLOUD_PERSON, "casey-lane"), local_person(NEW_PERSON, "jordan-bravo")],
            cloud_state(
                cloud_id_by_person={CLOUD_PERSON: CLOUD_PERSON},
                private_tag_keys=frozenset({"casey-lane", "jordan-bravo", "someone-cloud-only"}),
            ),
        )
        self.assertEqual([row.group_key for row in plan.tags_put], ["casey-lane"])
        # jordan-bravo's local decision is a machine default, so the cloud tag (a
        # human's word in the Powerset UI) stays; someone-cloud-only is not ours at all.
        self.assertEqual(plan.tags_delete, ())

    def test_a_human_share_tag_drops_the_cloud_private_tag(self):
        plan = plan_for(
            [share_row(NEW_PERSON, "jordan-bravo", reason=HUMAN_SHARE)],
            [local_person(NEW_PERSON, "jordan-bravo")],
            cloud_state(private_tag_keys=frozenset({"jordan-bravo", "someone-cloud-only"})),
        )
        self.assertEqual([row.group_key for row in plan.tags_delete], ["jordan-bravo"])

    def test_a_machine_private_suggestion_also_tags_the_cloud(self):
        plan = plan_for(
            [share_row(CLOUD_PERSON, "casey-lane", share=False, reason=PRIVATE_SUGGESTED)],
            [local_person(CLOUD_PERSON, "casey-lane")],
            cloud_state(cloud_id_by_person={CLOUD_PERSON: CLOUD_PERSON}),
        )
        self.assertEqual([row.group_key for row in plan.tags_put], ["casey-lane"])

    def test_a_cloud_minted_id_is_used_for_sources_tags_and_patches(self):
        cloud_minted = "99999999-9999-4999-8999-999999999999"
        plan = plan_for(
            [share_row(CLOUD_PERSON, "casey-lane")],
            [local_person(CLOUD_PERSON, "casey-lane", channels=("linkedin",))],
            cloud_state(cloud_id_by_person={CLOUD_PERSON: cloud_minted},
                        operator_ids_by_person={cloud_minted: (OTHER_OPERATOR,)}),
        )
        people_ns = next(ns for ns in plan.namespaces if ns.logical == "people")
        self.assertEqual(plan.persons_upsert, (CLOUD_PERSON,))
        self.assertEqual([row.person_id for row in plan.sources_insert], [cloud_minted])
        self.assertEqual(people_ns.patch_person_ids, (cloud_minted,))
        self.assertEqual(plan.allowed_operator_ids[cloud_minted], tuple(sorted((OPERATOR, OTHER_OPERATOR))))

    def test_private_person_absent_from_cloud_gets_no_tag(self):
        plan = plan_for(
            [share_row(NEW_PERSON, "jordan-bravo", share=False, reason=HUMAN_PRIVATE)],
            [local_person(NEW_PERSON, "jordan-bravo")],
            cloud_state(),
        )
        self.assertEqual(plan.tags_put, ())

    def test_entity_namespaces_upsert_only_ids_the_cloud_lacks(self):
        plan = plan_for(
            [share_row(NEW_PERSON, "jordan-bravo")],
            [local_person(NEW_PERSON, "jordan-bravo")],
            cloud_state(present_entity_ids={"companies": frozenset({"company-a"})}),
            company_ids_by_person={NEW_PERSON: ("company-a", "company-b")},
        )
        companies = next(ns for ns in plan.namespaces if ns.logical == "companies")
        self.assertEqual(companies.upsert_ids, ("company-b",))
        self.assertEqual(companies.patch_person_ids, ())


class SourceRowTests(unittest.TestCase):
    def test_channel_labels_map_to_cloud_names_and_identifiers(self):
        person = LocalPerson.from_csv_row({
            "id": NEW_PERSON,
            "public_identifier": "jordan-bravo",
            "source_channels": "linkedin_csv,gmail_msgvault,imessage,whatsapp",
            "interaction_counts": json.dumps({"gmail": 12, "imessage": 4}),
            "last_interaction": "2026-09-01T00:00:00+00:00",
            "primary_email": "casey@example.com",
            "primary_phone": "+15550100",
        })
        self.assertEqual(person.channels, ("gmail", "imessage", "linkedin", "whatsapp"))
        plan = plan_for([share_row(NEW_PERSON, "jordan-bravo")], [person], cloud_state())
        self.assertEqual(
            [(row.source_channel, row.source_identifier, row.total_interactions) for row in plan.sources_insert],
            [
                ("gmail", "casey@example.com", 12),
                ("imessage", "+15550100", 4),
                ("linkedin", "jordan-bravo", 0),
                ("whatsapp", "+15550100", 0),
            ],
        )

    def test_channel_without_an_identifier_produces_no_row(self):
        person = local_person(NEW_PERSON, "jordan-bravo", channels=("gmail",), primary_email="")
        plan = plan_for([share_row(NEW_PERSON, "jordan-bravo")], [person], cloud_state())
        self.assertEqual(plan.sources_insert, ())

    def test_allowed_operator_ids_unions_this_operator_onto_the_existing_ones(self):
        plan = plan_for(
            [share_row(NEW_PERSON, "jordan-bravo")],
            [local_person(NEW_PERSON, "jordan-bravo")],
            cloud_state(operator_ids_by_person={NEW_PERSON: (OTHER_OPERATOR,)}),
        )
        self.assertEqual(plan.allowed_operator_ids[NEW_PERSON], tuple(sorted((OPERATOR, OTHER_OPERATOR))))


class PostgresSqlTests(unittest.TestCase):
    def test_persons_upsert_keeps_every_existing_cloud_value(self):
        # The cloud owns a person it already has: fill NULLs, never replace.
        for column in CLOUD_PERSONS_COLUMNS:
            self.assertIn(f"{column} = COALESCE(persons.{column}, EXCLUDED.{column})",
                          postgres.PERSONS_UPSERT_SQL)

    def test_source_upsert_refreshes_counts_on_conflict(self):
        cur = FakeCursor()
        postgres.upsert_sources(cur, OPERATOR, [SourceRow(NEW_PERSON, "gmail", "casey@example.com", 7, "")])
        sql, params = cur.statements[0]
        self.assertIn("ON CONFLICT (operator_id, person_id, source_channel, source_identifier) DO UPDATE", sql)
        # Another discovery method's row with the same key keeps its own counts.
        self.assertIn("WHERE operator_person_sources.discovery_method = EXCLUDED.discovery_method", sql)
        self.assertEqual(params[:6], (OPERATOR, NEW_PERSON, "gmail", "casey@example.com", "powerpacks", 7))

    def test_operator_ids_by_person_groups_sorted_strings(self):
        cur = FakeCursor([[(NEW_PERSON, OTHER_OPERATOR), (NEW_PERSON, OPERATOR)]])
        self.assertEqual(postgres.fetch_operator_ids_by_person(cur, [NEW_PERSON]),
                         {NEW_PERSON: tuple(sorted((OPERATOR, OTHER_OPERATOR)))})

    def test_tag_put_keeps_an_existing_person_id(self):
        cur = FakeCursor()
        postgres.put_tags(cur, OPERATOR, [TagRow(CLOUD_PERSON, "casey-lane")])
        sql, _ = cur.statements[0]
        self.assertIn("DO UPDATE SET person_id = COALESCE(EXCLUDED.person_id, contact_tags.person_id)", sql)


class TurbopufferWriterTests(unittest.TestCase):
    def test_upsert_batches_at_500(self):
        ns = FakeNamespace()
        rows = [{"id": str(index)} for index in range(1001)]
        self.assertEqual(turbopuffer_writer.upsert_docs(ns, "people", rows), 1001)
        self.assertEqual([len(call["upsert_rows"]) for call in ns.writes], [500, 500, 1])
        self.assertEqual(ns.writes[0]["distance_metric"], "cosine_distance")

    def test_patch_writes_only_allowed_operator_ids(self):
        ns = FakeNamespace()
        turbopuffer_writer.patch_allowed_operator_ids(ns, {"doc-1": (OPERATOR,)})
        self.assertEqual(ns.writes, [{
            "patch_rows": [{"id": "doc-1", "allowed_operator_ids": [OPERATOR]}],
            "schema": {"allowed_operator_ids": {"type": "[]string"}},
        }])

    def test_person_doc_ids_come_from_the_cloud_namespace(self):
        ns = FakeNamespace(rows=[mock.Mock(id="doc-1", base_id=CLOUD_PERSON),
                                 mock.Mock(id="doc-2", base_id=CLOUD_PERSON)])
        self.assertEqual(turbopuffer_writer.fetch_person_doc_ids(ns, "people", [CLOUD_PERSON]),
                         {CLOUD_PERSON: ("doc-1", "doc-2")})
        self.assertEqual(ns.queries[0]["filters"], ("base_id", "In", [CLOUD_PERSON]))

    def test_each_person_namespace_addresses_documents_by_its_own_key(self):
        keys = []
        for logical in ("people", "summaries", "education"):
            ns = FakeNamespace()
            turbopuffer_writer.fetch_person_doc_ids(ns, logical, [CLOUD_PERSON])
            keys.append(ns.queries[0]["filters"][0])
        self.assertEqual(keys, ["base_id", "id", "person_id"])

    def test_missing_namespace_reports_no_live_attributes(self):
        ns = mock.Mock(schema=mock.Mock(side_effect=turbopuffer.NotFoundError(
            "missing", response=mock.Mock(status_code=404, headers={}), body=None)))
        self.assertEqual(turbopuffer_writer.live_attributes(ns), frozenset())


class NamespaceResolutionTests(unittest.TestCase):
    def test_staging_env_selects_the_dev_namespaces(self):
        with mock.patch.dict(os.environ, {"ALEPH_ENV": "staging"}, clear=False):
            names = {namespace.logical: upload_powerset.tp_backend.namespace_name(namespace.logical)
                     for namespace in NAMESPACES}
        self.assertEqual(names, {
            "people": "aleph_people_v1_dev",
            "summaries": "aleph_summaries_v1_dev",
            "education": "aleph_people_education_v1_dev",
            "companies": "aleph_companies_v1_dev",
            "schools": "aleph_education_v1_dev",
        })


class DryRunTests(unittest.TestCase):
    """The dry run must touch neither Postgres nor TurboPuffer."""

    def _fixture(self, root: Path) -> dict[str, Path]:
        db = root / "local-search.duckdb"
        con = duckdb.connect(str(db))
        con.execute("CREATE TABLE local_person_profiles (person_id VARCHAR, public_identifier VARCHAR, full_name VARCHAR)")
        con.execute("INSERT INTO local_person_profiles VALUES (?, 'jordan-bravo', 'Jordan Bravo')", [NEW_PERSON])
        con.execute("CREATE TABLE local_people_positions (id VARCHAR, base_id VARCHAR, company_id VARCHAR, position_title VARCHAR)")
        con.execute("INSERT INTO local_people_positions VALUES ('pos-1', ?, 'company-a', 'Engineer')", [NEW_PERSON])
        con.execute("CREATE TABLE local_companies (id VARCHAR, company_name VARCHAR)")
        con.execute("INSERT INTO local_companies VALUES ('company-a', 'Bravo Labs')")
        con.execute("CREATE TABLE local_summaries (id VARCHAR, base_id VARCHAR, summary VARCHAR)")
        con.execute("CREATE TABLE local_people_education (id VARCHAR, base_id VARCHAR, canonical_education_id VARCHAR)")
        con.execute("CREATE TABLE local_education (id VARCHAR, school_name VARCHAR)")
        con.close()

        people_csv = root / "people.csv"
        people_csv.write_text(
            "id,public_identifier,source_channels,interaction_counts,last_interaction,primary_email,primary_phone\n"
            f"{NEW_PERSON},jordan-bravo,linkedin_csv,,,,\n"
        )
        share_csv = root / "share.csv"
        share_csv.write_text(
            "person_id,public_identifier,share,reason,labels,source,updated_at\n"
            f"{NEW_PERSON},jordan-bravo,yes,,,machine,2026-09-24T00:00:00Z\n"
            f"{CLOUD_PERSON},casey-lane,no,human_private,,human,2026-09-24T00:00:00Z\n"
        )
        return {"db": db, "people_csv": people_csv, "share_csv": share_csv, "out_dir": root / "out"}

    def test_dry_run_selects_only_and_writes_one_manifest(self):
        namespace = FakeNamespace()
        # persons-by-slug answers for the private person only; the other three reads are empty.
        cursor = FakeCursor([[("casey-lane", CLOUD_PERSON)], [], [], []])
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        fake_psycopg2 = mock.Mock(connect=mock.Mock(return_value=connection))

        with tempfile.TemporaryDirectory() as tmp:
            paths = self._fixture(Path(tmp))
            with mock.patch.object(upload_powerset.postgres_client, "ensure_psycopg2", return_value=fake_psycopg2), \
                 mock.patch.object(upload_powerset.postgres_client, "database_url", return_value="postgresql://x"), \
                 mock.patch.object(upload_powerset.postgres_client, "load_env_file"), \
                 mock.patch.object(upload_powerset.tp_backend, "namespace", return_value=namespace), \
                 mock.patch.object(upload_powerset.tp_backend, "namespace_name", side_effect=NAMESPACE_NAMES.get):
                payload = upload_powerset.UploadPowerset(
                    operator_id=OPERATOR, dry_run=True, **paths).run()
            manifest = json.loads(Path(payload["manifest"]).read_text())

        self.assertEqual(namespace.writes, [])
        self.assertEqual([sql.strip().split()[0] for sql, _ in cursor.statements], ["SELECT"] * 4)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["plan"]["persons_upsert"], 1)
        self.assertEqual(payload["plan"]["tags_put"], 1)
        self.assertEqual(payload["plan"]["namespaces"]["companies"]["upsert"], 1)
        self.assertEqual(manifest["plan"]["persons_upsert"], 1)


class ApplyTests(unittest.TestCase):
    def test_apply_counts_written_rows_and_upserts_new_docs_but_patches_existing_docs(self):
        private_person = "55555555-5555-5555-8555-555555555555"
        plan = plan_for(
            [share_row(NEW_PERSON, "jordan-bravo"),
             share_row(CLOUD_PERSON, "casey-lane"),
             share_row(private_person, "private-person", share=False, reason=HUMAN_PRIVATE)],
            [local_person(NEW_PERSON, "jordan-bravo"), local_person(CLOUD_PERSON, "casey-lane")],
            cloud_state(cloud_id_by_person={CLOUD_PERSON: CLOUD_PERSON, private_person: private_person}),
        )
        cursor = FakeCursor(rowcounts=[1, 0, 1, 0, 0, 1])
        namespace = FakeNamespace(rows=[mock.Mock(id="existing-doc", base_id=CLOUD_PERSON,
                                                  person_id=CLOUD_PERSON)])

        with tempfile.TemporaryDirectory() as tmp:
            con = duckdb.connect(str(Path(tmp) / "local-search.duckdb"))
            con.execute("CREATE TABLE local_person_profiles (person_id VARCHAR, public_identifier VARCHAR)")
            con.execute("INSERT INTO local_person_profiles VALUES (?, 'jordan-bravo'), (?, 'casey-lane')",
                        [NEW_PERSON, CLOUD_PERSON])
            con.execute("CREATE TABLE local_people_positions (id VARCHAR, base_id VARCHAR, position_title VARCHAR)")
            con.execute("INSERT INTO local_people_positions VALUES ('new-doc', ?, 'Engineer')", [NEW_PERSON])
            con.execute("CREATE TABLE local_summaries (id VARCHAR, base_id VARCHAR, summary VARCHAR)")
            con.execute("CREATE TABLE local_people_education (id VARCHAR, base_id VARCHAR, person_id VARCHAR)")
            con.execute("CREATE TABLE local_companies (id VARCHAR, company_name VARCHAR)")
            con.execute("CREATE TABLE local_education (id VARCHAR, school_name VARCHAR)")
            uploader = upload_powerset.UploadPowerset(
                db=Path(tmp) / "local-search.duckdb", share_csv=Path(tmp) / "share.csv",
                people_csv=Path(tmp) / "people.csv", operator_id=OPERATOR)
            with mock.patch.object(upload_powerset.tp_backend, "namespace", return_value=namespace):
                result = uploader._apply(con, cursor, plan)
            con.close()

        person_writes = [call for call in namespace.writes if "upsert_rows" in call and
                         any(row["id"] == "new-doc" for row in call["upsert_rows"])]
        patched = [call for call in namespace.writes if "patch_rows" in call]
        self.assertEqual(len(person_writes), 1)
        self.assertTrue(any(row["id"] == "existing-doc" for call in patched for row in call["patch_rows"]))
        self.assertEqual(len([sql for sql, _ in cursor.statements if "INSERT INTO persons" in sql]), 2)
        self.assertEqual(len([sql for sql, _ in cursor.statements if "INSERT INTO operator_person_sources" in sql]), 2)
        self.assertEqual(len([sql for sql, _ in cursor.statements if "INSERT INTO contact_tags" in sql]), 1)
        self.assertEqual((result.persons_upserted, result.sources_inserted, result.tags_put), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
