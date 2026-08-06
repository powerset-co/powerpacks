"""Canonical parent files and SQLite membership move in one build pass."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.build_parents import BuildParents
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    FactRow,
    MergeVerdictRow,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.parents.assignment import mint_parent_id
from deep_context_sqlite_test_helpers import query


class ParentProjectionTest(unittest.TestCase):
    def test_minted_parent_id_keeps_the_founding_child_set_formula(self) -> None:
        self.assertEqual(mint_parent_id(["person-b", "person-a"]), "parent-65856992ac99")

    def test_singleton_markdown_and_sqlite_projection_stay_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            facts, raw = root / "facts", root / "raw"
            dossiers, parents = root / "dossiers", root / "parents"
            for path in (facts, raw, dossiers, parents):
                path.mkdir()
            child = {
                "person_id": "person-a", "name": "Jordan Bravo",
                "emails": ["jordan@example.com"], "phones": ["+15550100"],
                "headline": "Synthetic fixture headline.",
            }
            child_path = dossiers / "jordan-a.md"
            child_path.write_text("# Jordan Bravo\n\nBody\n")
            child_data = child_path.read_bytes()
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("old-person-a", "parent-worth:old-person-a", "Jordan Bravo", "jordan-a"),
                PersonRow("person-a", "old-person-a", "jordan-a", "jordan-a", "Jordan Bravo"),
                PersonIdentifiersProjection("person-a", (
                    PersonIdentifierRow("person-a", "email", "jordan@example.com", "jordan@example.com"),
                    PersonIdentifierRow("person-a", "phone", "+15550100", "+15550100"),
                )),
                ArtifactRow(
                    "dossier-person:person-a", "dossier", "old-person-a",
                    str(child_path), hashlib.sha256(child_data).hexdigest(), "projected",
                    person_id="person-a", payload_json=json.dumps({
                        **child,
                        "body": "# Jordan Bravo\n\nBody\n",
                        "source_channels": [],
                    }),
                ),
            ))
            with mock.patch(
                "packs.ingestion.primitives.deep_context.parents.rendering.now_iso",
                return_value="2026-01-02T03:04:05Z",
            ):
                child_path.unlink()
                BuildParents(
                    db=db,
                    parents_dir=parents,
                ).execute()

            # Get-or-create: the child's existing parent is absorbed, never
            # re-minted from membership — the seeded id survives the rebuild.
            parent_id = "old-person-a"
            parent_slug = "jordan-bravo-oldperso"
            self.assertEqual((parents / f"{parent_slug}.md").read_text(), (
                "---\n"
                f"parent_id: {parent_id}\n"
                'name: "Jordan Bravo"\n'
                f"slug: {parent_slug}\n"
                "kind: parent\n"
                "singleton: true\n"
                'children: ["jordan-a"]\n'
                'emails: ["jordan@example.com"]\n'
                'phones: ["+15550100"]\n'
                "generated_at: 2026-01-02T03:04:05Z\n"
                "---\n\n# Jordan Bravo\n\n"
                "Single identity — no duplicates detected. Full context in [[jordan-a]].\n\n"
                "Synthetic fixture headline.\n"
            ))
            parent_dossier = next(
                row
                for row in canonical_snapshot(db).artifacts
                if row.kind == "dossier" and row.parent_id == parent_id and not row.person_id
            )
            self.assertEqual(
                parent_dossier.path,
                str((parents / f"{parent_slug}.md").resolve()),
            )

    def test_merge_rekeys_facts_and_preserves_human_worth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            facts_dir, raw_dir = root / "facts", root / "raw"
            dossier_dir, parents_dir = root / "dossiers", root / "parents"
            for path in (facts_dir, raw_dir, dossier_dir, parents_dir):
                path.mkdir()
            people = (("person-a", "jordan-a"), ("person-b", "jordan-b"))
            merge_path = root / "merge.csv"
            merge_path.write_bytes(b"legacy merge csv must stay untouched\n")

            db = Db(root / "deep-context.sqlite")
            projection_rows = []
            for person_id, slug in people:
                parent_id = f"old-{person_id}"
                fact_path = facts_dir / f"{person_id}.jsonl"
                payload = {
                    "facts": {
                        "canonical_name": "Jordan Bravo",
                        "network_worth": {"decision": "yes", "reason": "known collaborator"},
                    }
                }
                data = (json.dumps(payload) + "\n").encode()
                fact_path.write_bytes(data)
                (raw_dir / f"{person_id}.json").write_text(
                    json.dumps({"source_channels": ["gmail_msgvault"]}),
                    encoding="utf-8",
                )
                (dossier_dir / f"{slug}.md").write_text(
                    f"# Jordan Bravo\n\n{person_id}\n",
                    encoding="utf-8",
                )
                artifact_key = f"facts:{person_id}"
                projection_rows.extend(
                    (
                        ParentRow(
                            parent_id,
                            f"parent-worth:{parent_id}",
                            "Jordan Bravo",
                            slug,
                        ),
                        PersonRow(
                            person_id,
                            parent_id,
                            slug,
                            slug,
                            "Jordan Bravo",
                        ),
                        PersonIdentifiersProjection(person_id, (
                            PersonIdentifierRow(
                                person_id, "email", f"{person_id}@example.com",
                                f"{person_id}@example.com",
                            ),
                        )),
                        ArtifactRow(
                            f"dossier-person:{person_id}", "dossier", parent_id,
                            str(dossier_dir / f"{slug}.md"),
                            hashlib.sha256((dossier_dir / f"{slug}.md").read_bytes()).hexdigest(),
                            "projected", person_id=person_id,
                            payload_json=json.dumps({
                                "person_id": person_id, "name": "Jordan Bravo",
                                "path": f"dossiers/{slug}.md", "headline": "",
                                "full_name": "Jordan Bravo",
                                "emails": [f"{person_id}@example.com"], "phones": [],
                                "source_channels": ["gmail_msgvault"],
                                "body": f"# Jordan Bravo\n\n{person_id}\n",
                            }),
                        ),
                        ArtifactRow(
                            artifact_key,
                            "facts",
                            parent_id,
                            str(fact_path),
                            hashlib.sha256(data).hexdigest(),
                            "projected",
                            person_id=person_id,
                        ),
                        FactRow(
                            person_id,
                            parent_id,
                            artifact_key,
                            person_id,
                            "yes",
                            "known collaborator",
                            facts_json=json.dumps(payload["facts"]),
                        ),
                    )
                )
            db.project_rows(tuple(projection_rows))
            db.replace_merge_verdicts((MergeVerdictRow(
                "person-a", "person-b", "jordan-a", "jordan-b", "sig",
                "llm", 1, 0.99, 1, "synthetic fixture", 1,
            ),))
            db.decide_worth("old-person-a", "yes")
            for input_dir in (facts_dir, raw_dir, dossier_dir):
                for path in input_dir.iterdir():
                    path.unlink()

            result = BuildParents(
                db=db,
                parents_dir=parents_dir,
            ).execute()

            self.assertEqual((result.parents_written, result.merged_parents), (1, 1))
            parents = query(db, "SELECT parent_id, human_worth FROM parents")
            self.assertEqual(len(parents), 1)
            self.assertEqual(parents[0]["human_worth"], "yes")
            parent_id = parents[0]["parent_id"]
            # |E|>1 elects the parent carrying the human worth decision, and every
            # dependent row is repointed onto it in the one replacement transaction.
            self.assertEqual(parent_id, "old-person-a")
            self.assertEqual(
                {row["parent_id"] for row in query(db, "SELECT parent_id FROM people")},
                {parent_id},
            )
            self.assertEqual(
                {row["parent_id"] for row in query(db, "SELECT parent_id FROM facts")},
                {parent_id},
            )
            self.assertEqual(result.worth_parent_rows, 1)
            self.assertEqual(query(db, "SELECT count(*) FROM person_identifiers")[0][0], 2)
            self.assertEqual(query(db, "SELECT count(*) FROM person_sources")[0][0], 2)
            self.assertEqual(merge_path.read_bytes(), b"legacy merge csv must stay untouched\n")

    def test_existing_multi_child_parent_stays_intact_without_verdict_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-family", "parent-worth:family", "Jordan Bravo", "jordan"),
                PersonRow("child-a", "parent-family", "jordan-a", "jordan", "Jordan Bravo"),
                PersonRow("child-b", "parent-family", "jordan-b", "jordan", "Jordan Bravo"),
            ))

            BuildParents(db=db, parents_dir=root / "parents").execute()

            rows = query(db, "SELECT person_id, parent_id FROM people ORDER BY person_id")
            self.assertEqual([tuple(row) for row in rows], [
                ("child-a", "parent-family"),
                ("child-b", "parent-family"),
            ])

    def test_accepted_representative_edge_merges_whole_parent_families(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-alpha", "parent-worth:alpha", "Jordan Alpha", "alpha"),
                ParentRow("parent-bravo", "parent-worth:bravo", "Jordan Bravo", "bravo"),
                PersonRow("alpha-a", "parent-alpha", "alpha-a", "alpha", "Jordan Alpha"),
                PersonRow("alpha-b", "parent-alpha", "alpha-b", "alpha", "Jordan Alpha"),
                PersonRow("bravo-a", "parent-bravo", "bravo-a", "bravo", "Jordan Bravo"),
                PersonRow("bravo-b", "parent-bravo", "bravo-b", "bravo", "Jordan Bravo"),
            ))
            db.replace_merge_verdicts((MergeVerdictRow(
                "alpha-a", "bravo-a", "alpha", "bravo", "evidence-v1",
                "llm", 1, 0.95, 1, "same synthetic person", 1,
            ),))

            BuildParents(db=db, parents_dir=root / "parents").execute()

            parents = {
                row["parent_id"] for row in query(db, "SELECT parent_id FROM people")
            }
            self.assertEqual(parents, {"parent-alpha"})
            self.assertEqual(query(db, "SELECT count(*) FROM people")[0][0], 4)

    def test_newer_parent_pair_rejection_supersedes_stale_accepted_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-alpha", "parent-worth:alpha", "Jordan Alpha", "alpha"),
                ParentRow("parent-bravo", "parent-worth:bravo", "Jordan Bravo", "bravo"),
                PersonRow("alpha-a", "parent-alpha", "alpha-a", "alpha", "Jordan Alpha"),
                PersonRow("alpha-b", "parent-alpha", "alpha-b", "alpha", "Jordan Alpha"),
                PersonRow("bravo-a", "parent-bravo", "bravo-a", "bravo", "Jordan Bravo"),
                PersonRow("bravo-b", "parent-bravo", "bravo-b", "bravo", "Jordan Bravo"),
            ))
            db.replace_merge_verdicts((
                MergeVerdictRow(
                    "alpha-a", "bravo-a", "alpha", "bravo", "old-evidence",
                    "llm", 1, 0.95, 1, "old accept", 1, "2026-08-05T00:00:00Z",
                ),
                MergeVerdictRow(
                    "alpha-b", "bravo-b", "alpha", "bravo", "new-evidence",
                    "llm", 0, 0.95, 1, "new reject", 0, "2026-08-06T00:00:00Z",
                ),
            ))

            BuildParents(db=db, parents_dir=root / "parents").execute()

            rows = query(db, "SELECT person_id, parent_id FROM people ORDER BY person_id")
            self.assertEqual([tuple(row) for row in rows], [
                ("alpha-a", "parent-alpha"),
                ("alpha-b", "parent-alpha"),
                ("bravo-a", "parent-bravo"),
                ("bravo-b", "parent-bravo"),
            ])


if __name__ == "__main__":
    unittest.main()
