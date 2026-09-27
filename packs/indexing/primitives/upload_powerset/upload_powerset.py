#!/usr/bin/env python3
"""Push the shared slice of the local search index to Powerset (the cloud hub).

Flow: read the canonical store's `share` table + merged/people.csv +
local-search.duckdb -> read the cloud
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
  2026-09-24: read the share list from SQLite, not share.csv.
  2026-09-24: shared = the three-way share value `yes`; `confirm` rows stay home.
  2026-09-24: created; delegated local reads and centralized namespace contracts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import duckdb
import turbopuffer
from dotenv import dotenv_values

REPO = Path(__file__).resolve().parents[4]
SEARCH_PRIMITIVES = REPO / "packs/search/primitives"
for _path in [REPO, SEARCH_PRIMITIVES / "lib", SEARCH_PRIMITIVES / "shared",
              SEARCH_PRIMITIVES / "turbopuffer"]:
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import postgres_client  # noqa: E402
import turbopuffer_search_backend as tp_backend  # noqa: E402

from packs.ingestion.primitives.common.jsonio import now_iso  # noqa: E402
from packs.ingestion.primitives.deep_context.db.models import ShareDecisionRow  # noqa: E402
from packs.ingestion.primitives.deep_context.db.share_views import share_decisions  # noqa: E402
from packs.ingestion.primitives.deep_context.db.store import open_existing_db  # noqa: E402
from packs.ingestion.primitives.deep_context.shared.common import CANONICAL_DB, load_env  # noqa: E402
from packs.ingestion.schemas.share_schema import SHARE_YES  # noqa: E402
from packs.indexing.primitives.upload_powerset import local_index, postgres, turbopuffer_writer  # noqa: E402
from packs.indexing.primitives.upload_powerset.models import (  # noqa: E402
    CloudState,
    LocalPerson,
    UploadPlan,
    UploadResult,
)
from packs.indexing.primitives.upload_powerset.plan import build_plan  # noqa: E402
from packs.indexing.primitives.upload_powerset.errors import log_error  # noqa: E402
from packs.indexing.primitives.upload_powerset.turbopuffer_writer import NAMESPACES  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402

DEFAULT_DB = REPO / ".powerpacks/search-index/local-search.duckdb"
DEFAULT_PEOPLE_CSV = REPO / ".powerpacks/network-import/merged/people.csv"
DEFAULT_SHARE_DB = REPO / CANONICAL_DB
DEFAULT_OUT_DIR = REPO / ".powerpacks/upload-powerset"

PREVIEW_IDS = 10


class UploadPowerset:
    """Make the cloud state for one operator equal the local share list."""

    def __init__(
        self,
        *,
        db: Path,
        share_db: Path,
        people_csv: Path,
        out_dir: Path = DEFAULT_OUT_DIR,
        operator_id: str | None = None,
        dry_run: bool = True,
        env_file: Path | None = None,
    ) -> None:
        self.db = db
        self.share_db = share_db
        self.people_csv = people_csv
        self.out_dir = out_dir
        self.operator_id = operator_id
        self.dry_run = dry_run
        self.env_file = env_file
        self.manifest_path = out_dir / "manifest.json"
        self._database_url = ""
        self._namespace_names: dict[str, str] = {}
        self._tp_client: Any = None

    def _namespace(self, logical: str) -> Any:
        return self._tp_client.namespace(self._namespace_names[logical])

    def run(self) -> dict[str, Any]:
        started_at = now_iso()
        previous = json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else {}
        payload: dict[str, Any] = {
            "status": "running", "stage": "planning", "dry_run": self.dry_run, "started_at": started_at,
            "progress": {"total": 0, "uploaded": 0, "skipped": 0, "namespaces": {}},
        }
        for key in ("target", "person_hashes", "owned_people", "pending_upserts"):
            if key in previous:
                payload[key] = previous[key]
        self._manifest(payload)
        try:
            return self._run(payload, previous)
        except Exception as exc:
            message = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith(
                ("Upload requires", "no users row")) else "Upload failed; retry to resume"
            payload.update(status="failed", error=message, error_type=type(exc).__name__, finished_at=now_iso())
            if isinstance(exc, turbopuffer.APIStatusError):
                payload["http_status"] = exc.status_code
            self._manifest(payload)
            log_error(self.out_dir, payload["stage"], exc, self.env_file)
            raise

    def _manifest(self, payload: dict[str, Any]) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        pending = self.manifest_path.with_suffix(".tmp")
        pending.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        pending.replace(self.manifest_path)

    def _run(self, payload: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
        config = dict(os.environ)
        if self.env_file:
            config.update({key: value for key, value in dotenv_values(self.env_file).items() if value is not None})
        self._database_url = config.get("DATABASE_URL") or postgres_client.database_url()
        self._namespace_names = {
            ns.logical: tp_backend.namespace_name(ns.logical, config=config) for ns in NAMESPACES
        }
        if any(not name.endswith(("_v3", "_v3_share_test")) for name in self._namespace_names.values()):
            raise RuntimeError("Upload requires the shared TurboPuffer v3 namespaces")
        if not config.get("TURBOPUFFER_API_KEY"):
            raise RuntimeError("Upload requires a TurboPuffer API key")
        self._tp_client = turbopuffer.Turbopuffer(
            api_key=config["TURBOPUFFER_API_KEY"],
            region=config.get("TURBOPUFFER_REGION", tp_backend.DEFAULT_REGION))
        if not self.db.exists():
            raise RuntimeError("Upload requires a local search index; build the index first")
        share_rows = share_decisions(open_existing_db(self.share_db))
        payload["progress"]["total"] = sum(row.share == SHARE_YES and bool(row.public_identifier) for row in share_rows)
        payload["stage"] = "checking_access"
        self._manifest(payload)
        people = {person.person_id: person
                  for person in (LocalPerson.from_csv_row(row) for row in CsvIO.read_dict_rows(self.people_csv))}
        con = duckdb.connect(str(self.db), read_only=True)
        psycopg2 = postgres_client.ensure_psycopg2()
        try:
            with psycopg2.connect(self._database_url) as conn:
                with conn.cursor() as cur:
                    postgres.verify_v3_schema(cur)
                    operator_id = self.operator_id or postgres.resolve_operator_id(
                        cur, postgres_client.credentials_subject())
                    plan = self._plan(con, cur, operator_id, share_rows, people, payload)
                    target = {"postgres_host": urlparse(self._database_url).hostname,
                              "postgres_database": urlparse(self._database_url).path,
                              "postgres_user": urlparse(self._database_url).username,
                              "postgres_schema": "powerset_v2", "operator_id": operator_id,
                              "namespaces": {ns.logical: ns.namespace for ns in plan.namespaces}}
                    indexed = {profile.id for profile in local_index.person_profiles(con, plan.persons_upsert)}
                    if missing := set(plan.persons_upsert) - indexed:
                        raise RuntimeError(f"Upload requires a current local index; {len(missing)} shared people lack profiles")
                    for ns in plan.namespaces:
                        if ns.logical != "schools" or not ns.upsert_ids:
                            continue
                        found = {row[0] for row in con.execute(
                            "SELECT id FROM local_education WHERE id = ANY(?)", [list(ns.upsert_ids)]).fetchall()}
                        if missing := set(ns.upsert_ids) - found:
                            raise RuntimeError(f"Upload requires a current local index; {len(missing)} {ns.logical} lack rows")
                    payload["stage"] = "checking_changes"
                    self._manifest(payload)
                    hashes = local_index.person_hashes(con, plan.persons_upsert)
                    same_target = previous.get("target") == target
                    old_hashes = previous.get("person_hashes", {}) if same_target else {}
                    owned_people = set(previous.get("owned_people", ())) if same_target else set()
                    newly_owned = set(next(ns.upsert_ids for ns in plan.namespaces if ns.logical == "people"))
                    changed = tuple(person_id for person_id in plan.persons_upsert
                                    if old_hashes.get(person_id) != hashes[person_id]
                                    and (person_id in owned_people or person_id in newly_owned))
                    retry_upserts = previous.get("pending_upserts", {}) if previous.get("target") == target else {}
                    shared_ids = set(plan.persons_upsert)
                    namespaces = []
                    for ns in plan.namespaces:
                        if ns.logical in {"people", "summaries", "education"}:
                            extra = (set(retry_upserts.get(ns.logical, ())) & shared_ids) | (set(changed) & owned_people)
                            namespaces.append(replace(ns, upsert_ids=tuple(sorted(set(ns.upsert_ids) | extra)),
                                                      patch_person_ids=tuple(sorted(set(ns.patch_person_ids) - extra))))
                        else:
                            namespaces.append(ns)
                    plan = replace(plan, namespaces=tuple(namespaces))
                    preview = plan_preview(plan)
                    preview["previously_uploaded"] = len(shared_ids & old_hashes.keys())
                    preview["companies_skipped_no_row"] = local_index.count_missing_companies(
                        con, plan.persons_upsert)
                    payload.update(operator_id=operator_id, plan=preview)
                    if not self.dry_run:
                        payload["target"] = target
                        payload["person_hashes"] = old_hashes
                        payload["owned_people"] = sorted(owned_people | newly_owned)
                        payload["pending_upserts"] = {ns.logical: ns.upsert_ids for ns in plan.namespaces}
                    payload["progress"] = {"total": len(plan.persons_upsert), "uploaded": 0,
                                           "skipped": len(plan.persons_upsert) - len(changed), "namespaces": {}}
                    self._manifest(payload)
                    result = UploadResult() if self.dry_run else self._apply(con, cur, plan, changed, payload)
                    if not self.dry_run:
                        payload["stage"] = "committing"
                        self._manifest(payload)
                if self.dry_run:
                    conn.rollback()
        finally:
            con.close()

        payload.update(status="completed", stage="completed", result=asdict(result), finished_at=now_iso())
        if not self.dry_run:
            payload["person_hashes"] = hashes
            payload.pop("pending_upserts", None)
            payload["progress"]["uploaded"] = result.people_uploaded
            payload["progress"]["skipped"] = max(0, len(plan.persons_upsert) - result.people_uploaded)
        self._manifest(payload)
        return payload | {"manifest": str(self.manifest_path)}

    def _plan(self, con: Any, cur: Any, operator_id: str, share_rows: tuple[ShareDecisionRow, ...],
              people: dict[str, LocalPerson], payload: dict[str, Any]) -> UploadPlan:
        payload["stage"] = "checking_people"
        self._manifest(payload)
        with_slug = [row for row in share_rows if row.public_identifier]
        shared_ids = sorted(row.person_id for row in with_slug if row.share == SHARE_YES)
        # Every slug, shared or not: a private person the cloud already has is
        # the one who needs the tag.
        cloud_ids = postgres.fetch_cloud_ids_by_slug(cur, sorted(row.public_identifier for row in with_slug))
        cloud_id_by_person = {
            row.person_id: cloud_ids[row.public_identifier] for row in with_slug if row.public_identifier in cloud_ids
        }
        company_ids_by_person = local_index.entity_ids_by_person(con, "companies", shared_ids)
        school_ids_by_person = local_index.entity_ids_by_person(con, "schools", shared_ids)
        namespace_names = self._namespace_names
        present_entity_ids = {}
        for logical, by_person in (("companies", company_ids_by_person), ("schools", school_ids_by_person)):
            payload["stage"] = f"checking_{logical}"
            self._manifest(payload)
            present_entity_ids[logical] = turbopuffer_writer.fetch_present_ids(
                self._namespace(logical),
                sorted({entity_id for ids in by_person.values() for entity_id in ids}),
            )
        cloud = CloudState(
            cloud_id_by_person=cloud_id_by_person,
            operator_sources=postgres.fetch_operator_sources(cur, operator_id),
            operator_source_keys=postgres.fetch_operator_source_keys(cur, operator_id),
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

    def _apply(self, con: Any, cur: Any, plan: UploadPlan,
               changed: tuple[str, ...] | None = None, payload: dict[str, Any] | None = None) -> UploadResult:
        changed = plan.persons_upsert if changed is None else changed
        profiles = local_index.person_profiles(con, changed)
        persons_upserted = postgres.upsert_persons(cur, profiles)
        sources_inserted = postgres.upsert_sources(cur, plan.operator_id, plan.sources_insert)
        sources_deleted = postgres.delete_sources(cur, plan.operator_id, plan.sources_delete)

        # The plan's allowed map is keyed by cloud id; re-read those after the writes.
        touched = sorted(plan.allowed_operator_ids)
        written = postgres.fetch_operator_ids_by_person(cur, touched)
        allowed = {person_id: written.get(person_id, ()) for person_id in touched}
        local_allowed = {local_id: allowed.get(cloud_id, ())
                         for local_id, cloud_id in plan.cloud_id_by_person.items()}
        local_by_cloud = {cloud_id: local_id for local_id, cloud_id in plan.cloud_id_by_person.items()}
        uploaded_people = set(changed)
        uploaded_people.update(local_by_cloud[row.person_id] for row in plan.sources_insert
                               if row.person_id in local_by_cloud)

        docs_upserted: dict[str, int] = {}
        docs_patched: dict[str, int] = {}
        for namespace_plan in plan.namespaces:
            if payload is not None:
                payload["stage"] = namespace_plan.logical
                self._manifest(payload)
            ns = self._namespace(namespace_plan.logical)
            rows = local_index.namespace_rows(con, namespace_plan.logical, namespace_plan.upsert_ids,
                                              local_allowed, plan.operator_id,
                                              turbopuffer_writer.live_attributes(ns))
            if namespace_plan.logical in {"people", "summaries", "education"}:
                for row in rows:
                    if namespace_plan.logical == "people":
                        row["base_id"] = plan.cloud_id_by_person.get(str(row["base_id"]), str(row["base_id"]))
                    elif namespace_plan.logical == "summaries":
                        row["id"] = plan.cloud_id_by_person.get(str(row["id"]), str(row["id"]))
                    else:
                        row["person_id"] = plan.cloud_id_by_person.get(str(row["person_id"]), str(row["person_id"]))
            docs_upserted[namespace_plan.logical] = turbopuffer_writer.upsert_docs(
                ns, namespace_plan.logical, rows)
            if namespace_plan.logical in {"people", "summaries", "education"} and rows:
                uploaded_people.update(namespace_plan.upsert_ids)
            if namespace_plan.patch_person_ids:
                doc_ids = turbopuffer_writer.fetch_person_doc_ids(
                    ns, namespace_plan.logical, namespace_plan.patch_person_ids)
                if namespace_plan.logical in {"people", "summaries", "education"}:
                    local_rows = local_index.namespace_rows(
                        con, namespace_plan.logical,
                        tuple(local_by_cloud.get(person_id, person_id) for person_id in namespace_plan.patch_person_ids),
                        local_allowed,
                        plan.operator_id, turbopuffer_writer.live_attributes(ns))
                    key = {"people": "base_id", "summaries": "id", "education": "person_id"}[namespace_plan.logical]
                    by_person: dict[str, list[dict[str, Any]]] = {}
                    for row in local_rows:
                        cloud_id = plan.cloud_id_by_person.get(str(row[key]), str(row[key]))
                        row[key] = cloud_id
                        by_person.setdefault(cloud_id, []).append(row)
                    for person_id in namespace_plan.patch_person_ids:
                        if person_id not in local_by_cloud:
                            continue
                        local_rows = by_person.get(person_id, [])
                        present = set(doc_ids.get(person_id, ()))
                        local_ids = {str(row["id"]) for row in local_rows}
                        if present and not (present & local_ids):
                            continue
                        missing = [row for row in local_rows if str(row["id"]) not in present]
                        docs_upserted[namespace_plan.logical] += turbopuffer_writer.upsert_docs(
                            ns, namespace_plan.logical, missing)
                        if missing:
                            uploaded_people.add(local_by_cloud[person_id])
                desired_acl = {doc_id: allowed.get(person_id, ())
                               for person_id, ids in doc_ids.items() for doc_id in ids}
                current_acl = turbopuffer_writer.fetch_allowed_operator_ids(ns, sorted(desired_acl))
                docs_patched[namespace_plan.logical] = turbopuffer_writer.patch_allowed_operator_ids(
                    ns, {doc_id: operators for doc_id, operators in desired_acl.items()
                         if current_acl.get(doc_id) != operators})
                if docs_patched[namespace_plan.logical]:
                    uploaded_people.update(local_by_cloud[person_id] for person_id, ids in doc_ids.items()
                                           if person_id in local_by_cloud and any(
                                               current_acl.get(doc_id) != desired_acl[doc_id] for doc_id in ids))
            if payload is not None:
                payload["progress"]["namespaces"] = {
                    logical: {"upserted": docs_upserted.get(logical, 0), "patched": docs_patched.get(logical, 0)}
                    for logical in docs_upserted
                }
                self._manifest(payload)

        tags_put = postgres.put_tags(cur, plan.operator_id, plan.tags_put)
        tags_deleted = postgres.delete_tags(cur, plan.operator_id, plan.tags_delete)
        return UploadResult(
            people_uploaded=len(uploaded_people),
            persons_upserted=persons_upserted,
            sources_inserted=sources_inserted,
            sources_deleted=sources_deleted,
            tags_put=tags_put,
            tags_deleted=tags_deleted,
            docs_upserted=docs_upserted,
            docs_patched=docs_patched,
        )


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


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--share-db", default=str(DEFAULT_SHARE_DB),
                        help="canonical deep-context store holding the share table")
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--operator-id", default=None, help="override the credentials-derived users.id")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--apply", action="store_true",
                        help="execute the plan against Postgres + TurboPuffer; without it: plan only, write nothing")
    args = parser.parse_args()

    payload = UploadPowerset(
        db=Path(args.db),
        share_db=Path(args.share_db),
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
