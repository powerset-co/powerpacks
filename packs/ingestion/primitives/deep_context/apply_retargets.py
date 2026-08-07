"""Realize approved SQLite retarget decisions into retarget-people.csv."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    PROFILE_CACHE_DIR,
    RETARGET_PEOPLE_CSV,
    emit,
)
from packs.ingestion.primitives.deep_context.db import identity_queries, queries
from packs.ingestion.primitives.deep_context.db.models import ApprovedState
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.profile_projection import profile_payloads
from packs.ingestion.primitives.deep_context.profile_models import ProfileResult
from packs.ingestion.primitives.enrich.profile_transforms import merge_provider_profile, normalize_rapidapi
from packs.ingestion.schemas.people_schema import (
    PEOPLE_SCHEMA_COLUMNS,
    extract_public_identifier,
    normalize_linkedin_url,
)
from packs.shared.csv_io import CsvIO

APPLY_APPROVED = {ApprovedState.AUTO.value, ApprovedState.YES.value}


@dataclass(frozen=True)
class RetargetCarry:
    primary_email: str = ""
    all_emails: str = ""
    primary_phone: str = ""
    all_phones: str = ""
    source_channels: str = ""

    def to_payload(self) -> dict[str, str]:
        return {
            "primary_email": self.primary_email,
            "all_emails": self.all_emails,
            "primary_phone": self.primary_phone,
            "all_phones": self.all_phones,
            "source_channels": self.source_channels,
        }


def build_retarget_row(
    url: str,
    pub: str,
    profile: ProfileResult | None,
    carry: RetargetCarry,
) -> dict[str, str]:
    raw = _cached_retarget_profile(profile, pub)
    row = merge_provider_profile({}, normalize_rapidapi(raw, pub, url), raw)
    row.update({key: value for key, value in carry.to_payload().items() if value})
    output = {key: str(row.get(key) or "") for key in PEOPLE_SCHEMA_COLUMNS}
    output.update(public_identifier=pub, linkedin_url=url)
    return output


def _cached_retarget_profile(
    profile: ProfileResult | None,
    public_identifier: str,
) -> dict[str, Any]:
    """Use a projected profile only when it belongs to the replacement identity."""
    if profile is None or not profile.normalized_profile.success:
        return {}
    raw = profile.raw_payload()
    if raw is None:
        return {}
    cached_identifier = (profile.normalized_profile.public_identifier or "").lower()
    return raw if cached_identifier == public_identifier.lower() else {}


def _carry(db: Db, parent_id: str) -> RetargetCarry:
    parent_identifiers = queries.identifiers(db, parent_id=parent_id)
    emails = [row.display_value or row.normalized_value for row in parent_identifiers if row.kind == "email"]
    phones = [row.display_value or row.normalized_value for row in parent_identifiers if row.kind == "phone"]
    sources = sorted({row.source for row in queries.sources(db, parent_id=parent_id)})
    return RetargetCarry(
        primary_email=emails[0] if emails else "",
        all_emails=json.dumps(emails, ensure_ascii=False) if emails else "",
        primary_phone=phones[0] if phones else "",
        all_phones=json.dumps(phones, ensure_ascii=False) if phones else "",
        source_channels=",".join(sources),
    )


class ApplyRetargets:
    """SQLite decisions in, one realization CSV out."""

    name = "deep_apply_retargets"

    def __init__(
        self,
        *,
        db: Db,
        profile_cache_dir: Path | None = None,
        out_csv: Path | None = None,
    ) -> None:
        self.db = db
        self.profile_cache_dir = Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.out_csv = Path(out_csv or RETARGET_PEOPLE_CSV)

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        identity_links = identity_queries.links(self.db)
        links = {row.row_key: row for row in identity_links}
        profiles = {key.lower(): value for key, value in profile_payloads(self.db).items()}
        markers = [row for row in identity_queries.review_rows(self.db) if row.action == "retarget"]
        realized = {row.public_identifier.lower() for row in identity_links}
        already_realized = 0
        pending = []
        for marker in markers:
            url = normalize_linkedin_url(marker.new_linkedin_url or "")
            pub = (marker.new_public_identifier or "").lower() or extract_public_identifier(url).lower()
            if pub and pub in realized:
                already_realized += 1
                continue
            pending.append((marker, url, pub))

        retargets = [row for row in pending if (row[0].approved or "").lower() in APPLY_APPROVED]
        rows, details = [], []
        cache_hits = skipped = 0
        for marker, url, pub in retargets:
            old = marker.public_identifier.lower()
            if not url or not pub:
                skipped += 1
                details.append({"old": old, "status": "skipped", "reason": "no new_linkedin_url"})
                continue
            profile: ProfileResult | None = profiles.get(marker.key.lower())
            cache_hits += int(bool(_cached_retarget_profile(profile, pub)))
            parent_id = links[marker.key].parent_id
            rows.append(
                build_retarget_row(
                    url,
                    pub,
                    profile,
                    _carry(self.db, parent_id),
                )
            )
            details.append(
                {
                    "old": old,
                    "new": pub,
                    "status": "projected",
                    "from_cache": bool(_cached_retarget_profile(profile, pub)),
                }
            )

        CsvIO.write_dict_rows(self.out_csv, PEOPLE_SCHEMA_COLUMNS, rows)
        return {
            "status": "completed",
            "source": "apply_retargets",
            "approved_retargets": len(retargets),
            "enriched": len(rows),
            "cache_hits": cache_hits,
            "skipped": skipped,
            "already_realized": already_realized,
            "retarget_people_csv": str(self.out_csv),
            "rows": len(rows),
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
        db=open_existing_db(args.db),
        profile_cache_dir=Path(args.profile_cache_dir),
        out_csv=Path(args.out_csv),
    ).run()
    emit(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
