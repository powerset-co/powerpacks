"""Canonical parent files and SQLite membership move in one build pass."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.merge_candidates.build_parents import BuildParents
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    FactRow,
    MergeVerdictRow,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
    PersonSourceRow,
    PersonSourcesProjection,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import mint_parent_id
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
            child_path.unlink()
            BuildParents(db=db, parents_dir=parents).execute()

            # Get-or-create: the child's existing parent is absorbed, never
            # re-minted from membership — the seeded id survives the rebuild.
            parent_id = "old-person-a"
            parent_slug = "jordan-a"
            parent_filename = "jordan-bravo-oldperso.md"
            self.assertEqual((parents / parent_filename).read_text(), (
                "---\n"
                f"parent_id: {parent_id}\n"
                'name: "Jordan Bravo"\n'
                f"slug: {parent_slug}\n"
                "kind: parent\n"
                "singleton: true\n"
                'children: ["jordan-a"]\n'
                'emails: ["jordan@example.com"]\n'
                'phones: ["+15550100"]\n'
                "---\n\n# Jordan Bravo\n\n"
                "Single identity — no duplicates detected. Full context in [[jordan-a]].\n"
            ))
            parent_dossier = next(
                row
                for row in canonical_snapshot(db).artifacts
                if row.kind == "dossier" and row.parent_id == parent_id and not row.person_id
            )
            self.assertEqual(
                parent_dossier.path,
                str((parents / parent_filename).resolve()),
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
                        PersonSourcesProjection(person_id, (
                            PersonSourceRow(person_id, "gmail_msgvault"),
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

            self.assertEqual((result.parents_changed, result.parents_merged), (1, 1))
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

    def test_unchanged_parent_dossier_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-a", "parent-worth:a", "Jordan Bravo", "jordan"),
                PersonRow("person-a", "parent-a", "jordan-a", "jordan", "Jordan Bravo"),
                PersonIdentifiersProjection("person-a", (
                    PersonIdentifierRow(
                        "person-a", "email", "jordan@example.com", "jordan@example.com",
                    ),
                )),
            ))
            parents_dir = root / "parents"

            first = BuildParents(db=db, parents_dir=parents_dir).execute()
            path = parents_dir / "jordan-bravo-a.md"
            first_bytes = path.read_bytes()
            first_mtime = path.stat().st_mtime_ns
            artifact_fingerprint = query(
                db,
                "SELECT content_fingerprint FROM artifacts "
                "WHERE artifact_key='dossier-parent:parent-a'",
            )[0][0]

            with mock.patch(
                "packs.ingestion.primitives.deep_context.merge_candidates.rendering.render_singleton",
                side_effect=AssertionError("unchanged parent must not render"),
            ):
                second = BuildParents(db=db, parents_dir=parents_dir).execute()

            self.assertEqual(first.parents_changed, 1)
            self.assertEqual(second.parents_changed, 0)
            self.assertEqual(first.singletons_written, 1)
            self.assertNotIn("parents_written", first.to_payload())
            self.assertNotIn("singleton_parents", first.to_payload())
            self.assertEqual(path.read_bytes(), first_bytes)
            self.assertEqual(path.stat().st_mtime_ns, first_mtime)
            self.assertEqual(
                query(
                    db,
                    "SELECT content_fingerprint FROM artifacts "
                    "WHERE artifact_key='dossier-parent:parent-a'",
                )[0][0],
                artifact_fingerprint,
            )
            artifact = query(
                db,
                "SELECT input_fingerprint, payload_json FROM artifacts "
                "WHERE artifact_key='dossier-parent:parent-a'",
            )[0]
            self.assertTrue(artifact["input_fingerprint"])
            self.assertNotIn("person_ids", json.loads(artifact["payload_json"]))

    def test_fact_change_rewrites_only_its_parent_dossier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-a", "parent-worth:a", "Jordan Bravo", "jordan"),
                ParentRow("parent-b", "parent-worth:b", "Casey Delta", "casey"),
                PersonRow("person-a", "parent-a", "jordan-a", "jordan", "Jordan Bravo"),
                PersonRow("person-b", "parent-b", "casey-b", "casey", "Casey Delta"),
            ))
            parents_dir = root / "parents"
            BuildParents(db=db, parents_dir=parents_dir).execute()
            jordan = parents_dir / "jordan-bravo-a.md"
            casey = parents_dir / "casey-delta-b.md"
            jordan_before = jordan.read_bytes()
            casey_before = casey.read_bytes()
            casey_mtime = casey.stat().st_mtime_ns
            prior_input = query(
                db,
                "SELECT input_fingerprint FROM artifacts "
                "WHERE artifact_key='dossier-parent:parent-a'",
            )[0][0]

            facts = {"canonical_name": "Jordan Bravo", "title": "Engineer"}
            facts_json = json.dumps(facts)
            db.project_rows((
                ArtifactRow(
                    "facts:parent-a",
                    "facts",
                    "parent-a",
                    str(root / "facts" / "parent-a.jsonl"),
                    hashlib.sha256(facts_json.encode()).hexdigest(),
                    "projected",
                ),
                FactRow(
                    "parent-a",
                    "parent-a",
                    "facts:parent-a",
                    facts_json=facts_json,
                ),
            ))

            result = BuildParents(db=db, parents_dir=parents_dir).execute()

            self.assertEqual(result.parents_changed, 1)
            # The old singleton contract intentionally does not derive a summary
            # from structured facts, but the input signal still advances.
            self.assertEqual(jordan.read_bytes(), jordan_before)
            self.assertNotEqual(
                query(
                    db,
                    "SELECT input_fingerprint FROM artifacts "
                    "WHERE artifact_key='dossier-parent:parent-a'",
                )[0][0],
                prior_input,
            )
            self.assertEqual(casey.read_bytes(), casey_before)
            self.assertEqual(casey.stat().st_mtime_ns, casey_mtime)
            with mock.patch(
                "packs.ingestion.primitives.deep_context.merge_candidates.rendering.render_singleton",
                side_effect=AssertionError("advanced input signal must converge"),
            ):
                converged = BuildParents(db=db, parents_dir=parents_dir).execute()
            self.assertEqual(converged.parents_changed, 0)

    def test_membership_change_rewrites_only_its_parent_dossier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-a", "parent-worth:a", "Jordan Bravo", "jordan"),
                ParentRow("parent-b", "parent-worth:b", "Casey Delta", "casey"),
                PersonRow("person-a", "parent-a", "jordan-a", "jordan", "Jordan Bravo"),
                PersonRow("person-b", "parent-b", "casey-b", "casey", "Casey Delta"),
            ))
            parents_dir = root / "parents"
            BuildParents(db=db, parents_dir=parents_dir).execute()
            jordan = parents_dir / "jordan-bravo-a.md"
            casey = parents_dir / "casey-delta-b.md"
            jordan_before = jordan.read_bytes()
            casey_before = casey.read_bytes()
            casey_mtime = casey.stat().st_mtime_ns
            db.project_rows((
                PersonRow("person-c", "parent-a", "jordan-c", "jordan", "Jordan Bravo"),
            ))

            result = BuildParents(db=db, parents_dir=parents_dir).execute()

            self.assertEqual(result.parents_changed, 1)
            self.assertNotEqual(jordan.read_bytes(), jordan_before)
            self.assertEqual(casey.read_bytes(), casey_before)
            self.assertEqual(casey.stat().st_mtime_ns, casey_mtime)

    def test_colliding_display_slugs_heal_to_distinct_parent_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parents_dir = root / "parents"
            parents_dir.mkdir()
            collided = parents_dir / "jordan.md"
            collided.write_text("stale shared dossier\n", encoding="utf-8")
            stale_fingerprint = hashlib.sha256(collided.read_bytes()).hexdigest()
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-a", "parent-worth:a", "Jordan Bravo", "jordan"),
                ParentRow("parent-b", "parent-worth:b", "Jordan Bravo", "jordan"),
                PersonRow("person-a", "parent-a", "jordan-a", "jordan", "Jordan Bravo"),
                PersonRow("person-b", "parent-b", "jordan-b", "jordan", "Jordan Bravo"),
                ArtifactRow(
                    "dossier:parent-a", "dossier", "parent-a", str(collided.resolve()),
                    stale_fingerprint, "projected",
                ),
                ArtifactRow(
                    "dossier:parent-b", "dossier", "parent-b", str(collided.resolve()),
                    stale_fingerprint, "projected",
                ),
            ))

            result = BuildParents(db=db, parents_dir=parents_dir).execute()

            self.assertEqual(result.parents_changed, 2)
            self.assertFalse(collided.exists())
            rows = query(
                db,
                "SELECT artifact_key, path FROM artifacts "
                "WHERE kind='dossier' ORDER BY parent_id",
            )
            self.assertEqual(
                [row["artifact_key"] for row in rows],
                ["dossier-parent:parent-a", "dossier-parent:parent-b"],
            )
            paths = {Path(row["path"]) for row in rows}
            self.assertEqual(paths, {
                (parents_dir / "jordan-bravo-a.md").resolve(),
                (parents_dir / "jordan-bravo-b.md").resolve(),
            })
            for path in paths:
                self.assertTrue(path.is_file())

    def test_disk_fingerprint_mismatch_is_healed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parents_dir = root / "parents"
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-a", "parent-worth:a", "Jordan Bravo", "jordan"),
                PersonRow("person-a", "parent-a", "jordan-a", "jordan", "Jordan Bravo"),
            ))
            BuildParents(db=db, parents_dir=parents_dir).execute()
            path = parents_dir / "jordan-bravo-a.md"
            expected = path.read_bytes()
            path.write_text("corrupted dossier\n", encoding="utf-8")

            result = BuildParents(db=db, parents_dir=parents_dir).execute()

            self.assertEqual(result.parents_changed, 1)
            self.assertEqual(path.read_bytes(), expected)
            row = query(
                db,
                "SELECT content_fingerprint FROM artifacts "
                "WHERE artifact_key='dossier-parent:parent-a'",
            )[0]
            self.assertEqual(row["content_fingerprint"], hashlib.sha256(expected).hexdigest())


if __name__ == "__main__":
    unittest.main()
