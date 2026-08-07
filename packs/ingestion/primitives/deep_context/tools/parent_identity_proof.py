#!/usr/bin/env python3
"""Prove stable parent identity against copied real Deep Context installs.

The legacy filesystem is read exactly once, by ``migrate_sqlite``, inside a
throwaway copy. Every assertion after that boundary uses the typed canonical
SQLite snapshot. The source install is never opened for writing, and the report
contains counts and booleans only -- never person or parent identifiers.

Flow: copy -> migrate once -> migration-only graph replay -> cold planning ->
incremental planning. Steady-state merge safety belongs to the standing SQL
invariants and seeded operation fuzz, not this temporary migration harness.

Removal countdown (2026-08-06): delete once no supported install predates
powerpacks v1.19.0.
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import tempfile
from collections import Counter
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    emit,
    ROOT,
)
from packs.ingestion.primitives.deep_context.db.models import (
    CanonicalGraphProjection,
    CanonicalSnapshot,
    SourceChannel,
)
from packs.ingestion.primitives.deep_context.migration.legacy import LegacyGraphMigration
from packs.ingestion.primitives.deep_context.db.identity_invariants import IdentityInvariantAudit
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.migration.migrate_sqlite import main as migrate_main
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import (
    ParentAssignment,
    ParentFacts,
    mint_parent_id,
)
from packs.ingestion.primitives.deep_context.migration.parent_graph import (
    clusters_from_pairs,
    plan_parents,
    singleton_plan,
)

Assignments = dict[str, str]


@dataclass
class ProofReport:
    """Non-identifying proof totals for one copied install."""

    source: str = ""
    migration_completed: bool = False
    migrated_people: int = 0
    slugs: int = 0
    parents_checked: int = 0
    parents_preserved: int = 0
    parents_lost: int = 0
    pairs_checked: int = 0
    pairs_preserved: int = 0
    pairs_changed: int = 0
    pairs_lost: int = 0
    cold_parents: int = 0
    cold_ids_identical: bool = False
    cold_partition_identical: bool = False
    incremental_seeded_children: int = 0
    incremental_added_children: int = 0
    incremental_parents_before: int = 0
    incremental_ids_unchanged: bool = False
    incremental_partition_matches_cold: bool = False
    identity_invariants_ok: bool = False
    identity_invariant_issues: int = 0
    identity_invariant_counts: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    status: str = "completed"


def assignments_of(snapshot: CanonicalSnapshot) -> Assignments:
    """Return every non-owner person assignment from the canonical snapshot."""
    return {row.person_id: row.parent_id for row in snapshot.people if not row.is_owner}


def partition_of(assignments: Assignments) -> set[frozenset[str]]:
    groups: dict[str, set[str]] = {}
    for person_id, parent_id in assignments.items():
        groups.setdefault(parent_id, set()).add(person_id)
    return {frozenset(group) for group in groups.values()}


def _planning_people(snapshot: CanonicalSnapshot) -> set[str]:
    owner_ids = {row.person_id for row in snapshot.facts if row.is_owner and row.person_id}
    return {row.person_id for row in snapshot.dossiers if row.person_id and row.person_id not in owner_ids}


def _plan(
    snapshot: CanonicalSnapshot,
    assignment: ParentAssignment,
    included_people: set[str],
) -> Assignments:
    """Run the production clustering and assignment policy over one snapshot."""
    dossiers = tuple(row for row in snapshot.dossiers if row.person_id and row.person_id in included_people)
    slugs_info = {
        row.slug: {
            "person_id": row.person_id,
            "name": row.name,
            "headline": row.headline,
            "full_name": row.full_name,
            "emails": list(row.emails),
            "phones": list(row.phones),
            "source_channels": list(row.source_channels),
        }
        for row in dossiers
    }
    slug_by_person = {str(info["person_id"]): slug for slug, info in slugs_info.items()}
    pairs = [
        {
            "slug_a": slug_by_person[row.person_a],
            "slug_b": slug_by_person[row.person_b],
            "confidence": str(row.confidence),
            "reason": row.reason,
        }
        for row in snapshot.merge_verdicts
        if row.accepted and row.person_a in slug_by_person and row.person_b in slug_by_person
    ]
    facts_by_person = {
        row.person_id: parse_json_object(row.facts_json)
        for row in snapshot.facts
        if row.person_id and row.person_id in included_people
    }
    owner_ids = {row.person_id for row in snapshot.facts if row.is_owner and row.person_id}
    owner_slugs = {slug for slug, info in slugs_info.items() if info["person_id"] in owner_ids}
    for slug in sorted(owner_slugs):
        assignment.reserve(mint_parent_id([str(slugs_info[slug]["person_id"])]))

    plans = plan_parents(
        clusters_from_pairs(pairs),
        pairs,
        slugs_info,
        owner_slugs,
        facts_by_person,
        assignment,
    )
    clustered = {child.slug for plan in plans for child in plan.confirmed}
    singletons = [
        singleton_plan(slug, info, assignment)
        for slug, info in slugs_info.items()
        if slug not in clustered and slug not in owner_slugs
    ]
    return {child.person_id: plan.parent_id for plan in (*plans, *singletons) for child in plan.confirmed}


def _seed_assignment(
    snapshot: CanonicalSnapshot,
    seeded: Assignments,
) -> ParentAssignment:
    slug_by_person = {row.person_id: row.slug for row in snapshot.dossiers if row.person_id}
    members = Counter(seeded.values())
    parents = {row.parent_id: row for row in snapshot.parents}
    facts = {}
    for parent_id, count in members.items():
        parent = parents.get(parent_id)
        facts[parent_id] = ParentFacts(
            int(parent is not None and parent.human_worth is not None),
            parent.human_worth_at or "" if parent else "",
            count,
        )
    return ParentAssignment(
        {
            slug_by_person[person_id]: parent_id
            for person_id, parent_id in seeded.items()
            if person_id in slug_by_person
        },
        facts,
    )


@contextmanager
def inside(workspace: Path) -> Iterator[None]:
    """Resolve the pipeline's fixed relative paths inside the sandbox."""
    previous = Path.cwd()
    os.chdir(workspace)
    try:
        yield
    finally:
        os.chdir(previous)


class ParentIdentityProof:
    """Copy one install and execute the five parent-identity proof legs."""

    def __init__(self, source: Path, keep: Path | None = None) -> None:
        self.source = source.resolve()
        self.keep = keep
        self.network_import = self.source.parent / "network-import"

    def stage(self, sandbox: Path) -> Path:
        workspace = sandbox / "workspace"
        shutil.copytree(self.source, workspace / ROOT, ignore=_skip_sockets)
        target = workspace / DEFAULT_BASE_DIR
        merged_people = self.network_import / "merged" / "people.csv"
        if merged_people.exists():
            (target / "merged").mkdir(parents=True, exist_ok=True)
            shutil.copy2(merged_people, target / "merged" / "people.csv")
        overrides = self.network_import / "overrides"
        if overrides.exists():
            shutil.copytree(overrides, target / "overrides")
        return workspace

    def migrate(self, workspace: Path) -> Db:
        """Cross the only filesystem-reader boundary exactly once."""
        with inside(workspace), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = migrate_main(["--db", str(CANONICAL_DB)])
        if result != 0:
            raise RuntimeError("legacy migration failed")
        return Db(workspace / CANONICAL_DB)

    def run(self) -> ProofReport:
        report = ProofReport(source=self.source.parent.parent.name)
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(self.keep) if self.keep else Path(directory)
            sandbox.mkdir(parents=True, exist_ok=True)
            workspace = self.stage(sandbox)
            try:
                db = self.migrate(workspace)
            except (OSError, RuntimeError):
                report.failures = ["legacy_migration"]
                report.status = "failed"
                return report

            report.migration_completed = True
            baseline_snapshot = canonical_snapshot(db)
            baseline = assignments_of(baseline_snapshot)
            planning_people = _planning_people(baseline_snapshot)
            planning_baseline = {
                person_id: parent_id for person_id, parent_id in baseline.items() if person_id in planning_people
            }
            report.migrated_people = len(baseline_snapshot.people)
            report.slugs = len(planning_people)
            report.parents_checked = len(set(baseline.values()))
            report.pairs_checked = len(baseline)

            projection = CanonicalGraphProjection(
                parents=baseline_snapshot.parents,
                people=baseline_snapshot.people,
                identifiers=baseline_snapshot.identifiers,
                sources=baseline_snapshot.sources,
            )
            LegacyGraphMigration.apply(db, projection)
            rebuilt = assignments_of(canonical_snapshot(db))
            identity_report = IdentityInvariantAudit(db).run()
            report.identity_invariants_ok = identity_report.ok
            report.identity_invariant_issues = len(identity_report.issues)
            report.identity_invariant_counts = dict(Counter(issue.code for issue in identity_report.issues))
            surviving = set(rebuilt.values())
            report.parents_preserved = sum(parent_id in surviving for parent_id in set(baseline.values()))
            report.parents_lost = report.parents_checked - report.parents_preserved
            report.pairs_preserved = sum(
                rebuilt.get(person_id) == parent_id for person_id, parent_id in baseline.items()
            )
            report.pairs_changed = sum(
                person_id in rebuilt and rebuilt[person_id] != parent_id for person_id, parent_id in baseline.items()
            )
            report.pairs_lost = sum(person_id not in rebuilt for person_id in baseline)

            cold = _plan(
                baseline_snapshot,
                ParentAssignment({}, {}),
                planning_people,
            )
            report.cold_parents = len(set(cold.values()))
            report.cold_ids_identical = cold == planning_baseline
            report.cold_partition_identical = partition_of(cold) == partition_of(planning_baseline)

            gmail_people = {
                row.person_id
                for row in baseline_snapshot.sources
                if row.person_id in planning_people and row.source == SourceChannel.GMAIL.value
            }
            seed_people = planning_people - gmail_people
            before = _plan(
                baseline_snapshot,
                ParentAssignment({}, {}),
                seed_people,
            )
            grown = _plan(
                baseline_snapshot,
                _seed_assignment(baseline_snapshot, before),
                planning_people,
            )
            report.incremental_seeded_children = len(before)
            report.incremental_added_children = len(grown.keys() - before.keys())
            report.incremental_parents_before = len(set(before.values()))
            report.incremental_ids_unchanged = all(
                grown.get(person_id) == parent_id for person_id, parent_id in before.items()
            )
            report.incremental_partition_matches_cold = partition_of(grown) == partition_of(cold)

        report.failures = [
            name
            for name, ok in (
                (
                    "preservation",
                    report.pairs_changed == 0 and report.pairs_lost == 0 and report.parents_lost == 0,
                ),
                ("cold_start_ids", report.cold_ids_identical),
                ("cold_start_partition", report.cold_partition_identical),
                ("incremental_ids", report.incremental_ids_unchanged),
                ("incremental_partition", report.incremental_partition_matches_cold),
                ("identity_invariants", report.identity_invariants_ok),
            )
            if not ok
        ]
        report.status = "failed" if report.failures else "completed"
        return report


def _skip_sockets(directory: str, names: list[str]) -> set[str]:
    """A live review UI socket cannot be copied and contains no proof state."""
    return {name for name in names if (Path(directory) / name).is_socket()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--deep-context",
        required=True,
        action="append",
        dest="deep_context",
        help="A real .powerpacks/deep-context directory (copied, never modified)",
    )
    parser.add_argument("--keep", help="Keep the sandbox at this path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    reports = [
        ParentIdentityProof(
            Path(source),
            Path(args.keep) / f"install-{position}" if args.keep else None,
        ).run()
        for position, source in enumerate(args.deep_context)
    ]
    failed = [report for report in reports if report.status == "failed"]
    emit(
        {
            "status": "failed" if failed else "completed",
            "installs": [asdict(report) for report in reports],
        }
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
