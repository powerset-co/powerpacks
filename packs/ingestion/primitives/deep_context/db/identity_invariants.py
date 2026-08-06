"""Read-only proofs for the parent-owned identity decision model."""

from __future__ import annotations

from dataclasses import dataclass

from packs.ingestion.primitives.deep_context.db.models import HUMAN_DECISION_SOURCES
from packs.ingestion.primitives.deep_context.db.schema import TABLES
from packs.ingestion.primitives.deep_context.db.store import Db


@dataclass(frozen=True)
class IdentityInvariantIssue:
    code: str
    owner: str
    detail: str


@dataclass(frozen=True)
class IdentityInvariantReport:
    parents_checked: int
    links_checked: int
    issues: tuple[IdentityInvariantIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


_EFFECTIVELY_APPROVED = """
CASE
  WHEN decision_action IS NOT NULL THEN
    decision_action IN ('verify', 'retarget') AND decision_approved='yes'
  ELSE
    machine_action IN ('verify', 'retarget') AND machine_approved IN ('auto', 'yes')
END
"""


def _schema_issues(db: Db) -> list[IdentityInvariantIssue]:
    issues = []
    for table in TABLES:
        for row in db.query(f"PRAGMA table_info({table})"):
            column = str(row["name"])
            allowed = (column.startswith("human_") and table == "parents") or (
                (column.startswith("decision_") or column == "decided_at") and table == "links"
            )
            if (
                column.startswith("human_") or column.startswith("decision_") or column == "decided_at"
            ) and not allowed:
                issues.append(
                    IdentityInvariantIssue(
                        "misplaced_decision_column",
                        table,
                        column,
                    )
                )
    return issues


class IdentityInvariantAudit:
    """Run the standing read-only proofs through one explicit DB service."""

    def __init__(self, db: Db) -> None:
        self.db = db

    def run(self) -> IdentityInvariantReport:
        """Check identity uniqueness, family settlement, and parent ownership."""
        issues = _schema_issues(self.db)
        approved = self.db.query(
            f"SELECT parent_id, count(*) AS approved FROM links "
            f"WHERE {_EFFECTIVELY_APPROVED} GROUP BY parent_id HAVING count(*) > 1"
        )
        issues.extend(
            IdentityInvariantIssue(
                "multiple_approved_candidates",
                str(row["parent_id"]),
                str(row["approved"]),
            )
            for row in approved
        )

        direct_sources = tuple(sorted(HUMAN_DECISION_SOURCES))
        source_slots = ",".join("?" for _ in direct_sources)
        unsettled = self.db.query(
            "SELECT decided.parent_id, count(*) AS undecided FROM "
            "(SELECT DISTINCT parent_id FROM links "
            f" WHERE decision_action IS NOT NULL AND decision_source IN ({source_slots})) decided "
            "JOIN links sibling USING(parent_id) "
            "WHERE sibling.decision_action IS NULL OR sibling.decision_approved IS NULL "
            "GROUP BY decided.parent_id",
            direct_sources,
        )
        issues.extend(
            IdentityInvariantIssue(
                "undecided_human_siblings",
                str(row["parent_id"]),
                str(row["undecided"]),
            )
            for row in unsettled
        )

        crossed = self.db.query(
            "SELECT cp.row_key, cp.person_id FROM candidate_people cp "
            "JOIN links l USING(row_key) JOIN people p USING(person_id) "
            "WHERE cp.parent_id!=l.parent_id OR cp.parent_id!=p.parent_id"
        )
        issues.extend(
            IdentityInvariantIssue(
                "cross_parent_candidate_membership",
                str(row["row_key"]),
                str(row["person_id"]),
            )
            for row in crossed
        )
        counts = self.db.query(
            "SELECT (SELECT count(*) FROM parents) AS parents, (SELECT count(*) FROM links) AS links"
        )[0]
        return IdentityInvariantReport(
            int(counts["parents"]),
            int(counts["links"]),
            tuple(issues),
        )
