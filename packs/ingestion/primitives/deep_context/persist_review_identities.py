"""Export approved real LinkedIn identities from SQLite to directory.csv."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from packs.ingestion.primitives.common import jsonio
from packs.ingestion.primitives.common.paths import DEFAULT_DIRECTORY_CSV
from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    emit,
)
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.db.identity_views import approved_identities
from packs.ingestion.primitives.imports.directory import (
    directory_identity_key,
    replace_directory_source_rows,
)
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url

DIRECTORY_SOURCE = "deep_context_review"


def _identity_rows(
    *,
    emails: list[str],
    phones: list[str],
    name: str,
    linkedin_url: str,
    person_id: str,
    reason: str,
    source_artifact: str,
) -> list[dict[str, str]]:
    url = normalize_linkedin_url(linkedin_url)
    public_identifier = extract_public_identifier(url)
    if not public_identifier:
        return []
    evidence = json.dumps(
        {
            "decision": "approved_deep_context_identity",
            "person_id": person_id,
            "public_identifier": public_identifier,
        },
        sort_keys=True,
    )
    rows = []
    for email, phone in (
        *((email, "") for email in dict.fromkeys(emails)),
        *(("", phone) for phone in dict.fromkeys(phones)),
    ):
        rows.append(
            {
                "source": DIRECTORY_SOURCE,
                "source_key": directory_identity_key(email, phone, name, public_identifier),
                "source_id": person_id,
                "source_channels": "deep_context",
                "status": "found",
                "email": email,
                "phone": phone,
                "name": name,
                "linkedin_url": url,
                "public_identifier": public_identifier,
                "confidence": "1.00",
                "matched_name": name,
                "evidence": evidence,
                "reasoning": reason,
                "source_artifact": source_artifact,
                "updated_at": jsonio.now_iso(),
                "_priority": "100",
            }
        )
    return rows


def rows_from_db(db: Db) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Approved real identities with every current parent email and phone."""
    rows: list[dict[str, str]] = []
    stats = {"review_considered": 0, "review_persisted": 0, "review_unanchored": 0}
    for decision in approved_identities(db):
        stats["review_considered"] += 1
        materialized = _identity_rows(
            emails=list(decision.emails),
            phones=list(decision.phones),
            name=decision.name,
            linkedin_url=decision.linkedin_url,
            person_id=decision.person_id,
            reason="Approved Deep Context identity review",
            source_artifact=str(db.db_path),
        )
        if materialized:
            stats["review_persisted"] += 1
            rows.extend(materialized)
        else:
            stats["review_unanchored"] += 1
    return rows, stats


class PersistReviewIdentities:
    """Project approved SQLite identities into the Deep Context directory slice."""

    def __init__(
        self,
        *,
        db: Db,
        directory_csv: Path | None = None,
        dry_run: bool = False,
    ) -> None:
        self.directory_csv = Path(directory_csv or DEFAULT_DIRECTORY_CSV)
        self.dry_run = dry_run
        self.db = db

    def run(self) -> dict[str, object]:
        rows, review_stats = rows_from_db(self.db)
        payload: dict[str, object] = {
            "source": "persist_review_identities",
            "status": "dry_run" if self.dry_run else "completed",
            **review_stats,
            "identity_rows": len(rows),
            "directory_csv": str(self.directory_csv),
        }
        if self.dry_run:
            return payload
        committed = replace_directory_source_rows(
            self.directory_csv,
            DIRECTORY_SOURCE,
            rows,
        )
        payload.update(committed)
        return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persist approved Deep Context LinkedIn identities to directory.csv")
    parser.add_argument("--directory-csv", default=str(DEFAULT_DIRECTORY_CSV))
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--dry-run", action="store_true", help="Report mappings without writing directory.csv")
    args = parser.parse_args(argv)
    db = open_existing_db(args.db)
    payload = PersistReviewIdentities(
        directory_csv=Path(args.directory_csv),
        dry_run=args.dry_run,
        db=db,
    ).run()
    emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
