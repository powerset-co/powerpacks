"""Tests for packs/indexing/primitives/upload_powerset."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import duckdb
import turbopuffer

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from packs.indexing.lib.identity import stable_person_id
from packs.indexing.primitives.upload_powerset import postgres, turbopuffer_writer, upload_powerset
from packs.indexing.primitives.upload_powerset.models import (
    PersonProfile,
    CloudState,
    LocalPerson,
    SourceRow,
    TagRow,
)
from packs.indexing.primitives.upload_powerset.plan import build_plan
from packs.indexing.primitives.upload_powerset.turbopuffer_writer import NAMESPACES
from packs.ingestion.primitives.deep_context_v2.db import queries, queries_share
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
from packs.ingestion.primitives.share.models import ShareDecisionRow
from packs.indexing.primitives.upload_powerset.manifest import UploadManifest
from packs.ingestion.schemas.share_schema import (
    FAMILY,
    HUMAN_PRIVATE,
    HUMAN_SHARE,
    SHARE_CONFIRM,
    SHARE_NO,
    SHARE_YES,
    WORTH_NO,
)

OPERATOR = "00000000-0000-0000-0000-0000000000aa"
OTHER_OPERATOR = "00000000-0000-0000-0000-0000000000bb"
# A LinkedIn person's id everywhere outside the store is uuid5 of linkedin:<slug> (indexing/lib/identity.py):
# the local index, the cloud and the uploader's share rows agree on it.
NEW_PERSON = stable_person_id(public_identifier="jordan-bravo")
CLOUD_PERSON = stable_person_id(public_identifier="casey-lane")
NO_SLUG_PERSON = "33333333-3333-5333-8333-333333333333"
STALE_PERSON = stable_person_id(public_identifier="riley-echo")

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


def share_row(person_id: str, slug: str, *, share: str = SHARE_YES, reason: str = "",
              labels: str = "") -> ShareDecisionRow:
    return ShareDecisionRow(person_id, slug, share, reason, labels, "machine", "2026-09-24T00:00:00Z")


def share_db(root: Path, rows: list[ShareDecisionRow]) -> Path:
    """A v2 store whose `share` table carries `rows`: one candidate per person, each its own parent."""
    path = root / "deep-context-v2.sqlite"
    conn = open_store(path)
    now = "2026-09-24T00:00:00Z"
    people = sorted({row.person_id for row in rows})
    queries.upsert_candidates(conn, [(person_id, f"Person {index}", 0, "{}", now) for index, person_id in enumerate(people)])
    conn.executemany("INSERT INTO candidate_parent (candidate_id, parent_id, reason, verdict_ref, created_at) VALUES (?, ?, 'singleton', NULL, ?)",
                     [(person_id, person_id, now) for person_id in people])
    conn.commit()
    queries_share.replace_share(conn, [], [(row.person_id, row.public_identifier or "", row.share, row.reason, row.labels,
                                            row.source, row.updated_at) for row in rows])
    conn.close()
    return path


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
        operator_source_keys=kwargs.pop("operator_source_keys", frozenset()),
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


class StatefulNamespace(FakeNamespace):
    def __init__(self):
        super().__init__()
        self.docs = {}

    def schema(self):
        return {key: {} for key in ("id", "base_id", "person_id", "position_title",
                                   "company_name", "summary", "allowed_operator_ids")}

    def write(self, **kwargs):
        super().write(**kwargs)
        for row in kwargs.get("upsert_rows", ()):
            self.docs[str(row["id"])] = dict(row)
        for row in kwargs.get("patch_rows", ()):
            self.docs[str(row["id"])].update(row)

    def query(self, **kwargs):
        self.queries.append(kwargs)
        key, _, wanted = kwargs["filters"]
        rows = [mock.Mock(**row) for row in self.docs.values()
                if str(row.get(key)) in wanted]
        return mock.Mock(rows=rows)


class ShareFamilyTests(unittest.TestCase):
    def test_share_and_search_read_separate_settings(self):
        with mock.patch.dict(os.environ, {"ALEPH_INDEX_VERSION": "v3"}):
            os.environ.pop("POWERPACKS_SHARE_INDEX_VERSION", None)
            self.assertEqual(upload_powerset.share_namespace("summaries"), "powerpacks_summaries_v1")
            self.assertEqual(postgres.share_schema(upload_powerset.share_version()), "powerset_share_v1")
            self.assertEqual(upload_powerset.tp_backend.namespace_name("summaries"), "aleph_summaries_v3")
        with mock.patch.dict(os.environ, {"ALEPH_INDEX_VERSION": "v3", "POWERPACKS_SHARE_INDEX_VERSION": "v2"}):
            self.assertEqual(upload_powerset.share_namespace("people"), "powerpacks_people_v2")
            self.assertEqual(upload_powerset.tp_backend.namespace_name("people"), "aleph_people_v3")
        with mock.patch.dict(os.environ, {"POWERPACKS_SHARE_INDEX_VERSION": "x; drop"}), \
                self.assertRaises(ValueError):
            upload_powerset.share_version()


class PlanBucketTests(unittest.TestCase):
    def test_missing_local_company_row_is_not_planned_for_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = duckdb.connect(str(Path(tmp) / "local-search.duckdb"))
            con.execute("CREATE TABLE local_people_positions (base_id VARCHAR, company_id VARCHAR)")
            con.execute("CREATE TABLE local_companies (id VARCHAR)")
            con.execute("INSERT INTO local_people_positions VALUES (?, 'company-missing')", [NEW_PERSON])
            by_person = upload_powerset.local_index.entity_ids_by_person(con, "companies", [NEW_PERSON])
            con.close()
        self.assertEqual(by_person, {})

    def test_other_discovery_source_key_is_not_upserted(self):
        key = (CLOUD_PERSON, "linkedin", "casey-lane")
        plan = plan_for([share_row(CLOUD_PERSON, "casey-lane")],
                        [local_person(CLOUD_PERSON, "casey-lane")],
                        cloud_state(cloud_id_by_person={CLOUD_PERSON: CLOUD_PERSON},
                                    operator_source_keys=frozenset({key})))
        self.assertEqual(plan.sources_insert, ())

    def test_unchanged_source_is_not_upserted_again(self):
        source = SourceRow(CLOUD_PERSON, "linkedin", "casey-lane", 0, "")
        plan = plan_for(
            [share_row(CLOUD_PERSON, "casey-lane")],
            [local_person(CLOUD_PERSON, "casey-lane")],
            cloud_state(cloud_id_by_person={CLOUD_PERSON: CLOUD_PERSON}, operator_sources=(source,)),
        )
        self.assertEqual(plan.sources_insert, ())

    def test_new_person_upserts_and_cloud_person_patches(self):
        plan = plan_for(
            [share_row(NEW_PERSON, "jordan-bravo"), share_row(CLOUD_PERSON, "casey-lane")],
            [local_person(NEW_PERSON, "jordan-bravo"), local_person(CLOUD_PERSON, "casey-lane")],
            cloud_state(cloud_id_by_person={CLOUD_PERSON: CLOUD_PERSON}),
        )
        people_ns = next(ns for ns in plan.namespaces if ns.logical == "people")
        self.assertEqual(plan.persons_upsert, tuple(sorted((NEW_PERSON, CLOUD_PERSON))))
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
            [share_row(STALE_PERSON, "casey-lane", share=SHARE_NO, reason=WORTH_NO)],
            [local_person(STALE_PERSON, "casey-lane")],
            cloud_state(operator_sources=(stale,), operator_ids_by_person={STALE_PERSON: (OPERATOR, OTHER_OPERATOR)}),
        )
        summaries = next(ns for ns in plan.namespaces if ns.logical == "summaries")
        self.assertEqual(plan.sources_delete, (stale,))
        self.assertEqual(summaries.patch_person_ids, (STALE_PERSON,))
        self.assertEqual(plan.allowed_operator_ids[STALE_PERSON], (OTHER_OPERATOR,))

    def test_a_confirm_row_is_neither_uploaded_nor_tagged(self):
        stale = SourceRow(CLOUD_PERSON, "gmail", "casey@example.com", 3, "")
        plan = plan_for(
            [share_row(CLOUD_PERSON, "casey-lane", share=SHARE_CONFIRM, reason=FAMILY)],
            [local_person(CLOUD_PERSON, "casey-lane")],
            cloud_state(cloud_id_by_person={CLOUD_PERSON: CLOUD_PERSON}, operator_sources=(stale,)),
        )
        self.assertEqual(plan.persons_upsert, ())
        self.assertEqual(plan.sources_insert, ())
        self.assertEqual(plan.sources_delete, (stale,))
        self.assertEqual(plan.tags_put, ())

    def test_private_person_in_cloud_gets_a_tag_and_an_un_privated_one_loses_it(self):
        plan = plan_for(
            [
                share_row(CLOUD_PERSON, "casey-lane", share=SHARE_NO, reason=HUMAN_PRIVATE),
                share_row(NEW_PERSON, "jordan-bravo"),
            ],
            [local_person(CLOUD_PERSON, "casey-lane"), local_person(NEW_PERSON, "jordan-bravo")],
            cloud_state(
                cloud_id_by_person={CLOUD_PERSON: CLOUD_PERSON},
                private_tag_keys=frozenset({"casey-lane", "jordan-bravo", "someone-cloud-only"}),
            ),
        )
        self.assertEqual(plan.tags_put, ())
        # jordan-bravo's local decision is a machine one, so the cloud tag (a
        # human's word in the Powerset UI) stays; someone-cloud-only is not ours at all.
        self.assertEqual(plan.tags_delete, ())

    def test_a_human_share_tag_drops_the_cloud_private_tag(self):
        plan = plan_for(
            [share_row(NEW_PERSON, "jordan-bravo", reason=HUMAN_SHARE)],
            [local_person(NEW_PERSON, "jordan-bravo")],
            cloud_state(private_tag_keys=frozenset({"jordan-bravo", "someone-cloud-only"})),
        )
        self.assertEqual([row.group_key for row in plan.tags_delete], ["jordan-bravo"])

    def test_a_machine_no_never_tags_the_cloud(self):
        plan = plan_for(
            [share_row(CLOUD_PERSON, "casey-lane", share=SHARE_NO, reason=WORTH_NO)],
            [local_person(CLOUD_PERSON, "casey-lane")],
            cloud_state(cloud_id_by_person={CLOUD_PERSON: CLOUD_PERSON}),
        )
        self.assertEqual(plan.tags_put, ())

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
            [share_row(NEW_PERSON, "jordan-bravo", share=SHARE_NO, reason=HUMAN_PRIVATE)],
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
    def test_equivalent_timestamp_formats_skip_source_update(self):
        local = local_person(CLOUD_PERSON, "casey-lane", last_interaction="2026-09-01T00:00:00Z")
        existing = SourceRow(CLOUD_PERSON, "linkedin", "casey-lane", 0, "2026-09-01 00:00:00+00")
        plan = plan_for([share_row(CLOUD_PERSON, "casey-lane")], [local],
                        cloud_state(cloud_id_by_person={CLOUD_PERSON: CLOUD_PERSON},
                                    operator_sources=(existing,)))
        self.assertEqual(plan.sources_insert, ())

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
        # hydrated_context: see test_the_persons_upsert_fills_an_empty_positions_context.
        for column in [column for column in CLOUD_PERSONS_COLUMNS if column != "hydrated_context"]:
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


class PostgresBatchTests(unittest.TestCase):
    """Writes go in multi-row statements of BATCH_ROWS, not one round trip per row."""

    def _profile(self, n: int) -> PersonProfile:
        return PersonProfile(f"id-{n}", f"slug-{n}", *([None] * 18))

    def test_persons_upsert_sends_one_statement_per_batch(self):
        cur = FakeCursor(rowcounts=[postgres.BATCH_ROWS, 1])
        count = postgres.upsert_persons(cur, [self._profile(n) for n in range(postgres.BATCH_ROWS + 1)])
        self.assertEqual(len(cur.statements), 2)
        first_sql, first_params = cur.statements[0]
        self.assertEqual(first_sql.count("NOW(), NOW())"), postgres.BATCH_ROWS)
        self.assertEqual(len(first_params), postgres.BATCH_ROWS * 25)
        self.assertLess(len(first_params), 65_535)
        self.assertEqual(count, postgres.BATCH_ROWS + 1)

    def test_sources_and_tags_batch_and_sum_affected_rows(self):
        sources = [SourceRow(NEW_PERSON, "gmail", f"p{n}@example.com", n, "") for n in range(1201)]
        cur = FakeCursor(rowcounts=[500, 500, 201])
        self.assertEqual(postgres.upsert_sources(cur, OPERATOR, sources), 1201)
        self.assertEqual(len(cur.statements), 3)
        cur = FakeCursor(rowcounts=[3])
        self.assertEqual(postgres.delete_sources(cur, OPERATOR, sources[:3]), 3)
        sql, params = cur.statements[0]
        self.assertIn("USING (VALUES", sql)
        self.assertEqual(params[-2:], (OPERATOR, "powerpacks"))
        cur = FakeCursor(rowcounts=[2, 2])
        self.assertEqual(postgres.put_tags(cur, OPERATOR, [TagRow(CLOUD_PERSON, "casey-lane")] * 2), 2)
        self.assertEqual(postgres.delete_tags(cur, OPERATOR, [TagRow("", "casey-lane")] * 2), 2)
        self.assertEqual(len(cur.statements), 2)

    def test_nothing_to_write_sends_nothing(self):
        cur = FakeCursor()
        self.assertEqual(postgres.upsert_sources(cur, OPERATOR, []), 0)
        self.assertEqual(cur.statements, [])


class LocalReadChunkTests(unittest.TestCase):
    """Local rows (vectors included) are read a bounded number of people at a time."""

    def _con(self, tmp: str, people: int):
        con = duckdb.connect(str(Path(tmp) / "local-search.duckdb"))
        con.execute("CREATE TABLE local_person_profiles (person_id VARCHAR, public_identifier VARCHAR)")
        con.execute("CREATE TABLE local_people_positions (id VARCHAR, base_id VARCHAR, vector FLOAT[])")
        con.execute("CREATE TABLE local_summaries (id VARCHAR, base_id VARCHAR)")
        con.execute("CREATE TABLE local_people_education (id VARCHAR, base_id VARCHAR)")
        for n in range(people):
            con.execute("INSERT INTO local_person_profiles VALUES (?, ?)", [f"p{n}", f"slug-{n}"])
            con.execute("INSERT INTO local_people_positions VALUES (?, ?, [0.5, 0.25])", [f"doc-{n}", f"p{n}"])
        return con

    def test_hashes_are_the_same_whatever_the_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = self._con(tmp, 5)
            ids = tuple(f"p{n}" for n in range(5))
            whole = upload_powerset.local_index.person_hashes(con, ids)
            reads = []
            execute = con.execute
            with mock.patch.object(upload_powerset.local_index, "PEOPLE_PER_READ", 2):
                chunked = upload_powerset.local_index.person_hashes(
                    mock.Mock(execute=lambda sql, params=None: reads.append(len(params[0])) or execute(sql, params)), ids)
            con.close()
        self.assertEqual(chunked, whole)
        self.assertLessEqual(max(reads), 2)

    def test_documents_are_built_and_written_a_chunk_at_a_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = self._con(tmp, 5)
            ids = tuple(f"p{n}" for n in range(5))
            with mock.patch.object(upload_powerset.local_index, "PEOPLE_PER_READ", 2):
                chunks = list(upload_powerset.local_index.namespace_row_chunks(
                    con, "people", ids, {}, OPERATOR, frozenset()))
            con.close()
        self.assertEqual([len(chunk) for chunk in chunks], [2, 2, 1])
        self.assertEqual(sorted(doc["id"] for chunk in chunks for doc in chunk), [f"doc-{n}" for n in range(5)])


class TurbopufferWriterTests(unittest.TestCase):
    def test_missing_namespace_has_no_present_entities_or_person_docs(self):
        missing = turbopuffer.NotFoundError(
            "missing", response=mock.Mock(status_code=404, headers={}), body=None)
        ns = mock.Mock(query=mock.Mock(side_effect=missing))
        self.assertEqual(turbopuffer_writer.fetch_present_ids(ns, ["company-a"]), frozenset())
        self.assertEqual(turbopuffer_writer.fetch_person_doc_ids(ns, "people", [CLOUD_PERSON]), {})

    def test_summary_document_uses_person_id_in_cloud(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = duckdb.connect(str(Path(tmp) / "local-search.duckdb"))
            con.execute("CREATE TABLE local_summaries (id VARCHAR, base_id VARCHAR, summary VARCHAR)")
            con.execute("INSERT INTO local_summaries VALUES ('local-summary', ?, 'Engineer')", [CLOUD_PERSON])
            rows = upload_powerset.local_index.namespace_rows(
                con, "summaries", (CLOUD_PERSON,), {CLOUD_PERSON: (OPERATOR,)}, OPERATOR, frozenset())
            con.close()
        self.assertEqual(rows[0]["id"], CLOUD_PERSON)

    def test_identical_allowed_operator_ids_need_no_patch(self):
        ns = FakeNamespace(rows=[mock.Mock(id="doc-1", allowed_operator_ids=[OPERATOR])])
        current = turbopuffer_writer.fetch_allowed_operator_ids(ns, ["doc-1"])
        self.assertEqual(current, {"doc-1": (OPERATOR,)})
        self.assertEqual(turbopuffer_writer.patch_allowed_operator_ids(
            ns, {doc_id: operators for doc_id, operators in {"doc-1": (OPERATOR,)}.items()
                 if current.get(doc_id) != operators}), 0)
        self.assertEqual(ns.writes, [])

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
            f"{STALE_PERSON},riley-echo,linkedin_csv,,,,\n"
        )
        canonical_db = share_db(root, [
            share_row(NEW_PERSON, "jordan-bravo"),
            share_row(CLOUD_PERSON, "casey-lane", share=SHARE_NO, reason=HUMAN_PRIVATE),
            share_row(STALE_PERSON, "riley-echo", share=SHARE_CONFIRM, reason=FAMILY, labels="is_family"),
        ])
        return {"db": db, "people_csv": people_csv, "share_db": canonical_db, "out_dir": root / "out"}

    def test_dry_run_selects_only_and_writes_one_manifest(self):
        namespace = FakeNamespace()
        # persons-by-slug answers for the private person only; the other three reads are empty.
        # The SET search_path answers nothing; the schema check reads the next row.
        cursor = FakeCursor([[], [('powerset_share_v1', 'powerset_share_v1, powerset_v2, pg_catalog')], [("casey-lane", CLOUD_PERSON)], [], [], []])
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        fake_psycopg2 = mock.Mock(connect=mock.Mock(return_value=connection))

        with tempfile.TemporaryDirectory() as tmp:
            paths = self._fixture(Path(tmp))
            paths["out_dir"].mkdir()
            (paths["out_dir"] / "manifest.json").write_text(json.dumps({
                "status": "completed", "target": {"postgres_host": "another-index"},
                "person_hashes": {NEW_PERSON: "previously-uploaded"},
            }))
            with mock.patch.object(upload_powerset.postgres_client, "ensure_psycopg2", return_value=fake_psycopg2), \
                 mock.patch.object(upload_powerset.postgres_client, "database_url", return_value="postgresql://x"), \
                 mock.patch.object(upload_powerset.postgres_client, "load_env_file"), \
                 mock.patch.object(upload_powerset.turbopuffer, "Turbopuffer", return_value=mock.Mock(namespace=mock.Mock(return_value=namespace))), \
                 mock.patch.object(upload_powerset.tp_backend, "namespace_name", side_effect=lambda logical, **kwargs: NAMESPACE_NAMES[logical].replace('_v1', '_share_v1')), \
                 mock.patch.dict(os.environ, {"TURBOPUFFER_API_KEY": "test-key"}):
                payload = upload_powerset.UploadPowerset(
                    operator_id=OPERATOR, dry_run=True, **paths).run()
            manifest = json.loads(Path(payload["manifest"]).read_text())

        self.assertEqual(namespace.writes, [])
        self.assertEqual([sql.strip().split()[0] for sql, _ in cursor.statements], ["SET"] + ["SELECT"] * 6)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["plan"]["previously_uploaded"], 0)
        self.assertEqual(manifest["person_hashes"], {NEW_PERSON: "previously-uploaded"})
        # The `yes` row only: the human's `private` and the machine's `confirm` stay home.
        self.assertEqual(payload["plan"]["persons_upsert_ids"], [NEW_PERSON])
        self.assertEqual(payload["plan"]["persons_upsert"], 1)
        self.assertEqual(payload["plan"]["tags_put"], 1)
        self.assertEqual(payload["plan"]["namespaces"]["companies"]["upsert"], 1)
        self.assertEqual(manifest["plan"]["persons_upsert"], 1)

    def test_a_failed_first_upload_leaves_new_people_new_on_the_next_check(self):
        # The failed run recorded Jordan as owned before its writes rolled back; the cloud
        # still lacks Jordan, so the next check counts one new person, not a changed one.
        plan = plan_for([share_row(NEW_PERSON, "jordan-bravo")],
                        [local_person(NEW_PERSON, "jordan-bravo")], cloud_state())
        plan = replace(plan, namespaces=tuple(replace(ns, namespace=ns.namespace.replace("_v1", "_share_v1"))
                                              for ns in plan.namespaces))
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._fixture(Path(tmp))
            connection = mock.MagicMock()
            connection.__enter__.return_value = connection
            connection.cursor.return_value.__enter__.return_value = FakeCursor()
            fake_psycopg2 = mock.Mock(connect=mock.Mock(return_value=connection))
            with mock.patch.object(upload_powerset.postgres_client, "ensure_psycopg2", return_value=fake_psycopg2), \
                 mock.patch.object(upload_powerset.postgres_client, "database_url", return_value="postgresql://user@host/db"), \
                 mock.patch.object(postgres, "use_share_schema"), \
                 mock.patch.object(upload_powerset.turbopuffer, "Turbopuffer"), \
                 mock.patch.object(upload_powerset.tp_backend, "namespace_name",
                                   side_effect=lambda logical, **kwargs: NAMESPACE_NAMES[logical].replace("_v1", "_share_v1")), \
                 mock.patch.object(upload_powerset.UploadPowerset, "_plan", return_value=plan), \
                 mock.patch.dict(os.environ, {"TURBOPUFFER_API_KEY": "test-key"}):
                upload_powerset.UploadPowerset(operator_id=OPERATOR, dry_run=True, **paths).run()
                manifest = paths["out_dir"] / "manifest.json"
                checked = UploadManifest.read(manifest)
                target = {"postgres_host": "host", "postgres_database": "/db",
                          "postgres_schema": "powerset_share_v1", "operator_id": OPERATOR,
                          "namespaces": {ns.logical: ns.namespace for ns in plan.namespaces}}
                replace(checked, status="failed", dry_run=False, target=target,
                        owned_people=(NEW_PERSON,)).write(manifest)
                upload_powerset.UploadPowerset(operator_id=OPERATOR, dry_run=True, **paths).run()
            again = json.loads(manifest.read_text())["plan"]
        self.assertEqual((again["new_to_cloud"], again["changed"], again["already_in_cloud"]), (1, 0, 0))

    def test_a_shared_person_the_cloud_has_without_positions_is_rewritten(self):
        # Jordan is in the cloud with an empty-positions context (an upload before the
        # profile fix) and has positions locally: the check counts Jordan as changed.
        plan = plan_for([share_row(NEW_PERSON, "jordan-bravo")], [local_person(NEW_PERSON, "jordan-bravo")],
                        cloud_state(cloud_id_by_person={NEW_PERSON: NEW_PERSON}))
        plan = replace(plan, namespaces=tuple(replace(ns, namespace=ns.namespace.replace("_v1", "_share_v1"))
                                              for ns in plan.namespaces))
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._fixture(Path(tmp))
            con = duckdb.connect(str(paths["db"]))
            con.execute("ALTER TABLE local_people_positions ADD COLUMN IF NOT EXISTS base_id VARCHAR")
            con.close()
            connection = mock.MagicMock()
            connection.__enter__.return_value = connection
            connection.cursor.return_value.__enter__.return_value = FakeCursor()
            fake_psycopg2 = mock.Mock(connect=mock.Mock(return_value=connection))
            with mock.patch.object(upload_powerset.postgres_client, "ensure_psycopg2", return_value=fake_psycopg2), \
                 mock.patch.object(upload_powerset.postgres_client, "database_url", return_value="postgresql://user@host/db"), \
                 mock.patch.object(postgres, "use_share_schema"), \
                 mock.patch.object(postgres, "fetch_ids_without_positions", return_value=frozenset({NEW_PERSON})), \
                 mock.patch.object(upload_powerset.turbopuffer, "Turbopuffer"), \
                 mock.patch.object(upload_powerset.tp_backend, "namespace_name",
                                   side_effect=lambda logical, **kwargs: NAMESPACE_NAMES[logical].replace("_v1", "_share_v1")), \
                 mock.patch.object(upload_powerset.UploadPowerset, "_plan", return_value=plan), \
                 mock.patch.dict(os.environ, {"TURBOPUFFER_API_KEY": "test-key"}):
                upload_powerset.UploadPowerset(operator_id=OPERATOR, dry_run=True, **paths).run()
            saved = json.loads((paths["out_dir"] / "manifest.json").read_text())
        self.assertEqual((saved["plan"]["changed"], saved["plan"]["already_in_cloud"]), (1, 0))
        self.assertEqual(saved["progress"]["skipped"], 0)

    def test_the_persons_upsert_fills_an_empty_positions_context(self):
        self.assertIn("WHEN COALESCE(persons.hydrated_context -> 'positions', '[]'::jsonb) = '[]'::jsonb",
                      postgres.PERSONS_UPSERT_SQL)

    def test_same_network_second_apply_makes_no_cloud_writes(self):
        first = plan_for([share_row(NEW_PERSON, "jordan-bravo")],
                         [local_person(NEW_PERSON, "jordan-bravo")], cloud_state())
        source = SourceRow(NEW_PERSON, "linkedin", "jordan-bravo")
        second = plan_for([share_row(NEW_PERSON, "jordan-bravo")],
                          [local_person(NEW_PERSON, "jordan-bravo")],
                          cloud_state(cloud_id_by_person={NEW_PERSON: NEW_PERSON},
                                      operator_sources=(source,)))
        first = replace(first, namespaces=tuple(replace(ns, namespace=ns.namespace.replace("_v1", "_share_v1"))
                                                for ns in first.namespaces))
        second = replace(second, namespaces=tuple(replace(ns, namespace=ns.namespace.replace("_v1", "_share_v1"))
                                                  for ns in second.namespaces))
        namespaces = {logical: StatefulNamespace() for logical in NAMESPACE_NAMES}
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        fake_psycopg2 = mock.Mock(connect=mock.Mock(return_value=connection))
        writes = []
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._fixture(Path(tmp))
            with mock.patch.object(upload_powerset.postgres_client, "ensure_psycopg2", return_value=fake_psycopg2), \
                 mock.patch.object(upload_powerset.postgres_client, "database_url", return_value="postgresql://user@host/db"), \
                 mock.patch.object(postgres, "use_share_schema"), \
                 mock.patch.object(upload_powerset.turbopuffer, "Turbopuffer", return_value=mock.Mock(namespace=mock.Mock(side_effect=lambda name: namespaces[next(k for k,v in NAMESPACE_NAMES.items() if upload_powerset.share_namespace(k) == name)]))), \
                 mock.patch.object(upload_powerset.tp_backend, "namespace_name", side_effect=lambda logical, **kwargs: NAMESPACE_NAMES[logical].replace('_v1', '_share_v1')), \
                 mock.patch.dict(os.environ, {"TURBOPUFFER_API_KEY": "test-key"}), \
                 mock.patch.object(postgres, "upsert_persons", side_effect=lambda cur, rows: writes.append(("persons", len(rows))) or len(rows)), \
                 mock.patch.object(postgres, "upsert_sources", side_effect=lambda cur, op, rows: writes.append(("sources", len(rows))) or len(rows)), \
                 mock.patch.object(postgres, "delete_sources", return_value=0), \
                 mock.patch.object(postgres, "fetch_operator_ids_by_person", side_effect=lambda cur, ids: {person_id: (OPERATOR,) for person_id in ids}), \
                 mock.patch.object(postgres, "put_tags", return_value=0), \
                 mock.patch.object(postgres, "delete_tags", return_value=0), \
                 mock.patch.object(upload_powerset.UploadPowerset, "_plan", side_effect=[first, second, first]):
                one = upload_powerset.UploadPowerset(operator_id=OPERATOR, dry_run=False, **paths).run()
                before = sum(len(ns.writes) for ns in namespaces.values())
                two = upload_powerset.UploadPowerset(operator_id=OPERATOR, dry_run=False, **paths).run()
                unchanged_writes = sum(len(ns.writes) for ns in namespaces.values())
                restored = upload_powerset.UploadPowerset(operator_id=OPERATOR, dry_run=False, **paths).run()
        self.assertEqual(one["progress"]["uploaded"], 1)
        self.assertEqual(two["progress"]["uploaded"], 0)
        self.assertEqual(two["plan"]["previously_uploaded"], 1)
        self.assertEqual(two["progress"]["skipped"], 1)
        self.assertEqual(unchanged_writes, before)
        self.assertEqual(restored["progress"]["uploaded"], 1)
        self.assertEqual(writes, [("persons", 1), ("sources", 1), ("persons", 0), ("sources", 0),
                                  ("persons", 1), ("sources", 1)])

    def test_failure_after_first_namespace_resumes_then_skips(self):
        initial = plan_for([share_row(NEW_PERSON, "jordan-bravo")],
                           [local_person(NEW_PERSON, "jordan-bravo")], cloud_state())
        source = SourceRow(NEW_PERSON, "linkedin", "jordan-bravo")
        settled = plan_for([share_row(NEW_PERSON, "jordan-bravo")],
                           [local_person(NEW_PERSON, "jordan-bravo")],
                           cloud_state(cloud_id_by_person={NEW_PERSON: NEW_PERSON},
                                       operator_sources=(source,)))
        def v3(plan):
            return replace(plan, namespaces=tuple(replace(ns, namespace=ns.namespace.replace("_v1", "_share_v1"))
                                                   for ns in plan.namespaces))

        namespaces = {logical: StatefulNamespace() for logical in NAMESPACE_NAMES}
        summaries = namespaces["summaries"]
        original_write = summaries.write
        failure = [True]

        def fail_once(**kwargs):
            if failure[0]:
                failure[0] = False
                raise ConnectionError("temporary network failure")
            return original_write(**kwargs)

        summaries.write = fail_once
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        cursor = FakeCursor()
        connection.cursor.return_value.__enter__.return_value = cursor
        fake_psycopg2 = mock.Mock(connect=mock.Mock(return_value=connection))
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._fixture(Path(tmp))
            local = duckdb.connect(str(paths["db"]))
            local.execute("INSERT INTO local_summaries VALUES (?, ?, 'Engineer')", [NEW_PERSON, NEW_PERSON])
            local.close()
            with mock.patch.object(upload_powerset.postgres_client, "ensure_psycopg2", return_value=fake_psycopg2), \
                 mock.patch.object(upload_powerset.postgres_client, "database_url", return_value="postgresql://user@host/db"), \
                 mock.patch.object(postgres, "use_share_schema"), \
                 mock.patch.object(upload_powerset.turbopuffer, "Turbopuffer", return_value=mock.Mock(namespace=mock.Mock(side_effect=lambda name: namespaces[next(k for k,v in NAMESPACE_NAMES.items() if upload_powerset.share_namespace(k) == name)]))), \
                 mock.patch.object(upload_powerset.tp_backend, "namespace_name", side_effect=lambda logical, **kwargs: NAMESPACE_NAMES[logical].replace('_v1', '_share_v1')), \
                 mock.patch.dict(os.environ, {"TURBOPUFFER_API_KEY": "test-key"}), \
                 mock.patch.object(postgres, "upsert_persons", side_effect=lambda cur, rows: len(rows)), \
                 mock.patch.object(postgres, "upsert_sources", side_effect=lambda cur, op, rows: len(rows)), \
                 mock.patch.object(postgres, "delete_sources", return_value=0), \
                 mock.patch.object(postgres, "fetch_operator_ids_by_person", side_effect=lambda cur, ids: {person_id: (OPERATOR,) for person_id in ids}), \
                 mock.patch.object(postgres, "put_tags", return_value=0), \
                 mock.patch.object(postgres, "delete_tags", return_value=0), \
                 mock.patch.object(upload_powerset.UploadPowerset, "_plan", side_effect=[v3(initial), v3(initial), v3(settled)]):
                with self.assertRaises(ConnectionError):
                    upload_powerset.UploadPowerset(operator_id=OPERATOR, dry_run=False, **paths).run()
                failed = json.loads((paths["out_dir"] / "manifest.json").read_text())
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["pending_upserts"]["people"], [NEW_PERSON])
                ids_after_failure = set(namespaces["people"].docs)
                resumed = upload_powerset.UploadPowerset(operator_id=OPERATOR, dry_run=False, **paths).run()
                writes_before_third = sum(len(ns.writes) for ns in namespaces.values())
                final = upload_powerset.UploadPowerset(operator_id=OPERATOR, dry_run=False, **paths).run()
        self.assertEqual(set(namespaces["people"].docs), ids_after_failure)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(final["progress"]["uploaded"], 0)
        self.assertEqual(sum(len(ns.writes) for ns in namespaces.values()), writes_before_third)

    def test_dry_run_other_target_keeps_previous_upload_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            original = {"status": "completed", "target": {"postgres_host": "original"},
                        "person_hashes": {NEW_PERSON: "digest"}, "owned_people": [NEW_PERSON]}
            (out / "manifest.json").write_text(json.dumps(original))
            uploader = upload_powerset.UploadPowerset(
                db=out / "db", share_db=out / "share", people_csv=out / "people",
                out_dir=out, dry_run=True)

            def other_target(payload, previous):
                replacement = replace(payload, status="completed", plan={"persons_upsert": 0})
                replacement.write(uploader.manifest_path)
                return replacement

            with mock.patch.object(uploader, "_run", side_effect=other_target):
                uploader.run()
            manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["target"], original["target"])
        self.assertEqual(manifest["person_hashes"], original["person_hashes"])
        self.assertEqual(manifest["owned_people"], original["owned_people"])

    def test_failed_upload_retains_pending_docs_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            uploader = upload_powerset.UploadPowerset(
                db=out / "db", share_db=out / "share", people_csv=out / "people",
                out_dir=out, dry_run=False)
            pending = {"people": [NEW_PERSON], "summaries": [NEW_PERSON]}
            (out / "manifest.json").write_text(json.dumps({
                "status": "running", "target": {"postgres_host": "host"},
                "pending_upserts": pending, "owned_people": [NEW_PERSON],
                "person_hashes": {NEW_PERSON: "old"},
            }))
            with mock.patch.object(uploader, "_run", side_effect=ConnectionError("temporary network failure")):
                with self.assertRaises(ConnectionError):
                    uploader.run()
            failed = json.loads((out / "manifest.json").read_text())
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["pending_upserts"], pending)
        self.assertEqual(failed["person_hashes"], {NEW_PERSON: "old"})
        self.assertEqual(failed["error"], "Upload failed. Check again to resume.")


class ApplyTests(unittest.TestCase):
    def test_unshared_person_never_repairs_missing_documents(self):
        stale = SourceRow(STALE_PERSON, "linkedin", "riley-echo", 0, "")
        plan = plan_for(
            [share_row(STALE_PERSON, "riley-echo", share=SHARE_NO, reason=WORTH_NO)],
            [local_person(STALE_PERSON, "riley-echo")],
            cloud_state(operator_sources=(stale,)),
        )
        namespace = FakeNamespace()
        with tempfile.TemporaryDirectory() as tmp:
            con = duckdb.connect(str(Path(tmp) / "local-search.duckdb"))
            con.execute("CREATE TABLE local_person_profiles (person_id VARCHAR, public_identifier VARCHAR)")
            con.execute("CREATE TABLE local_people_positions (id VARCHAR, base_id VARCHAR, position_title VARCHAR)")
            con.execute("INSERT INTO local_people_positions VALUES ('private-position', ?, 'Engineer')", [STALE_PERSON])
            con.execute("CREATE TABLE local_summaries (id VARCHAR, base_id VARCHAR, summary VARCHAR)")
            con.execute("CREATE TABLE local_people_education (id VARCHAR, base_id VARCHAR, person_id VARCHAR)")
            con.execute("CREATE TABLE local_companies (id VARCHAR, company_name VARCHAR)")
            con.execute("CREATE TABLE local_education (id VARCHAR, school_name VARCHAR)")
            uploader = upload_powerset.UploadPowerset(
                db=Path(tmp) / "local-search.duckdb", share_db=Path(tmp) / "deep-context.sqlite",
                people_csv=Path(tmp) / "people.csv", operator_id=OPERATOR, out_dir=Path(tmp) / "out")
            uploader._tp_client = mock.Mock(namespace=mock.Mock(return_value=namespace))
            uploader._namespace_names = {logical: logical for logical in NAMESPACE_NAMES}
            with mock.patch.object(upload_powerset.tp_backend, "namespace", return_value=namespace):
                result = uploader._apply(con, FakeCursor(), plan)
            con.close()
        self.assertFalse(any("upsert_rows" in call for call in namespace.writes))
        self.assertEqual(result.people_uploaded, 1)

    def test_existing_cloud_person_with_no_namespace_docs_is_repaired(self):
        plan = plan_for(
            [share_row(CLOUD_PERSON, "casey-lane")],
            [local_person(CLOUD_PERSON, "casey-lane")],
            cloud_state(cloud_id_by_person={CLOUD_PERSON: CLOUD_PERSON}),
        )
        namespace = FakeNamespace()
        with tempfile.TemporaryDirectory() as tmp:
            con = duckdb.connect(str(Path(tmp) / "local-search.duckdb"))
            con.execute("CREATE TABLE local_person_profiles (person_id VARCHAR, public_identifier VARCHAR)")
            con.execute("INSERT INTO local_person_profiles VALUES (?, 'casey-lane')", [CLOUD_PERSON])
            con.execute("CREATE TABLE local_people_positions (id VARCHAR, base_id VARCHAR, position_title VARCHAR)")
            con.execute("INSERT INTO local_people_positions VALUES ('local-position', ?, 'Engineer')", [CLOUD_PERSON])
            con.execute("CREATE TABLE local_summaries (id VARCHAR, base_id VARCHAR, summary VARCHAR)")
            con.execute("CREATE TABLE local_people_education (id VARCHAR, base_id VARCHAR, person_id VARCHAR)")
            con.execute("CREATE TABLE local_companies (id VARCHAR, company_name VARCHAR)")
            con.execute("CREATE TABLE local_education (id VARCHAR, school_name VARCHAR)")
            uploader = upload_powerset.UploadPowerset(
                db=Path(tmp) / "local-search.duckdb", share_db=Path(tmp) / "deep-context.sqlite",
                people_csv=Path(tmp) / "people.csv", operator_id=OPERATOR, out_dir=Path(tmp) / "out")
            uploader._tp_client = mock.Mock(namespace=mock.Mock(return_value=namespace))
            uploader._namespace_names = {logical: logical for logical in NAMESPACE_NAMES}
            with mock.patch.object(upload_powerset.tp_backend, "namespace", return_value=namespace):
                result = uploader._apply(con, FakeCursor(), plan)
            con.close()
        self.assertEqual(result.docs_upserted["people"], 1)

    def test_missing_docs_for_people_already_in_the_cloud_go_in_one_write(self):
        # Two people the cloud has, neither with position docs: one people-namespace write, not one each.
        plan = plan_for(
            [share_row(NEW_PERSON, "jordan-bravo"), share_row(CLOUD_PERSON, "casey-lane")],
            [local_person(NEW_PERSON, "jordan-bravo"), local_person(CLOUD_PERSON, "casey-lane")],
            cloud_state(cloud_id_by_person={NEW_PERSON: NEW_PERSON, CLOUD_PERSON: CLOUD_PERSON}),
        )
        namespace = FakeNamespace()
        with tempfile.TemporaryDirectory() as tmp:
            con = duckdb.connect(str(Path(tmp) / "local-search.duckdb"))
            con.execute("CREATE TABLE local_person_profiles (person_id VARCHAR, public_identifier VARCHAR)")
            con.execute("CREATE TABLE local_people_positions (id VARCHAR, base_id VARCHAR, position_title VARCHAR)")
            con.execute("INSERT INTO local_people_positions VALUES ('pos-a', ?, 'Engineer'), ('pos-b', ?, 'Founder')",
                        [NEW_PERSON, CLOUD_PERSON])
            con.execute("CREATE TABLE local_summaries (id VARCHAR, base_id VARCHAR, summary VARCHAR)")
            con.execute("CREATE TABLE local_people_education (id VARCHAR, base_id VARCHAR, person_id VARCHAR)")
            con.execute("CREATE TABLE local_companies (id VARCHAR, company_name VARCHAR)")
            con.execute("CREATE TABLE local_education (id VARCHAR, school_name VARCHAR)")
            uploader = upload_powerset.UploadPowerset(
                db=Path(tmp) / "local-search.duckdb", share_db=Path(tmp) / "deep-context.sqlite",
                people_csv=Path(tmp) / "people.csv", operator_id=OPERATOR, out_dir=Path(tmp) / "out")
            uploader._tp_client = mock.Mock(namespace=mock.Mock(return_value=namespace))
            uploader._namespace_names = {logical: logical for logical in NAMESPACE_NAMES}
            with mock.patch.object(turbopuffer_writer, "fetch_allowed_operator_ids", return_value={}):
                uploader._apply(con, FakeCursor(), plan, changed=())
            con.close()
        position_writes = [call["upsert_rows"] for call in namespace.writes
                           if "upsert_rows" in call and {row["id"] for row in call["upsert_rows"]} & {"pos-a", "pos-b"}]
        self.assertEqual([sorted(row["id"] for row in rows) for rows in position_writes], [["pos-a", "pos-b"]])

    def test_apply_counts_written_rows_and_upserts_new_docs_but_patches_existing_docs(self):
        private_person = "55555555-5555-5555-8555-555555555555"
        plan = plan_for(
            [share_row(NEW_PERSON, "jordan-bravo"),
             share_row(CLOUD_PERSON, "casey-lane"),
             share_row(private_person, "private-person", share=SHARE_NO, reason=HUMAN_PRIVATE)],
            [local_person(NEW_PERSON, "jordan-bravo"), local_person(CLOUD_PERSON, "casey-lane")],
            cloud_state(cloud_id_by_person={CLOUD_PERSON: CLOUD_PERSON, private_person: private_person}),
        )
        # One statement per table: both people in one persons upsert, one of them affected.
        cursor = FakeCursor(rowcounts=[1, 1, 1])
        namespace = FakeNamespace(rows=[mock.Mock(id="existing-doc", base_id=CLOUD_PERSON,
                                                  person_id=CLOUD_PERSON, allowed_operator_ids=[])])

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
                db=Path(tmp) / "local-search.duckdb", share_db=Path(tmp) / "deep-context.sqlite",
                people_csv=Path(tmp) / "people.csv", operator_id=OPERATOR, out_dir=Path(tmp) / "out")
            uploader._tp_client = mock.Mock(namespace=mock.Mock(return_value=namespace))
            uploader._namespace_names = {logical: logical for logical in NAMESPACE_NAMES}
            with mock.patch.object(upload_powerset.tp_backend, "namespace", return_value=namespace), \
                 mock.patch.object(turbopuffer_writer, "fetch_allowed_operator_ids", return_value={}):
                result = uploader._apply(con, cursor, plan)
            con.close()

        person_writes = [call for call in namespace.writes if "upsert_rows" in call and
                         any(row["id"] == "new-doc" for row in call["upsert_rows"])]
        patched = [call for call in namespace.writes if "patch_rows" in call]
        self.assertEqual(len(person_writes), 1)
        self.assertTrue(any(row["id"] == "existing-doc" for call in patched for row in call["patch_rows"]))
        persons = [params for sql, params in cursor.statements if "INSERT INTO persons" in sql]
        self.assertEqual([len(params) for params in persons], [2 * 25])
        self.assertEqual(len([sql for sql, _ in cursor.statements if "INSERT INTO operator_person_sources" in sql]), 1)
        self.assertEqual(len([sql for sql, _ in cursor.statements if "INSERT INTO contact_tags" in sql]), 1)
        self.assertEqual((result.persons_upserted, result.sources_inserted, result.tags_put), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
