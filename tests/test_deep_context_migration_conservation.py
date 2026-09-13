import csv
import json
import os
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.identity_queries import research_rows
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import ResearchQueueRow, filter_already_done
from packs.ingestion.primitives.deep_context.migration.legacy import import_legacy, LegacyImportError


class MigrationConservationTests(unittest.TestCase):
    def _index(self, root: Path) -> Path:
        path = root / "index.json"
        path.write_text(json.dumps({
            "parents": {"casey-current": {"parent_id": "parent-casey", "children": ["casey"], "name": "Casey Example"}},
            "slugs": {"casey": {"person_id": "person-casey", "name": "Casey Example", "path": "casey.md"}},
        }))
        return path

    def _research(self, root: Path, handles: tuple[str, ...]) -> Path:
        directory = root / "research"
        directory.mkdir()
        with (directory / "research_queue.csv").open("w") as handle:
            writer = csv.DictWriter(handle, fieldnames=["handle", "source_person_ids"])
            writer.writeheader()
            for slug in handles:
                writer.writerow({"handle": slug, "source_person_ids": '["person-casey"]'})
        for position, slug in enumerate(handles):
            target = directory / slug / "01_research_parallel.json"
            target.parent.mkdir()
            target.write_text(json.dumps({
                "person": {"full_name": "Casey Example"},
                "summary": {"text": slug},
                "metadata": {"research_date": f"2026-08-{position + 10:02d}"},
            }))
            # Restored filesystem timestamps must not decide which result is newest.
            os.utime(target, (100 - position, 100 - position))
        return directory

    def test_renamed_research_keeps_all_results_and_reuses_current_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            research = self._research(root, ("casey-old-z", "casey-old-a"))
            db = Db(root / "state.sqlite")
            import_legacy(db, review_csv=root / "missing.csv", index_json=self._index(root), research_dir=research)
            rows = {row.handle: row for row in research_rows(db)}
            self.assertEqual(set(rows), {"casey-old-z", "casey-old-a", "casey-current"})
            self.assertEqual(json.loads(rows["casey-current"].result_json)["content"]["summary"], "casey-old-a")
            self.assertEqual({row.parent_id for row in rows.values()}, {"parent-casey"})
            queue = [ResearchQueueRow("parent-casey", False, "person-casey", "casey-current", ("person-casey",), "Casey Example")]
            pending, reused = filter_already_done(queue, queries.artifacts(db, kind="research"))
            self.assertEqual((pending, reused), ([], 1))

    def test_current_research_result_is_not_replaced_by_an_old_handle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            research = self._research(root, ("casey-current", "casey-old"))
            db = Db(root / "state.sqlite")
            import_legacy(db, review_csv=root / "missing.csv", index_json=self._index(root), research_dir=research)
            rows = {row.handle: row for row in research_rows(db)}
            self.assertEqual(set(rows), {"casey-current", "casey-old"})
            self.assertEqual(json.loads(rows["casey-current"].result_json)["content"]["summary"], "casey-current")

    def test_research_without_known_ownership_is_refused_instead_of_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            research = self._research(root, ("casey-old",))
            (research / "research_queue.csv").write_text('handle,source_person_ids\ncasey-old,"[""unknown-person""]"\n')
            db = Db(root / "state.sqlite")
            with self.assertRaisesRegex(LegacyImportError, "research.*owner"):
                import_legacy(db, review_csv=root / "missing.csv", index_json=self._index(root), research_dir=research)
            self.assertEqual(db.query("SELECT * FROM people"), [])

    def test_approved_child_retarget_survives_another_members_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            person = "candidate:email:casey@example.com"
            other = "candidate:email:casey@work.example"
            index = root / "index.json"
            index.write_text(json.dumps({
                "parents": {"casey": {"parent_id": "parent-casey", "children": ["home", "work"]}},
                "slugs": {"home": {"person_id": person}, "work": {"person_id": other}},
            }))
            facts = root / "facts"
            facts.mkdir()
            (facts / f"{person}.jsonl").write_text(json.dumps({"facts": {"network_worth": {"decision": "yes"}}}) + "\n")
            review = root / "review.csv"
            with review.open("w") as handle:
                writer = csv.DictWriter(handle, fieldnames=["public_identifier", "person_id", "action", "approved", "new_linkedin_url"])
                writer.writeheader()
                writer.writerow({"public_identifier": person, "person_id": person, "action": "retarget", "approved": "auto",
                                 "new_linkedin_url": "https://www.linkedin.com/in/casey-example"})
            verdicts = root / "verdicts.jsonl"
            verdicts.write_text(json.dumps({"parent_slug": "casey", "person_ids": [other, person], "no_link": True,
                                           "verdict": {"verdict": "needs_review"}}) + "\n")
            db = Db(root / "state.sqlite")
            import_legacy(db, review_csv=review, index_json=index, facts_dir=facts, verdicts_jsonl=verdicts)
            rows = db.query("SELECT * FROM links WHERE row_key=?", (person,))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["machine_action"], "retarget")
            self.assertEqual(rows[0]["machine_approved"], "auto")
            self.assertEqual(rows[0]["machine_proposed_url"], "https://www.linkedin.com/in/casey-example")

    def test_dossier_channels_survive_without_synthetic_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "casey.md").write_text('---\nperson_id: person-casey\nsource_channels: ["gmail_msgvault", "whatsapp"]\n---\n# Casey Example\n')
            db = Db(root / "state.sqlite")
            import_legacy(db, review_csv=root / "missing.csv", index_json=self._index(root))
            self.assertEqual(
                {(row["person_id"], row["source"]) for row in db.query("SELECT * FROM person_sources")},
                {("person-casey", "gmail_msgvault"), ("person-casey", "whatsapp")},
            )

    def test_owner_dossier_outside_parent_groups_keeps_body_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = self._index(root)
            payload = json.loads(index.read_text())
            payload["parents"] = {}
            index.write_text(json.dumps(payload))
            body = '---\nperson_id: person-casey\nsource_channels: ["gmail_msgvault"]\n---\n# Casey Example\n'
            (root / "casey.md").write_text(body)
            facts = root / "facts"
            facts.mkdir()
            (facts / "person-casey.jsonl").write_text(json.dumps({
                "facts": {"canonical_name": "Casey Example", "is_owner": True},
            }) + "\n")
            db = Db(root / "state.sqlite")
            import_legacy(db, review_csv=root / "missing.csv", index_json=index, facts_dir=facts)
            dossiers = queries.artifacts(db, kind="dossier")
            self.assertEqual(len(dossiers), 1)
            self.assertEqual(dossiers[0].person_id, "person-casey")
            self.assertEqual(json.loads(dossiers[0].payload_json)["body"], body)
            self.assertEqual(db.query("SELECT is_owner FROM people")[0]["is_owner"], 1)
            self.assertEqual(
                [(row["person_id"], row["source"]) for row in db.query("SELECT * FROM person_sources")],
                [("person-casey", "gmail_msgvault")],
            )


if __name__ == "__main__":
    unittest.main()
