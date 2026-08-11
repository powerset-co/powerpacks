"""Project no-LinkedIn research results as one synthetic identity per parent.

A synthetic profile stands in for a person deep research could not attach a
verified LinkedIn URL to. Assembled row shape (CSV columns, illustrative
subset — see `SYNTHETIC_COLUMNS` for the full list)::

    {
      "id": "b7e5c3d2-...-uuid",           # existing person_id, else the parent id
      "public_identifier": "parent-3f9a1c2b4d5e",
      "full_name": "Jordan Bravo",
      "headline": "Product Manager at Example Co",
      "current_title": "Product Manager",
      "current_company": "Example Co",
      "work_experiences": "[{\\"title\\": \\"Product Manager\\", ...}]",
      "approved": "auto",                  # "" pending review, else a human "yes"/"no"
      "synthetic_metadata": "{\\"completeness\\": 0.72, \\"gaps\\": [\\"education\\"]}"
    }

`full_name`/`headline`/`work_experiences`/`education`/location come straight
off a Parallel research result (evidence-backed — see
`models.SyntheticResearchProfile`). `id`/`public_identifier`/
`entity_urn`/`approved` are minted or derived here; see the notes at each
derivation below.

Changelog:
  2026-08-08: `public_identifier` is now the parent id (was a hash of
    whichever email/phone won this run's research). The parent id is
    immutable once minted (`ensure_parents/assignment.py`); the old hash
    changed whenever a different contact channel won, which needed a
    `collisions`-set reconciliation to carry a human yes/no decision forward
    across the rename — that mechanism is gone along with the reason for it.
    `db.context_queries.migrate_legacy_synthetic_keys` re-keys any
    pre-existing SQLite rows once, in place, preserving every column
    including human decisions; see its docstring for the removal condition.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    DEEP_RESEARCH_DIR,
    ENRICH_MANIFEST,
    LINKEDIN_OVERRIDES_CSV,
)
from packs.ingestion.primitives.deep_context.db.context_queries import (
    migrate_legacy_synthetic_keys,
)
from packs.ingestion.primitives.deep_context.db.identity_views import synthetic_fallback
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
    RowKind,
    SyntheticProfileRow,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.db.view_models import SyntheticFallbackRow
from packs.ingestion.primitives.deep_context.manifests.enrichment_receipt import (
    EnrichmentReceipt,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.primitives.deep_context.enrich.synthetic.models import (
    SyntheticCsvRow,
    SyntheticEducation,
    SyntheticPosition,
    SyntheticResearchProfile,
)
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS, normalize_linkedin_url

DEFAULT_OUT = LINKEDIN_OVERRIDES_CSV.parent / "synthetic-people.csv"
DEFAULT_AUTO_COMPLETENESS = 0.6
PROVENANCE_COLUMNS = ["source_parent_slug", "source_person_ids", "source_candidate_public_identifier"]
SYNTHETIC_COLUMNS = PEOPLE_SCHEMA_COLUMNS + PROVENANCE_COLUMNS + ["approved", "synthetic_metadata"]
USER_APPROVED = frozenset({ApprovedState.YES.value, ApprovedState.NO.value})


def _merge_profiles(
    profiles: list[SyntheticResearchProfile],
) -> SyntheticResearchProfile | None:
    """Collapse N research runs for one parent into one profile: highest
    completeness wins as the base, headline/summary/location backfill from
    the first other run that has them, positions/education union-dedup by
    `.key`, and completeness/gaps take the max/union across all runs."""
    ordered = sorted(profiles, key=lambda item: item.completeness, reverse=True)
    if len(ordered) < 2:
        return ordered[0] if ordered else None

    def unique_positions() -> tuple[SyntheticPosition, ...]:
        values: list[SyntheticPosition] = []
        seen: set[tuple[str, str, str]] = set()
        for item in ordered:
            for row in item.positions:
                if any(row.key) and row.key not in seen:
                    seen.add(row.key)
                    values.append(row)
        return tuple(values)

    def unique_education() -> tuple[SyntheticEducation, ...]:
        values: list[SyntheticEducation] = []
        seen: set[tuple[str, str, str]] = set()
        for item in ordered:
            for row in item.education:
                if any(row.key) and row.key not in seen:
                    seen.add(row.key)
                    values.append(row)
        return tuple(values)

    base = ordered[0]
    headline: str | None = next(
        (item.headline for item in ordered if item.headline), None
    )
    summary: str | None = next(
        (item.summary for item in ordered if item.summary), None
    )
    location: SyntheticResearchProfile = next(
        (item for item in ordered if item.city or item.state or item.country or item.location_raw),
        base,
    )
    return replace(
        base,
        headline=headline,
        summary=summary,
        city=location.city,
        state=location.state,
        country=location.country,
        location_raw=location.location_raw,
        positions=unique_positions(),
        education=unique_education(),
        completeness=max(item.completeness for item in ordered),
        gaps=tuple(dict.fromkeys(gap for item in ordered for gap in item.gaps)),
    )


def build_synthetic_row(
    profile: SyntheticResearchProfile,
    source: SyntheticFallbackRow,
    person_ids: list[str],
    auto_completeness: float = DEFAULT_AUTO_COMPLETENESS,
) -> SyntheticCsvRow:
    """Build one CSV row: name/headline/positions/education/location are
    evidence copied from `profile` (research); `id`/`public_identifier`/
    `entity_urn`/`approved` are derived here. `approved="auto"` (vs "" pending
    review) is the entire bar between asserting this identity unattended and
    requiring a human yes — completeness >= `auto_completeness` (default 0.6).

    `public_identifier` is `source.parent_id`: the one stable, immutable id
    for this cluster (`ensure_parents/assignment.py`) — unlike a hash of
    email/phone, it never changes when a different contact channel wins a
    later research run, so this identity's row_key never needs to migrate
    run over run."""
    current: SyntheticPosition | None = next(
        (row for row in profile.positions if row.is_current), None
    )
    handle = source.display_slug or source.handle
    email, phone = source.primary_email, source.phone_e164
    public_identifier = source.parent_id
    completeness = profile.completeness
    row = {column: "" for column in SYNTHETIC_COLUMNS}
    row.update({
        "id": person_ids[0] if person_ids else public_identifier,
        "public_identifier": public_identifier,
        "full_name": profile.full_name or source.display_name,
        "first_name": profile.first_name or "", "last_name": profile.last_name or "",
        "headline": profile.headline or "",
        "summary": profile.summary or "",
        "city": profile.city or "", "state": profile.state or "",
        "country": profile.country or "",
        "location_raw": profile.location_raw or ", ".join(
            value for value in (profile.city, profile.country) if value
        ),
        "work_experiences": json.dumps([item.to_payload() for item in profile.positions], ensure_ascii=False) if profile.positions else "",
        "education": json.dumps([item.to_payload() for item in profile.education], ensure_ascii=False) if profile.education else "",
        "current_title": current.title or "" if current else "",
        "current_company": current.company_name or "" if current else "",
        "entity_urn": f"synthetic:{person_ids[0] if person_ids else public_identifier}",
        "enrichment_provider": "synthetic", "enriched_at": now_iso(),
        "twitter_handle": profile.twitter_handle or "",
        "primary_email": email, "primary_phone": phone,
        "approved": "auto" if completeness >= auto_completeness else "",
        "source_parent_slug": handle, "source_person_ids": json.dumps(person_ids),
        "source_candidate_public_identifier": source.candidate_key,
        # Self-reported research diagnostics (the provider's own completeness/
        # confidence/gaps estimate) — a confidence signal, not verified fact.
        "synthetic_metadata": json.dumps({
            "completeness": completeness, "name_confidence": profile.name_confidence,
            "gaps": list(profile.gaps),
            "research_date": profile.research_date or "",
            "research_method": profile.research_method or "",
            "source_channel": profile.source_channel or ("email" if email else "phone"),
        }, ensure_ascii=False),
    })
    return SyntheticCsvRow.from_payload(row)


class AssembleSyntheticProfile:
    """SQLite-first synthetic projection; CSV is a one-way result export."""

    def __init__(
        self, *, db: Db, research_dir: Path | None = None, out: Path | None = None,
        auto_completeness: float = DEFAULT_AUTO_COMPLETENESS,
        manifest: str | Path | None = None,
    ) -> None:
        research_path = Path(research_dir or DEEP_RESEARCH_DIR)
        self.db, self.out = db, Path(out or DEFAULT_OUT)
        self.auto_completeness = auto_completeness
        self.manifest_path = Path(manifest) if manifest else (
            ENRICH_MANIFEST if research_path.resolve() == DEEP_RESEARCH_DIR.resolve() else None
        )
        self.artifact_root = self.manifest_path.parent if self.manifest_path else research_path

    def execute(self) -> dict[str, Any]:
        started = time.monotonic()
        migrated_legacy_keys = migrate_legacy_synthetic_keys(self.db)
        counts = {key: 0 for key in (
            "built", "auto_approved", "pending_review", "preserved_user_rows",
            "skipped_with_linkedin", "skipped_unusable",
            "pruned_stale_machine_rows", "collapsed_merged_parents",
        )}
        # Strict worth gate (effective_worth='yes', not the looser !='no' used
        # by attached/heal): a synthetic profile ASSERTS facts about a real
        # person from research alone, so it only builds where a human/machine
        # call actually affirmed the parent — an unclassified "maybe" must
        # never get a fabricated identity.
        sources = synthetic_fallback(self.db)
        existing: dict[str, SyntheticCsvRow] = {}
        groups: dict[str, list[tuple[SyntheticResearchProfile, SyntheticFallbackRow]]] = {}
        for source in sources:
            parent_id = source.parent_id
            for item in source.existing_synthetics:
                row: SyntheticCsvRow | None = SyntheticCsvRow.from_json(
                    item.profile_json,
                    approved=item.approved,
                )
                if row is None:
                    continue
                existing[item.public_identifier] = row
            result: ResearchResult | None = ResearchResult.from_json(source.result_json)
            if result is None:
                continue
            rejected = source.machine_reject == "yes"
            if result.linkedin_url and not rejected:
                counts["skipped_with_linkedin"] += 1
            elif not result.usable:
                counts["skipped_unusable"] += 1
            else:
                groups.setdefault(parent_id, []).append((
                    SyntheticResearchProfile.from_result(result),
                    source,
                ))
        # Drop every non-user-decided row up front so a parent that no longer
        # needs a synthetic fallback (e.g. a real LinkedIn attached since the
        # last run) disappears from output instead of lingering forever; a row
        # a human already said yes/no to is never touched here. The
        # source_parent_slug check is live legacy tolerance, not redundancy:
        # migration writes preserved user rows that lack it, and those must
        # survive the sweep.
        for public_identifier, row in list(existing.items()):
            if row.source_parent_slug and (row.approved or "").lower() not in USER_APPROVED:
                existing.pop(public_identifier)
                counts["pruned_stale_machine_rows"] += 1

        projections: list[tuple[str, str, list[str], SyntheticCsvRow]] = []
        for parent_id, items in sorted(groups.items()):
            if len(items) > 1:
                counts["collapsed_merged_parents"] += 1
            person_ids = list(dict.fromkeys(
                person_id for _, source in items for person_id in source.person_ids
            ))
            source: SyntheticFallbackRow = next(
                (item for _, item in items if item.primary_email or item.phone_e164),
                items[0][1],
            )
            profile: SyntheticResearchProfile | None = _merge_profiles(
                [item for item, _ in items]
            )
            if profile is None:
                continue
            row = build_synthetic_row(
                profile, source, person_ids, self.auto_completeness
            )
            # public_identifier == parent_id: the same key every run builds
            # for this parent, so a prior row (if any) is always found here —
            # no collision/rename bookkeeping needed to carry a human
            # decision forward (see the module Changelog).
            public_identifier = row.public_identifier
            previous: SyntheticCsvRow | None = existing.get(public_identifier)
            if previous and (previous.approved or "").lower() in USER_APPROVED:
                # A human already said yes/no for this parent — never
                # overwritten by a re-run, even if research content changed.
                row = previous
                counts["preserved_user_rows"] += 1
            else:
                counts["built"] += 1
                counts["auto_approved" if row.approved == "auto" else "pending_review"] += 1
            existing[public_identifier] = row
            projections.append((public_identifier, parent_id, person_ids, row))

        self.out.parent.mkdir(parents=True, exist_ok=True)
        with self.out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=SYNTHETIC_COLUMNS)
            writer.writeheader()
            writer.writerows({key: row.to_payload().get(key, "") for key in SYNTHETIC_COLUMNS}
                             for _, row in sorted(existing.items()))
        summary = {
            "status": "completed", "primitive": "assemble_synthetic_profile", **counts,
            "total_rows": len(existing), "out": str(self.out),
            "migrated_legacy_synthetic_keys": migrated_legacy_keys,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        synthetic_dir = self.artifact_root / "synthetic"
        synthetic_dir.mkdir(parents=True, exist_ok=True)
        artifact_projections: list[ArtifactProjection] = []
        for public_identifier, parent_id, person_ids, row in projections:
            path = synthetic_dir / f"{hashlib.sha1(public_identifier.encode()).hexdigest()}.json"
            payload = row.to_payload()
            data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
            path.write_bytes(data)
            profile_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            # row.linkedin_url is always blank for rows built above —
            # build_synthetic_row never sets it (a group with a real, accepted
            # linkedin_url is filtered out before groups is built, see
            # skipped_with_linkedin) — the canonical normalizer here guards a
            # legacy/future writer, not a path this method exercises today.
            linkedin_value = (row.linkedin_url or "").strip()
            linkedin_url: str | None = (
                normalize_linkedin_url(linkedin_value) if linkedin_value else None
            )
            artifact_key = f"synthetic:{public_identifier}"
            # completeness >= auto_completeness is the only bar between
            # asserting this identity unattended and requiring a human "yes"
            # (see build_synthetic_row); bridged into the SQLite candidate
            # row below so review/realize treat it the same as a human yes.
            auto_approved = row.approved == "auto"
            display_name = (row.full_name or "").strip() or None
            # Real (non-owner, non-ghost) person_ids from this parent's
            # family — the synthetic profile's link into the people graph.
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
                    source=WriterSource.DEEP_RESEARCH.value,
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
                "assembly": summary, "outputs": {"synthetic_people_csv": str(self.out)},
            })
        return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    paths = {"research-dir": DEEP_RESEARCH_DIR, "out": DEFAULT_OUT, "db": CANONICAL_DB}
    for flag, default in paths.items():
        parser.add_argument(f"--{flag}", default=str(default))
    parser.add_argument("--auto-completeness", type=float, default=DEFAULT_AUTO_COMPLETENESS)
    parser.add_argument("--manifest")
    args = parser.parse_args(argv)
    payload = AssembleSyntheticProfile(
        db=open_existing_db(args.db), research_dir=Path(args.research_dir), out=Path(args.out),
        auto_completeness=args.auto_completeness, manifest=args.manifest,
    ).execute()
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
