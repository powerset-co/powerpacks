"""Realize approved SQLite retarget decisions into retarget-people.csv."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    PROFILE_CACHE_DIR,
    RETARGET_PEOPLE_CSV,
    emit,
    load_env,
)
from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
    IdentityMachineProjection,
    ReviewExportRow,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot, identity_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.profile_projection import hydrate_profiles
from packs.ingestion.primitives.enrich.profile_transforms import merge_provider_profile, normalize_rapidapi
from packs.ingestion.schemas.people_schema import (
    PEOPLE_SCHEMA_COLUMNS,
    extract_public_identifier,
    normalize_linkedin_url,
)
from packs.shared.csv_io import CsvIO

USER_APPROVED = {ApprovedState.YES.value, ApprovedState.NO.value}
APPLY_APPROVED = {ApprovedState.AUTO.value, ApprovedState.YES.value}
def judge_accepted_candidate_retarget(row: ReviewExportRow) -> bool:
    return (
        row.action.strip().lower() == "retarget"
        and row.approved.strip().lower() not in USER_APPROVED
        and row.person_id.strip().lower().startswith("candidate:")
        and row.llm_reject.strip().lower() not in {"yes", "true", "1", "spam"}
    )


def _approve(row: Any) -> IdentityMachineProjection:
    values = {name: value for name, value in asdict(row).items()
              if name in IdentityMachineProjection.__dataclass_fields__}
    values.update(machine_approved=ApprovedState.YES.value, updated_at=now_iso())
    return IdentityMachineProjection(**values)


def enrich_one(url: str, pub: str, cache_dir: Path) -> dict[str, Any]:
    _, profiles = hydrate_profiles(
        [{"public_identifier": pub, "linkedin_url": url}], cache_dir
    )
    result = profiles.get(pub, {})
    if (result.get("normalized_profile") or {}).get("success") is not True:
        return {"raw": None, "from_cache": bool(result.get("from_cache")),
                "error": result.get("detail") or "enrichment failed / no profile"}
    return {"raw": result.get("data"), "from_cache": bool(result.get("from_cache")),
            "error": ""}


def build_retarget_row(url: str, pub: str, raw: dict[str, Any], carry: dict[str, str]) -> dict[str, str]:
    row = merge_provider_profile({}, normalize_rapidapi(raw, pub, url), raw)
    row.update({key: value for key, value in carry.items() if value})
    output = {key: str(row.get(key) or "") for key in PEOPLE_SCHEMA_COLUMNS}
    output.update(public_identifier=pub, linkedin_url=url)
    return output


def _carry(snapshot: Any, parent_id: str) -> dict[str, str]:
    people = {row.person_id for row in snapshot.people if row.parent_id == parent_id}
    emails = [row.display_value or row.normalized_value for row in snapshot.identifiers
              if row.person_id in people and row.kind == "email"]
    phones = [row.display_value or row.normalized_value for row in snapshot.identifiers
              if row.person_id in people and row.kind == "phone"]
    sources = sorted({row.source for row in snapshot.sources if row.person_id in people})
    return {
        "primary_email": emails[0] if emails else "",
        "all_emails": json.dumps(emails, ensure_ascii=False) if emails else "",
        "primary_phone": phones[0] if phones else "",
        "all_phones": json.dumps(phones, ensure_ascii=False) if phones else "",
        "source_channels": ",".join(sources),
    }


class ApplyRetargets:
    """SQLite decisions in, one realization CSV out."""

    name = "deep_apply_retargets"

    def __init__(
        self, *, db: Db, profile_cache_dir: Path | None = None,
        out_csv: Path | None = None,
    ) -> None:
        self.db = db
        self.profile_cache_dir = Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.out_csv = Path(out_csv or RETARGET_PEOPLE_CSV)

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        identity = identity_snapshot(self.db)
        canonical = canonical_snapshot(self.db)
        links = {row.row_key: row for row in identity.links}
        markers = [row for row in identity.review_rows if row.action == "retarget"]
        realized = {row.public_identifier.lower() for row in identity.links}
        finalized, realized_keys = 0, []
        pending = []
        for marker in markers:
            url = normalize_linkedin_url(marker.new_linkedin_url)
            pub = marker.new_public_identifier.lower() or extract_public_identifier(url).lower()
            if pub and pub in realized:
                if marker.approved.lower() not in USER_APPROVED:
                    finalized += 1
                    realized_keys.append(marker.key)
                continue
            pending.append((marker, url, pub))
        if realized_keys:
            self.db.project_rows(tuple(_approve(links[key]) for key in realized_keys if key in links))

        retargets = [row for row in pending if (
            row[0].approved.lower() in APPLY_APPROVED
            or judge_accepted_candidate_retarget(row[0])
        )]
        if retargets:
            load_env()
        rows, details = [], []
        cache_hits = misses = skipped = 0
        for marker, url, pub in retargets:
            old = marker.public_identifier.lower()
            if not url or not pub:
                skipped += 1
                details.append({"old": old, "status": "skipped", "reason": "no new_linkedin_url"})
                continue
            result = enrich_one(url, pub, self.profile_cache_dir)
            if result["error"]:
                skipped += 1
                details.append({"old": old, "new": pub, "status": "skipped",
                                "reason": result["error"]})
                continue
            cache_hits += int(result["from_cache"])
            misses += int(not result["from_cache"])
            parent_id = links[marker.key].parent_id
            rows.append(build_retarget_row(url, pub, result["raw"], _carry(canonical, parent_id)))
            details.append({"old": old, "new": pub, "status": "enriched",
                            "from_cache": result["from_cache"]})

        CsvIO.write_dict_rows(self.out_csv, PEOPLE_SCHEMA_COLUMNS, rows)
        return {
            "status": "completed", "source": "apply_retargets",
            "approved_retargets": len(retargets), "enriched": len(rows),
            "cache_hits": cache_hits, "rapidapi_misses": misses, "skipped": skipped,
            "finalized_applied": finalized, "stranded_count": 0, "stranded": [],
            "retarget_people_csv": str(self.out_csv), "rows": len(rows),
            "details": details[:50],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "updated_at": now_iso(),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--profile-cache-dir", default=str(PROFILE_CACHE_DIR))
    parser.add_argument("--out-csv", default=str(RETARGET_PEOPLE_CSV))
    args = parser.parse_args(argv)
    payload = ApplyRetargets(
        db=Db(Path(args.db)), profile_cache_dir=Path(args.profile_cache_dir),
        out_csv=Path(args.out_csv),
    ).run()
    emit(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
