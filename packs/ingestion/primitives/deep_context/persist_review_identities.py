"""Export approved real LinkedIn identities from SQLite to directory.csv."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.common.paths import DEFAULT_DIRECTORY_CSV
from packs.ingestion.primitives.deep_context.common import (
    CONSOLIDATE_PEOPLE_CSV,
    DEFAULT_PEOPLE_CSV,
    emit,
    LINKEDIN_OVERRIDES_CSV,
    RETARGET_PEOPLE_CSV,
    ROOT,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.views import approved_review_identities
from packs.ingestion.primitives.imports.directory import commit_directory_rows, directory_identity_key
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url

DIRECTORY_SOURCE = "deep_context_review"
CANONICAL_DB = ROOT / "deep-context.sqlite"


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
    evidence = json.dumps({
        "decision": "approved_deep_context_identity",
        "person_id": person_id,
        "public_identifier": public_identifier,
    }, sort_keys=True)
    rows: list[dict[str, str]] = []
    for email in dict.fromkeys(emails):
        rows.append({
            "source": DIRECTORY_SOURCE,
            "source_key": directory_identity_key(email, "", name, public_identifier),
            "source_id": person_id,
            "source_channels": "deep_context",
            "status": "found",
            "email": email,
            "phone": "",
            "name": name,
            "linkedin_url": url,
            "public_identifier": public_identifier,
            "confidence": "1.00",
            "matched_name": name,
            "evidence": evidence,
            "reasoning": reason,
            "source_artifact": source_artifact,
            "updated_at": now_iso(),
            "_priority": "100",
        })
    for phone in dict.fromkeys(phones):
        rows.append({
            "source": DIRECTORY_SOURCE,
            "source_key": directory_identity_key("", phone, name, public_identifier),
            "source_id": person_id,
            "source_channels": "deep_context",
            "status": "found",
            "email": "",
            "phone": phone,
            "name": name,
            "linkedin_url": url,
            "public_identifier": public_identifier,
            "confidence": "1.00",
            "matched_name": name,
            "evidence": evidence,
            "reasoning": reason,
            "source_artifact": source_artifact,
            "updated_at": now_iso(),
            "_priority": "100",
        })
    return rows


def rows_from_db(db: Db) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Approved real identities with every current parent email and phone."""
    rows: list[dict[str, str]] = []
    stats = {"review_considered": 0, "review_persisted": 0, "review_unanchored": 0}
    for decision in approved_review_identities(db):
        stats["review_considered"] += 1
        materialized = _identity_rows(
            emails=decision["emails"],
            phones=decision["phones"],
            name=decision["name"],
            linkedin_url=decision["linkedin_url"],
            person_id=decision["person_id"],
            reason="Approved Deep Context identity review",
            source_artifact=str(CANONICAL_DB),
        )
        if materialized:
            stats["review_persisted"] += 1
            rows.extend(materialized)
        else:
            stats["review_unanchored"] += 1
    return rows, stats


class Payload(dict):
    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def to_payload(self) -> dict[str, Any]:
        return dict(self)


class PersistReviewIdentities:
    """Persists approved review/consolidation/retarget identities into the
    `deep_context_review` row slice of the shared `directory.csv`."""

    def __init__(
        self,
        *,
        review_csv: Path | None = None,
        people_csv: Path | None = None,
        consolidate_people_csv: Path | None = None,
        retarget_people_csv: Path | None = None,
        directory_csv: Path | None = None,
        dry_run: bool = False,
        db: Db | None = None,
    ) -> None:
        del review_csv, people_csv, consolidate_people_csv, retarget_people_csv
        self.directory_csv = Path(directory_csv or DEFAULT_DIRECTORY_CSV)
        self.dry_run = dry_run
        self.db = db or Db(CANONICAL_DB)

    def run(self) -> Payload:
        return self.execute()

    def execute(self) -> Payload:
        rows, review_stats = rows_from_db(self.db)
        payload = Payload(
            source="persist_review_identities",
            status="dry_run" if self.dry_run else "completed", **review_stats,
            consolidated_people=0, retarget_people=0, identity_rows=len(rows),
            directory_csv=str(self.directory_csv),
        )
        if self.dry_run:
            return payload
        committed = commit_directory_rows(self.directory_csv, rows)
        payload.update(committed)
        return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persist approved Deep Context LinkedIn identities to directory.csv")
    parser.add_argument("--review-csv", default=str(LINKEDIN_OVERRIDES_CSV))
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    parser.add_argument("--consolidate-people-csv", default=str(CONSOLIDATE_PEOPLE_CSV))
    parser.add_argument("--retarget-people-csv", default=str(RETARGET_PEOPLE_CSV))
    parser.add_argument("--directory-csv", default=str(DEFAULT_DIRECTORY_CSV))
    parser.add_argument("--dry-run", action="store_true", help="Report mappings without writing directory.csv")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = PersistReviewIdentities(
        review_csv=Path(args.review_csv),
        people_csv=Path(args.people_csv),
        consolidate_people_csv=Path(args.consolidate_people_csv),
        retarget_people_csv=Path(args.retarget_people_csv),
        directory_csv=Path(args.directory_csv),
        dry_run=args.dry_run,
    ).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
