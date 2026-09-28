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
The People route requires an unchanged completed check before applying. The CLI
keeps its explicit --apply path. Either way one manifest lands in
.powerpacks/upload-powerset/.

Reconcile, not append: the cloud state for THIS operator is made equal to the
share list. An un-share removes the operator from allowed_operator_ids and
deletes its source rows; documents are never deleted.

Changelog:
  2026-09-27: read os.environ only; .env is loaded once by the caller's entry point.
  2026-09-27: bind real runs to checked decisions and target; report typed stages.
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
from packs.indexing.primitives.upload_powerset.errors import SAFE_ERRORS, log_error, safe_error  # noqa: E402
from packs.indexing.primitives.upload_powerset.manifest import (  # noqa: E402
    CHANGED_CHECK, CHECK_FAILED, UPLOAD_FAILED, CheckChanged, Stage, UploadManifest, share_digest,
)
from packs.indexing.primitives.upload_powerset.turbopuffer_writer import NAMESPACES, NAMESPACE_BY_LOGICAL  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402

DEFAULT_DB = REPO / ".powerpacks/search-index/local-search.duckdb"
DEFAULT_PEOPLE_CSV = REPO / ".powerpacks/network-import/merged/people.csv"
DEFAULT_SHARE_DB = REPO / CANONICAL_DB
DEFAULT_OUT_DIR = REPO / ".powerpacks/upload-powerset"
# The namespace family the shared cloud is served from; the upload never targets another.
UPLOAD_INDEX_VERSION = "v3"

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
        require_checked: bool = False,
    ) -> None:
        self.db = db
        self.share_db = share_db
        self.people_csv = people_csv
        self.out_dir = out_dir
        self.operator_id = operator_id
        self.dry_run = dry_run
        self.require_checked = require_checked
        self.manifest_path = out_dir / "manifest.json"
        self._database_url = ""
        self._namespace_names: dict[str, str] = {}
        self._tp_client: Any = None

    def _namespace(self, logical: str) -> Any:
        return self._tp_client.namespace(self._namespace_names[logical])

    def run(self) -> dict[str, Any]:
        started_at = now_iso()
        previous = UploadManifest.read(self.manifest_path)
        current = replace(previous, status="running", stage=Stage.PLANNING,
                          dry_run=self.dry_run, started_at=started_at, finished_at=None,
                          error=None, error_type=None, progress=UploadManifest().progress,
                          plan=previous.plan)
        current.write(self.manifest_path)
        try:
            return self._run(current, previous)
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            current = UploadManifest.read(self.manifest_path)
            if isinstance(exc, CheckChanged):
                # Refused before any write: the last upload stands and there is nothing to log.
                replace(current, status="failed", error=CHANGED_CHECK, error_type=type(exc).__name__,
                        finished_at=now_iso()).write(self.manifest_path)
                raise
            message = safe_error(exc, CHECK_FAILED if self.dry_run else UPLOAD_FAILED)
            current = replace(current, status="failed", error=message,
                              error_type=type(exc).__name__, finished_at=now_iso())
            if isinstance(exc, turbopuffer.APIStatusError):
                current = replace(current, http_status=exc.status_code)
            if not self.dry_run:
                current = replace(current, last_upload={"finished_at": current.finished_at,
                    "status": "failed", "uploaded": current.progress["uploaded"],
                    "skipped": current.progress["skipped"]})
            current.write(self.manifest_path)
            log_error(self.out_dir, current.stage or Stage.PLANNING, exc)
            raise

    def _run(self, current: UploadManifest, previous: UploadManifest) -> dict[str, Any]:
        # The server (cmd_serve) and the CLI (main) each load .env once, at start.
        config = dict(os.environ)
        self._database_url = postgres_client.database_url()
        # The shared cloud the upload writes is the v3 family, whatever version the search
        # side reads; a per-namespace override still points a rehearsal at _v3_share_test.
        self._namespace_names = {
            ns.logical: tp_backend.namespace_name(
                ns.logical, config={**config, "ALEPH_INDEX_VERSION": UPLOAD_INDEX_VERSION})
            for ns in NAMESPACES
        }
        suffixes = {"_v3_share_test" if name.endswith("_v3_share_test") else
                    "_v3" if name.endswith("_v3") else "invalid"
                    for name in self._namespace_names.values()}
        if len(suffixes) != 1 or "invalid" in suffixes:
            raise RuntimeError(SAFE_ERRORS["namespace"])
        if not config.get("TURBOPUFFER_API_KEY"):
            raise RuntimeError(SAFE_ERRORS["api_key"])
        self._tp_client = turbopuffer.Turbopuffer(
            api_key=config["TURBOPUFFER_API_KEY"],
            region=config.get("TURBOPUFFER_REGION", tp_backend.DEFAULT_REGION))
        if not self.db.exists():
            raise RuntimeError(SAFE_ERRORS["local_index"])
        share_rows = share_decisions(open_existing_db(self.share_db))
        digest = share_digest(share_rows)
        current = current.at(Stage.CHECKING_ACCESS, share_digest=digest, progress={
            **current.progress, "total": sum(row.share == SHARE_YES and bool(row.public_identifier)
                                                for row in share_rows)})
        current.write(self.manifest_path)
        people = {person.person_id: person
                  for person in (LocalPerson.from_csv_row(row) for row in CsvIO.read_dict_rows(self.people_csv))}
        con = duckdb.connect(str(self.db), read_only=True)
        psycopg2 = postgres_client.ensure_psycopg2()
        try:
            with psycopg2.connect(self._database_url) as conn:
                with conn.cursor() as cur:
                    postgres.use_v3_schema(cur)
                    operator_id = self.operator_id or postgres.resolve_operator_id(
                        cur, postgres_client.credentials_subject())
                    plan = self._plan(con, cur, operator_id, share_rows, people)
                    target = {"postgres_host": urlparse(self._database_url).hostname,
                              "postgres_database": urlparse(self._database_url).path,
                              "postgres_schema": "powerset_v2", "operator_id": operator_id,
                              "namespaces": {ns.logical: ns.namespace for ns in plan.namespaces}}
                    indexed = {profile.id for profile in local_index.person_profiles(con, plan.persons_upsert)}
                    if missing := set(plan.persons_upsert) - indexed:
                        raise RuntimeError(f"Upload requires a current local index; {len(missing)} shared people lack profiles")
                    current = current.at(Stage.CHECKING_CHANGES)
                    current.write(self.manifest_path)
                    hashes = local_index.person_hashes(con, plan.persons_upsert)
                    same_target = previous.target == target
                    old_hashes = previous.person_hashes if same_target else {}
                    owned_people = set(previous.owned_people) if same_target else set()
                    newly_owned = set(next(ns.upsert_ids for ns in plan.namespaces if ns.logical == "people"))
                    # Owned people whose content moved; new people are written too but counted as new.
                    changed = tuple(person_id for person_id in plan.persons_upsert
                                    if person_id in owned_people and old_hashes.get(person_id) != hashes[person_id])
                    to_write = tuple(sorted(set(changed) | newly_owned))
                    retry_upserts = previous.pending_upserts if same_target else {}
                    shared_ids = set(plan.persons_upsert)
                    namespaces = []
                    for ns in plan.namespaces:
                        if NAMESPACE_BY_LOGICAL[ns.logical].person_grain:
                            extra = (set(retry_upserts.get(ns.logical, ())) & shared_ids) | set(changed)
                            namespaces.append(replace(ns, upsert_ids=tuple(sorted(set(ns.upsert_ids) | extra)),
                                                      patch_person_ids=tuple(sorted(set(ns.patch_person_ids) - extra))))
                        else:
                            namespaces.append(ns)
                    plan = replace(plan, namespaces=tuple(namespaces))
                    preview = plan_preview(plan)
                    preview["previously_uploaded"] = len(shared_ids & old_hashes.keys())
                    preview["companies_skipped_no_row"] = local_index.count_missing_companies(
                        con, plan.persons_upsert)
                    preview["schools_skipped_no_row"] = local_index.count_missing_schools(
                        con, plan.persons_upsert)
                    preview.update(marked_share=sum(row.share == SHARE_YES for row in share_rows),
                                   with_linkedin=len(plan.persons_upsert),
                                   without_linkedin=len(plan.skipped_no_linkedin),
                                   new_to_cloud=len(newly_owned), changed=len(changed),
                                   already_shared=len(shared_ids & old_hashes.keys()) - len(set(changed) & old_hashes.keys()),
                                   losing_access=len({row.person_id for row in plan.sources_delete} -
                                                     set(plan.cloud_id_by_person.values())),
                                   companies_missing=preview["companies_skipped_no_row"])
                    # In the cloud through another operator: this upload adds you as a source.
                    preview["already_in_cloud"] = (len(shared_ids) - len(newly_owned) - len(changed)
                                                   - preview["already_shared"])
                    if self.require_checked and not self.dry_run and (previous.plan is None or previous.checked_target != target
                                             or previous.plan != preview or previous.share_digest != digest):
                        raise CheckChanged(CHANGED_CHECK)
                    current = replace(current, operator_id=operator_id, plan=preview,
                                      checked_target=target if self.dry_run else previous.checked_target)
                    if not self.dry_run:
                        current = replace(current, target=target, person_hashes=old_hashes,
                            owned_people=tuple(sorted(owned_people | newly_owned)),
                            pending_upserts={ns.logical: ns.upsert_ids for ns in plan.namespaces})
                    total = len(plan.persons_upsert) + preview["losing_access"]
                    current = replace(current, progress={"total": total, "uploaded": 0,
                                            "skipped": total - len(to_write), "namespaces": {}})
                    current.write(self.manifest_path)
                    result = UploadResult() if self.dry_run else self._apply(con, cur, plan, to_write)
                    if not self.dry_run:
                        current = UploadManifest.read(self.manifest_path).at(Stage.COMMITTING)
                        current.write(self.manifest_path)
                if self.dry_run:
                    conn.rollback()
        finally:
            con.close()

        current = UploadManifest.read(self.manifest_path).at(Stage.COMPLETED, status="completed",
                                                               result=asdict(result), finished_at=now_iso())
        if not self.dry_run:
            progress = {**current.progress, "uploaded": result.people_uploaded,
                        "skipped": max(0, current.progress["total"] - result.people_uploaded)}
            current = replace(current, person_hashes=hashes, pending_upserts={}, progress=progress,
                last_upload={"finished_at": current.finished_at, "status": "completed",
                             "uploaded": progress["uploaded"], "skipped": progress["skipped"]})
        current.write(self.manifest_path)
        return asdict(current) | {"manifest": str(self.manifest_path)}

    def _plan(self, con: Any, cur: Any, operator_id: str, share_rows: tuple[ShareDecisionRow, ...],
              people: dict[str, LocalPerson]) -> UploadPlan:
        UploadManifest.read(self.manifest_path).at(Stage.CHECKING_PEOPLE).write(self.manifest_path)
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
            stage = Stage.CHECKING_COMPANIES if logical == "companies" else Stage.CHECKING_SCHOOLS
            UploadManifest.read(self.manifest_path).at(stage).write(self.manifest_path)
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
               changed: tuple[str, ...] | None = None) -> UploadResult:
        changed = plan.persons_upsert if changed is None else changed
        newly_owned = set(next(ns.upsert_ids for ns in plan.namespaces if ns.logical == "people"))
        UploadManifest.read(self.manifest_path).at(Stage.WRITING_PEOPLE).write(self.manifest_path)
        profiles = local_index.person_profiles(con, tuple(sorted(set(changed) | newly_owned)))
        persons_upserted = postgres.upsert_persons(cur, profiles)
        sources_inserted = postgres.upsert_sources(cur, plan.operator_id, plan.sources_insert)
        sources_deleted = postgres.delete_sources(cur, plan.operator_id, plan.sources_delete)
        tags_put = postgres.put_tags(cur, plan.operator_id, plan.tags_put)
        tags_deleted = postgres.delete_tags(cur, plan.operator_id, plan.tags_delete)

        # The plan's allowed map is keyed by cloud id; re-read those after the writes.
        touched = sorted(plan.allowed_operator_ids)
        written = postgres.fetch_operator_ids_by_person(cur, touched)
        allowed = {person_id: written.get(person_id, ()) for person_id in touched}
        local_allowed = {local_id: allowed.get(cloud_id, ())
                         for local_id, cloud_id in plan.cloud_id_by_person.items()}
        local_by_cloud = {cloud_id: local_id for local_id, cloud_id in plan.cloud_id_by_person.items()}
        uploaded_people = ({local_by_cloud[row.person_id] for row in plan.sources_insert
                            if row.person_id in local_by_cloud} if sources_inserted else set())
        if sources_deleted:
            uploaded_people.update(local_by_cloud.get(row.person_id, row.person_id)
                                   for row in plan.sources_delete)

        docs_upserted: dict[str, int] = {}
        docs_patched: dict[str, int] = {}
        for namespace_plan in plan.namespaces:
            UploadManifest.read(self.manifest_path).at(Stage(namespace_plan.logical)).write(self.manifest_path)
            ns = self._namespace(namespace_plan.logical)
            live = turbopuffer_writer.live_attributes(ns)
            namespace = NAMESPACE_BY_LOGICAL[namespace_plan.logical]
            docs_upserted[namespace_plan.logical] = 0
            for rows in local_index.namespace_row_chunks(con, namespace_plan.logical, namespace_plan.upsert_ids,
                                                         local_allowed, plan.operator_id, live):
                if namespace.person_grain:
                    for row in rows:
                        key = namespace.doc_key
                        row[key] = plan.cloud_id_by_person.get(str(row[key]), str(row[key]))
                docs_upserted[namespace_plan.logical] += turbopuffer_writer.upsert_docs(
                    ns, namespace_plan.logical, rows)
                if namespace.person_grain:
                    uploaded_people.update(local_by_cloud.get(str(row[namespace.doc_key]), str(row[namespace.doc_key]))
                                           for row in rows)
            if namespace_plan.patch_person_ids:
                doc_ids = turbopuffer_writer.fetch_person_doc_ids(
                    ns, namespace_plan.logical, namespace_plan.patch_person_ids)
                if namespace.person_grain:
                    patch_ids = namespace_plan.patch_person_ids
                    for start in range(0, len(patch_ids), local_index.PEOPLE_PER_READ):
                        chunk = patch_ids[start:start + local_index.PEOPLE_PER_READ]
                        local_rows = local_index.namespace_rows(
                            con, namespace_plan.logical,
                            tuple(local_by_cloud.get(person_id, person_id) for person_id in chunk),
                            local_allowed, plan.operator_id, live)
                        key = namespace.doc_key
                        by_person: dict[str, list[dict[str, Any]]] = {}
                        for row in local_rows:
                            cloud_id = plan.cloud_id_by_person.get(str(row[key]), str(row[key]))
                            row[key] = cloud_id
                            by_person.setdefault(cloud_id, []).append(row)
                        for person_id in chunk:
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
            current = UploadManifest.read(self.manifest_path)
            current = replace(current, progress={**current.progress, "namespaces": {
                    logical: {"upserted": docs_upserted.get(logical, 0), "patched": docs_patched.get(logical, 0)}
                    for logical in docs_upserted
                }})
            current.write(self.manifest_path)

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
    ).run()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
