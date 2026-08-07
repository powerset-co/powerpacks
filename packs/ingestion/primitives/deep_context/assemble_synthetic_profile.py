"""Project no-LinkedIn research results as one synthetic identity per parent."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    ENRICH_MANIFEST,
    LINKEDIN_OVERRIDES_CSV,
)
from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_review
from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
    ArtifactKind,
    ArtifactProjection,
    ArtifactRow,
    CandidatePeopleProjection,
    CandidatePersonRow,
    LinkRow,
    ProjectionStatus,
    ReviewAction,
    ReviewSource,
    RowKind,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.db.view_models import SyntheticFallbackRow
from packs.ingestion.primitives.deep_context.enrichment_receipt import EnrichmentReceipt
from packs.ingestion.primitives.deep_context.research_reconcile.selection import DR_OUT_DIR
from packs.ingestion.primitives.deep_context.research_result import ResearchResult
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS, normalize_linkedin_url

DEFAULT_OUT = LINKEDIN_OVERRIDES_CSV.parent / "synthetic-people.csv"
DEFAULT_AUTO_COMPLETENESS = 0.6
PROVENANCE_COLUMNS = ["source_parent_slug", "source_person_ids", "source_candidate_public_identifier"]
SYNTHETIC_COLUMNS = PEOPLE_SCHEMA_COLUMNS + PROVENANCE_COLUMNS + ["approved", "synthetic_metadata"]
USER_APPROVED = frozenset({ApprovedState.YES.value, ApprovedState.NO.value})


def _public_identifier(email: str, phone: str, handle: str) -> str:
    value = email.strip().lower() or phone.strip()
    if value:
        kind = "email" if email else "phone"
        return f"synth-{kind}-{hashlib.sha1(value.encode()).hexdigest()[:12]}"
    return f"synth-x-{handle.strip().lower()}"


def _completeness(profile: dict[str, Any]) -> float:
    return float((profile.get("metadata") or {}).get("estimated_completeness") or 0)


def _merge_profiles(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(profiles, key=_completeness, reverse=True)
    if len(ordered) < 2:
        return ordered[0] if ordered else {}

    def first(field: str) -> Any:
        return next((item.get(field) for item in ordered if item.get(field)), {})

    def unique(field: str, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        for item in ordered:
            for row in item.get(field) or []:
                if not isinstance(row, dict):
                    continue
                key = tuple(str(row.get(name) or "").strip().lower() for name in keys)
                if any(key) and key not in seen:
                    seen.add(key)
                    result.append(row)
        return result

    result = dict(ordered[0])
    metadata = dict(result.get("metadata") or {})
    metadata["estimated_completeness"] = max(map(_completeness, ordered))
    metadata["gaps"] = list(dict.fromkeys(
        str(gap).strip() for item in ordered
        for gap in (item.get("metadata") or {}).get("gaps") or [] if str(gap).strip()
    ))
    result.update({
        "headline": first("headline"), "summary": first("summary"),
        "location": first("location"),
        "positions": unique("positions", ("company_name", "title", "start_date")),
        "education": unique("education", ("school_name", "school", "degree")),
        "metadata": metadata,
    })
    return result


def build_synthetic_row(
    profile: dict[str, Any], source: SyntheticFallbackRow, person_ids: list[str],
    auto_completeness: float = DEFAULT_AUTO_COMPLETENESS,
) -> dict[str, str]:
    person, location = profile.get("person") or {}, profile.get("location") or {}
    metadata, social = profile.get("metadata") or {}, profile.get("social") or {}
    positions = [row for row in profile.get("positions") or [] if isinstance(row, dict)]
    education = [row for row in profile.get("education") or [] if isinstance(row, dict)]
    current = next((row for row in positions if row.get("is_current")), {})
    handle = source.display_slug or source.handle
    email, phone = source.primary_email, source.phone_e164
    public_identifier = _public_identifier(email, phone, handle)
    completeness = _completeness(profile)
    row = {column: "" for column in SYNTHETIC_COLUMNS}
    row.update({
        "id": person_ids[0] if person_ids else public_identifier,
        "public_identifier": public_identifier,
        "full_name": person.get("full_name") or source.display_name,
        "first_name": person.get("first_name") or "", "last_name": person.get("last_name") or "",
        "headline": (profile.get("headline") or {}).get("text") or "",
        "summary": (profile.get("summary") or {}).get("text") or "",
        "city": location.get("city") or "", "state": location.get("state") or "",
        "country": location.get("country") or "",
        "location_raw": location.get("raw") or ", ".join(
            value for value in (location.get("city"), location.get("country")) if value
        ),
        "work_experiences": json.dumps(positions, ensure_ascii=False) if positions else "",
        "education": json.dumps(education, ensure_ascii=False) if education else "",
        "current_title": current.get("title") or "",
        "current_company": current.get("company_name") or "",
        "entity_urn": f"synthetic:{person_ids[0] if person_ids else public_identifier}",
        "enrichment_provider": "synthetic", "enriched_at": now_iso(),
        "twitter_handle": social.get("twitter_handle") or "",
        "primary_email": email, "primary_phone": phone,
        "approved": "auto" if completeness >= auto_completeness else "",
        "source_parent_slug": handle, "source_person_ids": json.dumps(person_ids),
        "source_candidate_public_identifier": source.candidate_key,
        "synthetic_metadata": json.dumps({
            "completeness": completeness, "name_confidence": person.get("confidence"),
            "gaps": metadata.get("gaps") or [],
            "research_date": metadata.get("research_date") or "",
            "research_method": metadata.get("research_method") or "",
            "source_channel": metadata.get("source_channel") or ("email" if email else "phone"),
        }, ensure_ascii=False),
    })
    return row


class AssembleSyntheticProfile:
    """SQLite-first synthetic projection; CSV is a one-way result export."""

    name = "deep_assemble_synthetic"

    def __init__(
        self, *, db: Db, research_dir: Path | None = None, out: Path | None = None,
        auto_completeness: float = DEFAULT_AUTO_COMPLETENESS,
        manifest: str | Path | None = None, prune: bool = True,
    ) -> None:
        research_path = Path(research_dir or DR_OUT_DIR)
        self.db, self.out = db, Path(out or DEFAULT_OUT)
        self.auto_completeness, self.prune = auto_completeness, prune
        self.manifest_path = Path(manifest) if manifest else (
            ENRICH_MANIFEST if research_path.resolve() == DR_OUT_DIR.resolve() else None
        )
        self.artifact_root = self.manifest_path.parent if self.manifest_path else research_path

    def run(self) -> dict[str, Any]:
        return self.execute()

    def execute(self) -> dict[str, Any]:
        started = time.monotonic()
        counts = {key: 0 for key in (
            "built", "auto_approved", "pending_review", "preserved_user_rows",
            "skipped_with_linkedin", "skipped_unusable", "skipped_worth_no",
            "pruned_stale_machine_rows", "collapsed_merged_parents",
        )}
        sources = linkedin_review(self.db, "synthetic")
        existing: dict[str, dict[str, str]] = {}
        parent_pubs: dict[str, set[str]] = {}
        groups: dict[str, list[tuple[dict[str, Any], SyntheticFallbackRow]]] = {}
        for source in sources:
            parent_id = source.parent_id
            for item in source.existing_synthetics:
                public_identifier = item.public_identifier
                try:
                    row = json.loads(item.profile_json or "{}")
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    row["approved"] = item.approved
                    existing[public_identifier] = {key: str(value or "") for key, value in row.items()}
                    parent_pubs.setdefault(parent_id, set()).add(public_identifier)
            result = ResearchResult.from_json(source.result_json)
            if result is None:
                continue
            rejected = source.machine_reject.lower() in {"1", "true", "yes"}
            if result.linkedin_url and not rejected:
                counts["skipped_with_linkedin"] += 1
            elif not result.usable:
                counts["skipped_unusable"] += 1
            else:
                groups.setdefault(parent_id, []).append((
                    result.to_payload(without_linkedin=bool(result.linkedin_url)), source,
                ))
        if self.prune:
            for public_identifier, row in list(existing.items()):
                if row.get("source_parent_slug") and row.get("approved", "").lower() not in USER_APPROVED:
                    existing.pop(public_identifier)
                    counts["pruned_stale_machine_rows"] += 1

        projections: list[tuple[str, str, list[str], dict[str, str]]] = []
        for parent_id, items in sorted(groups.items()):
            if len(items) > 1:
                counts["collapsed_merged_parents"] += 1
            person_ids = list(dict.fromkeys(
                person_id for _, source in items for person_id in source.person_ids
            ))
            if items[0][1].effective_worth == "no" and all(
                person_id.startswith("candidate:") for person_id in items[0][1].person_ids
            ):
                counts["skipped_worth_no"] += 1
                continue
            source = next(
                (item for _, item in items if item.primary_email or item.phone_e164),
                items[0][1],
            )
            row = build_synthetic_row(_merge_profiles([item for item, _ in items]), source, person_ids,
                                      self.auto_completeness)
            public_identifier = row["public_identifier"].lower()
            collisions = parent_pubs.get(parent_id, set()) | {public_identifier}
            decisions = {existing.get(pub, {}).get("approved", "").lower() for pub in collisions}
            decision = "no" if "no" in decisions else "yes" if "yes" in decisions else ""
            previous = existing.get(public_identifier)
            if previous and previous.get("approved", "").lower() in USER_APPROVED:
                row = previous
                counts["preserved_user_rows"] += 1
            else:
                row["approved"] = decision or row["approved"]
                counts["built"] += 1
                counts["auto_approved" if row["approved"] == "auto" else "pending_review"] += 1
            for old_public_identifier in collisions - {public_identifier}:
                existing.pop(old_public_identifier, None)
            existing[public_identifier] = row
            projections.append((public_identifier, parent_id, person_ids, row))

        self.out.parent.mkdir(parents=True, exist_ok=True)
        with self.out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=SYNTHETIC_COLUMNS)
            writer.writeheader()
            writer.writerows({key: row.get(key, "") for key in SYNTHETIC_COLUMNS}
                             for _, row in sorted(existing.items()))
        result = {
            "status": "completed", "primitive": "assemble_synthetic_profile", **counts,
            "total_rows": len(existing), "out": str(self.out),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        synthetic_dir = self.artifact_root / "synthetic"
        synthetic_dir.mkdir(parents=True, exist_ok=True)
        artifact_projections: list[ArtifactProjection] = []
        for public_identifier, parent_id, person_ids, row in projections:
            path = synthetic_dir / f"{hashlib.sha1(public_identifier.encode()).hexdigest()}.json"
            data = json.dumps(row, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
            path.write_bytes(data)
            profile_json = json.dumps(row, sort_keys=True, separators=(",", ":"))
            social = row.get("social") if isinstance(row.get("social"), dict) else {}
            linkedin_value = str(
                row.get("linkedin_url") or social.get("linkedin_url") or ""
            ).strip()
            linkedin_url = normalize_linkedin_url(linkedin_value) if linkedin_value else None
            artifact_key = f"synthetic:{public_identifier}"
            auto_approved = row.get("approved") == "auto"
            display_name = str(row.get("full_name") or "").strip() or None
            member_ids = sorted({
                str(person_id).strip().lower()
                for person_id in person_ids
                if str(person_id).strip()
            })
            artifact_projections.append(ArtifactProjection(
                artifact=ArtifactRow(
                    artifact_key=artifact_key,
                    kind=ArtifactKind.SYNTHETIC.value,
                    parent_id=parent_id,
                    path=str(path.resolve()),
                    content_fingerprint=hashlib.sha256(data).hexdigest(),
                    status=ProjectionStatus.PROJECTED.value,
                    candidate_key=public_identifier,
                    projected_at=now_iso(),
                ),
                candidate=LinkRow(
                    public_identifier,
                    parent_id,
                    public_identifier,
                    RowKind.SYNTHETIC.value,
                    linkedin_url,
                    display_name,
                    machine_action=(ReviewAction.VERIFY.value if auto_approved else None),
                    machine_approved=("auto" if auto_approved else None),
                    source=ReviewSource.DEEP_RESEARCH.value,
                    updated_at=now_iso(),
                ),
                candidate_people=CandidatePeopleProjection(
                    public_identifier,
                    tuple(
                        CandidatePersonRow(public_identifier, person_id, parent_id)
                        for person_id in member_ids
                    ),
                ),
                synthetic_profile=SyntheticProfileRow(
                    public_identifier,
                    public_identifier,
                    profile_json,
                    artifact_key,
                    linkedin_url,
                    display_name,
                    now_iso(),
                ),
            ))
        self.db.project_rows(tuple(artifact_projections))
        if self.manifest_path:
            EnrichmentReceipt(self.manifest_path).write({
                "stage": "enrich", "status": "research_complete", "phase": "profiles_pending",
                "assembly": result, "outputs": {"synthetic_people_csv": str(self.out)},
            })
        return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    paths = {"research-dir": DR_OUT_DIR, "out": DEFAULT_OUT, "db": CANONICAL_DB}
    for flag, default in paths.items():
        parser.add_argument(f"--{flag}", default=str(default))
    parser.add_argument("--auto-completeness", type=float, default=DEFAULT_AUTO_COMPLETENESS)
    parser.add_argument("--manifest")
    args = parser.parse_args(argv)
    payload = AssembleSyntheticProfile(
        db=open_existing_db(args.db), research_dir=Path(args.research_dir), out=Path(args.out),
        auto_completeness=args.auto_completeness, manifest=args.manifest,
    ).run()
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
