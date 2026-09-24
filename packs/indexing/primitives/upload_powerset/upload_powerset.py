#!/usr/bin/env python3
"""Push the shared slice of the local search index to Powerset (the cloud hub).

Flow: read share.csv + merged/people.csv + local-search.duckdb -> read the cloud
state for those people (persons rows, this operator's powerpacks source rows,
every operator that can see them, this operator's private tags, which
company/school docs already exist) -> plan.build_plan reconciles the two ->
--dry-run emits the plan and stops; --apply upserts persons, reconciles
operator_person_sources, re-reads the authoritative allowed_operator_ids, writes
or patches the five TurboPuffer namespaces, then puts/deletes contact_tags.
Either way one manifest lands in .powerpacks/upload-powerset/.

Reconcile, not append: the cloud state for THIS operator is made equal to the
share list. An un-share removes the operator from allowed_operator_ids and
deletes its source rows; documents are never deleted.

Changelog:
  2026-09-24: created.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import duckdb

REPO = Path(__file__).resolve().parents[4]
SEARCH_PRIMITIVES = REPO / "packs/search/primitives"
for _path in [REPO, SEARCH_PRIMITIVES / "lib", SEARCH_PRIMITIVES / "shared",
              SEARCH_PRIMITIVES / "local", SEARCH_PRIMITIVES / "turbopuffer"]:
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import postgres_client  # noqa: E402
import turbopuffer_search_backend as tp_backend  # noqa: E402

from packs.indexing.lib.contracts import contract_attribute_names, load_search_contract, vector_metadata  # noqa: E402
from packs.ingestion.primitives.common.jsonio import now_iso  # noqa: E402
from packs.indexing.primitives.upload_powerset import postgres, turbopuffer_writer  # noqa: E402
from packs.indexing.primitives.upload_powerset.models import (  # noqa: E402
    CloudState,
    LocalPerson,
    PersonProfile,
    ShareRow,
    UploadPlan,
    UploadResult,
)
from packs.indexing.primitives.upload_powerset.plan import ENTITY_NAMESPACES, PERSON_NAMESPACES, build_plan  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402

DEFAULT_DB = REPO / ".powerpacks/search-index/local-search.duckdb"
DEFAULT_PEOPLE_CSV = REPO / ".powerpacks/network-import/merged/people.csv"
DEFAULT_SHARE_CSV = REPO / ".powerpacks/share/share.csv"
DEFAULT_OUT_DIR = REPO / ".powerpacks/upload-powerset"

LOGICAL_NAMESPACES = PERSON_NAMESPACES + ENTITY_NAMESPACES
NAMESPACE_TABLE = {
    "people": "local_people_positions",
    "summaries": "local_summaries",
    "education": "local_people_education",
    "companies": "local_companies",
    "schools": "local_education",
}
PREVIEW_IDS = 10


class UploadPowerset:
    """Make the cloud state for one operator equal the local share list."""

    def __init__(
        self,
        *,
        db: Path,
        share_csv: Path,
        people_csv: Path,
        out_dir: Path = DEFAULT_OUT_DIR,
        operator_id: str | None = None,
        dry_run: bool = True,
        env_file: Path | None = None,
    ) -> None:
        self.db = db
        self.share_csv = share_csv
        self.people_csv = people_csv
        self.out_dir = out_dir
        self.operator_id = operator_id
        self.dry_run = dry_run
        self.env_file = env_file
        self.manifest_path = out_dir / "manifest.json"

    def run(self) -> dict[str, Any]:
        postgres_client.load_env_file(self.env_file)
        share_rows = tuple(ShareRow.from_csv_row(row) for row in CsvIO.read_dict_rows(self.share_csv))
        people = {person.person_id: person
                  for person in (LocalPerson.from_csv_row(row) for row in CsvIO.read_dict_rows(self.people_csv))}
        con = duckdb.connect(str(self.db), read_only=True)
        psycopg2 = postgres_client.ensure_psycopg2()
        started_at = now_iso()
        try:
            with psycopg2.connect(postgres_client.database_url()) as conn:
                with conn.cursor() as cur:
                    operator_id = self.operator_id or postgres.resolve_operator_id(
                        cur, postgres_client.credentials_subject())
                    plan = self._plan(con, cur, operator_id, share_rows, people)
                    result = UploadResult() if self.dry_run else self._apply(con, cur, plan)
                if self.dry_run:
                    conn.rollback()
        finally:
            con.close()

        payload = {
            "status": "completed",
            "dry_run": self.dry_run,
            "operator_id": plan.operator_id,
            # ALEPH_ENV redirects TurboPuffer only; there is one Postgres. Both targets are named here.
            "postgres_host": urlparse(postgres_client.database_url()).hostname,
            "namespaces": {ns.logical: ns.namespace for ns in plan.namespaces},
            "plan": plan_preview(plan),
            "result": result_payload(result),
            "started_at": started_at,
            "finished_at": now_iso(),
        }
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return payload | {"manifest": str(self.manifest_path)}

    def _plan(self, con: Any, cur: Any, operator_id: str, share_rows: tuple[ShareRow, ...],
              people: dict[str, LocalPerson]) -> UploadPlan:
        with_slug = [row for row in share_rows if row.public_identifier]
        shared_ids = sorted(row.person_id for row in with_slug if row.share)
        # Every slug, shared or not: a private person the cloud already has is
        # the one who needs the tag.
        cloud_ids = postgres.fetch_cloud_ids_by_slug(cur, sorted(row.public_identifier for row in with_slug))
        cloud_id_by_person = {
            row.person_id: cloud_ids[row.public_identifier] for row in with_slug if row.public_identifier in cloud_ids
        }
        company_ids_by_person = self._entity_ids_by_person(con, "companies", shared_ids)
        school_ids_by_person = self._entity_ids_by_person(con, "schools", shared_ids)
        namespace_names = {logical: tp_backend.namespace_name(logical) for logical in LOGICAL_NAMESPACES}
        present_entity_ids = {
            logical: turbopuffer_writer.fetch_present_ids(
                tp_backend.namespace(logical),
                sorted({entity_id for ids in by_person.values() for entity_id in ids}),
            )
            for logical, by_person in
            (("companies", company_ids_by_person), ("schools", school_ids_by_person))
        }
        cloud = CloudState(
            cloud_id_by_person=cloud_id_by_person,
            operator_sources=postgres.fetch_operator_sources(cur, operator_id),
            operator_ids_by_person=postgres.fetch_operator_ids_by_person(
                cur, sorted(cloud_id_by_person.get(person_id, person_id) for person_id in shared_ids)),
            private_tag_keys=postgres.fetch_private_tag_keys(cur, operator_id),
            present_entity_ids=present_entity_ids,
        )
        return build_plan(
            operator_id=operator_id,
            share_rows=share_rows,
            people=people,
            cloud=cloud,
            namespace_names=namespace_names,
            company_ids_by_person=company_ids_by_person,
            school_ids_by_person=school_ids_by_person,
        )

    def _apply(self, con: Any, cur: Any, plan: UploadPlan) -> UploadResult:
        profiles = self._person_profiles(con, plan.persons_upsert)
        persons_upserted = postgres.upsert_persons(cur, profiles)
        sources_inserted = postgres.upsert_sources(cur, plan.operator_id, plan.sources_insert)
        sources_deleted = postgres.delete_sources(cur, plan.operator_id, plan.sources_delete)

        # The plan's allowed map is keyed by cloud id; re-read those after the writes.
        touched = sorted(plan.allowed_operator_ids)
        written = postgres.fetch_operator_ids_by_person(cur, touched)
        allowed = {person_id: written.get(person_id, ()) for person_id in touched}

        docs_upserted: dict[str, int] = {}
        docs_patched: dict[str, int] = {}
        for namespace_plan in plan.namespaces:
            ns = tp_backend.namespace(namespace_plan.logical)
            rows = self._namespace_rows(con, namespace_plan.logical, namespace_plan.upsert_ids,
                                        allowed, plan.operator_id,
                                        turbopuffer_writer.live_attributes(ns))
            docs_upserted[namespace_plan.logical] = turbopuffer_writer.upsert_docs(
                ns, namespace_plan.logical, rows)
            if not namespace_plan.patch_person_ids:
                continue
            doc_ids = turbopuffer_writer.fetch_person_doc_ids(
                ns, namespace_plan.logical, namespace_plan.patch_person_ids)
            docs_patched[namespace_plan.logical] = turbopuffer_writer.patch_allowed_operator_ids(
                ns,
                {doc_id: allowed.get(person_id, ())
                 for person_id, ids in doc_ids.items() for doc_id in ids},
            )

        tags_put = postgres.put_tags(cur, plan.operator_id, plan.tags_put)
        tags_deleted = postgres.delete_tags(cur, plan.operator_id, plan.tags_delete)
        return UploadResult(
            persons_upserted=persons_upserted,
            sources_inserted=sources_inserted,
            sources_deleted=sources_deleted,
            tags_put=tags_put,
            tags_deleted=tags_deleted,
            docs_upserted=docs_upserted,
            docs_patched=docs_patched,
        )

    def _entity_ids_by_person(self, con: Any, logical: str, person_ids: list[str]) -> dict[str, tuple[str, ...]]:
        if logical == "companies":
            sql = """
                SELECT p.base_id, p.company_id FROM local_people_positions p
                JOIN local_companies c ON c.id = p.company_id
                WHERE p.base_id = ANY(?)
            """
        else:
            sql = """
                SELECT e.base_id, e.canonical_education_id FROM local_people_education e
                JOIN local_education s ON s.id = e.canonical_education_id
                WHERE e.base_id = ANY(?)
            """
        by_person: dict[str, set[str]] = {}
        for person_id, entity_id in con.execute(sql, [person_ids]).fetchall():
            by_person.setdefault(str(person_id), set()).add(str(entity_id))
        return {person_id: tuple(sorted(ids)) for person_id, ids in by_person.items()}

    def _person_profiles(self, con: Any, person_ids: tuple[str, ...]) -> list[PersonProfile]:
        rows = con.execute(
            "SELECT * FROM local_person_profiles WHERE person_id = ANY(?) ORDER BY person_id",
            [list(person_ids)],
        )
        columns = [column[0] for column in rows.description]
        return [PersonProfile.from_db_row(dict(zip(columns, row))) for row in rows.fetchall()]

    def _namespace_rows(self, con: Any, logical: str, ids: tuple[str, ...],
                        allowed: dict[str, tuple[str, ...]], operator_id: str,
                        live: frozenset[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        table = NAMESPACE_TABLE[logical]
        columns = self._doc_columns(con, logical, table, live)
        # The person key is selected last and never written: the live person
        # namespaces do not all carry it (aleph_summaries_v1 and
        # aleph_people_education_v1 have no base_id attribute).
        key = "base_id" if logical in PERSON_NAMESPACES else "id"
        select = ", ".join(f't."{column}"' for column in columns)
        rows = con.execute(
            f'SELECT {select}, t."{key}" FROM {table} t WHERE t."{key}" = ANY(?) ORDER BY t."id"',
            [list(ids)],
        )
        names = [column[0] for column in rows.description][:-1]
        docs = []
        for row in rows.fetchall():
            doc = {name: value for name, value in zip(names, row) if value is not None}
            if logical in PERSON_NAMESPACES:
                doc["allowed_operator_ids"] = list(allowed.get(str(row[-1]), ()))
            elif logical == "companies":
                doc["allowed_operator_ids"] = [operator_id]
            docs.append(doc)
        return docs

    def _doc_columns(self, con: Any, logical: str, table: str, live: frozenset[str]) -> list[str]:
        """Contract columns the local table has AND the live namespace holds.

        The contracts are a superset of the live namespaces (aleph_people_v1 has
        no company_* or *_followers attributes, aleph_companies_v1 no aliases),
        so intersecting with the live schema is what keeps an upload from
        widening a shared index. An empty `live` means the namespace does not
        exist yet, and the contract is then the only available shape.
        """
        contract = load_search_contract(f"turbopuffer/{logical}.namespace.json")
        names = ["id"] + [name for name in contract_attribute_names(contract) if name != "id"]
        if vector_metadata(contract) is not None:
            names.append("vector")
        columns = {row[0] for row in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]).fetchall()}
        allowed = columns if not live else columns & (live | {"id"})
        return [name for name in names if name in allowed]


def plan_preview(plan: UploadPlan) -> dict[str, Any]:
    """Counts plus the first ids per bucket. Ids and counts only — a source row's
    identifier is an email or a phone number, and a person without a LinkedIn is
    keyed by one, so neither leaves the machine."""
    preview = plan.counts()
    preview["persons_upsert_ids"] = list(plan.persons_upsert[:PREVIEW_IDS])
    preview["sources_insert_keys"] = [f"{row.person_id}:{row.source_channel}"
                                      for row in plan.sources_insert[:PREVIEW_IDS]]
    preview["sources_delete_keys"] = [f"{row.person_id}:{row.source_channel}"
                                      for row in plan.sources_delete[:PREVIEW_IDS]]
    preview["tags_put_person_ids"] = [row.person_id for row in plan.tags_put[:PREVIEW_IDS]]
    preview["namespace_ids"] = {
        ns.logical: {"upsert": list(ns.upsert_ids[:PREVIEW_IDS]), "patch": list(ns.patch_person_ids[:PREVIEW_IDS])}
        for ns in plan.namespaces
    }
    return preview


def result_payload(result: UploadResult) -> dict[str, Any]:
    return {
        "persons_upserted": result.persons_upserted,
        "sources_inserted": result.sources_inserted,
        "sources_deleted": result.sources_deleted,
        "tags_put": result.tags_put,
        "tags_deleted": result.tags_deleted,
        "docs_upserted": result.docs_upserted,
        "docs_patched": result.docs_patched,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--share-csv", default=str(DEFAULT_SHARE_CSV))
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--operator-id", default=None, help="override the credentials-derived users.id")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--apply", action="store_true",
                        help="execute the plan against Postgres + TurboPuffer; without it: plan only, write nothing")
    args = parser.parse_args()

    payload = UploadPowerset(
        db=Path(args.db),
        share_csv=Path(args.share_csv),
        people_csv=Path(args.people_csv),
        out_dir=Path(args.out_dir),
        operator_id=args.operator_id,
        dry_run=not args.apply,
        env_file=Path(args.env_file) if args.env_file else None,
    ).run()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
