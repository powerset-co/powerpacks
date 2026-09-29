from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from dataclasses import replace
from unittest import mock

from packs.ingestion.primitives.deep_context.shared.check_readiness import CheckReadiness
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.collection.models import ChatDbProbe
from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import (
    project_imported_people,
    read_imported_people,
)
from packs.ingestion.primitives.deep_context.db.models import ArtifactRow, FactRow, LinkRow
from packs.ingestion.primitives.deep_context.db.identity_queries import links
from packs.ingestion.primitives.deep_context.db.identity_views import enrichment_queue, linkedin_queue
from packs.ingestion.primitives.deep_context.db.workflow_views import workflow_state
from packs.ingestion.primitives.deep_context.realize.export_people import ExportPeople
from packs.ingestion.primitives.imports.merge_people import PeopleMerge, merge_group
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.ingestion.primitives.share.share_list import ShareList
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.shared.csv_io import CsvIO


FIELDS = [
    "id",
    "full_name",
    "primary_email",
    "all_emails",
    "primary_phone",
    "all_phones",
    "source_channels",
    "superseded_person_ids",
    "headline",
    "public_identifier",
    "linkedin_url",
]


class ImportedPeopleBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.csv = self.root / "people.csv"
        self.db = Db(self.root / "deep-context.sqlite")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, rows: list[dict[str, str]]) -> None:
        CsvIO.write_dict_rows(self.csv, FIELDS, rows)

    def test_roster_survives_removing_import_csv(self) -> None:
        from packs.ingestion.primitives.deep_context.synthesis.runner import parent_headlines

        self.write([{"id": "person-1", "full_name": "Jordan Bravo", "headline": "Founder",
                     "source_channels": "linkedin_csv", "public_identifier": "jordan-bravo"}])
        EnsureParents(db=self.db, people_csv=self.csv).run()
        self.csv.rename(self.csv.with_suffix(".csv.bkup"))
        self.assertEqual(queries.imported_people(self.db)[0].headline, "Founder")
        self.assertEqual(queries.imported_people(self.db)[0].id, "person-1")
        self.assertEqual(list(parent_headlines(self.db).values()), ["Founder"])
        self.assertEqual(ShareEvidence(self.db).load()[0].full_name, "Jordan Bravo")

    def test_imported_link_is_reviewable_after_reset_and_reruns_preserve_decisions(self) -> None:
        self.write([{
            "id": "person-1", "full_name": "Jordan Bravo",
            "primary_email": "jordan@example.test", "source_channels": "gmail_msgvault",
            "public_identifier": "jordan-bravo",
            "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
        }])
        imported = read_imported_people(self.csv)
        project_imported_people(self.db, imported)
        parent_id = canonical_snapshot(self.db).people[0].parent_id
        self.db.project_rows((
            ArtifactRow("facts:one", "facts", parent_id, "/facts/one", "sha", "projected"),
            FactRow(parent_id, parent_id, "facts:one", machine_worth="yes", facts_json="{}"),
        ))
        self.assertEqual(workflow_state(self.db).next_action, "enrich")
        self.assertEqual(enrichment_queue(self.db), [])
        candidate = linkedin_queue(self.db)[0].candidates[0]
        self.assertEqual(candidate.row_key, "jordan-bravo")
        self.db.decide_identity(candidate.row_key, "verify", approved="yes")
        project_imported_people(self.db, imported)
        self.assertEqual(links(self.db)[0].decision_action, "verify")
        self.assertEqual(ExportPeople(db=self.db, out_dir=self.root / "merged").run()["accepted_identities"], 1)
        self.db.reset_review()
        self.assertEqual(workflow_state(self.db).next_action, "enrich")
        self.assertEqual(links(self.db)[0].linkedin_url, imported[0].linkedin_url)

        self.db.project_rows((LinkRow(
            "jordan-bravo", parent_id, "jordan-bravo", "pub",
            imported[0].linkedin_url, source="deep-context-reconcile", machine_action="verify", machine_approved="auto",
        ),))
        project_imported_people(self.db, imported)
        self.assertEqual(links(self.db)[0].machine_approved, "auto")
        self.assertEqual(workflow_state(self.db).next_action, "realize")

    def test_merge_promotion_preserves_parent_and_human_worth(self) -> None:
        original = PeopleRow(id="candidate:email:jordan@example.test", full_name="Jordan Bravo",
                             primary_email="jordan@example.test", source_channels="gmail_msgvault")
        CsvIO.write_dict_rows(self.csv, PEOPLE_SCHEMA_COLUMNS, [original.to_row()])
        project_imported_people(self.db, read_imported_people(self.csv))
        parent_id = canonical_snapshot(self.db).people[0].parent_id
        self.db.decide_worth(parent_id, "yes")
        promoted = original.model_copy(update={"public_identifier": "jordan-bravo"})
        merged = merge_group("linkedin:jordan-bravo", [promoted])
        CsvIO.write_dict_rows(self.csv, PEOPLE_SCHEMA_COLUMNS, [merged])
        EnsureParents(db=self.db, people_csv=self.csv).run()
        snapshot = canonical_snapshot(self.db)
        self.assertEqual(len(snapshot.parents), 1)
        self.assertEqual(snapshot.parents[0].human_worth, "yes")
        self.assertEqual({person.parent_id for person in snapshot.people}, {parent_id})

    def test_realized_retarget_does_not_create_a_second_review(self) -> None:
        self.write([{"id": "candidate:email:jordan@example.test", "full_name": "Jordan Bravo"}])
        project_imported_people(self.db, read_imported_people(self.csv))
        parent_id = canonical_snapshot(self.db).people[0].parent_id
        self.db.project_rows((LinkRow("research:one", parent_id, "", "research", paid_profile=True, source="deep-context-reconcile"),))
        self.db.decide_identity("research:one", "retarget", approved="yes",
                                replacement_url="https://www.linkedin.com/in/jordan-bravo",
                                replacement_public_identifier="jordan-bravo")
        incoming = replace(read_imported_people(self.csv)[0], person_id="linkedin-person-1",
                           superseded_person_ids=("candidate:email:jordan@example.test",),
                           public_identifier="jordan-bravo",
                           linkedin_url="https://www.linkedin.com/in/jordan-bravo")
        project_imported_people(self.db, (incoming,))
        self.assertEqual([row.row_key for row in links(self.db)],
                         ["candidate:email:jordan@example.test", "research:one"])

    def test_verified_research_uses_its_slug_and_does_not_merge_unlinked_people(self) -> None:
        self.write([
            {"id": "old-person", "full_name": "Jordan Bravo"},
            {"id": "unlinked-person", "full_name": "Casey Delta"},
        ])
        project_imported_people(self.db, read_imported_people(self.csv))
        parent_id = next(row.parent_id for row in canonical_snapshot(self.db).people if row.person_id == "old-person")
        self.db.project_rows((LinkRow("research:one", parent_id, "jordan-bravo", "research",
                                     paid_profile=True, source="deep-context-reconcile"),))
        self.db.decide_identity("research:one", "verify", approved="yes")
        project_imported_people(self.db, read_imported_people(self.csv))
        self.assertEqual(len(canonical_snapshot(self.db).parents), 2)
        incoming = replace(read_imported_people(self.csv)[0], person_id="new-person",
                           public_identifier="jordan-bravo",
                           linkedin_url="https://www.linkedin.com/in/jordan-bravo")
        project_imported_people(self.db, (incoming,))
        self.assertEqual(len(canonical_snapshot(self.db).parents), 2)
        self.assertEqual([row.row_key for row in links(self.db)], ["research:one"])

    def test_realize_retarget_keeps_the_old_linkedin_parent_and_share_worth(self) -> None:
        source = self.root / "source.csv"
        directory = self.root / "directory.csv"
        CsvIO.write_dict_rows(source, PEOPLE_SCHEMA_COLUMNS, [PeopleRow(
            id="candidate:email:jordan@example.test", full_name="Jordan Bravo",
            primary_email="jordan@example.test", source_channels="gmail_msgvault",
        ).to_row()])
        CsvIO.write_dict_rows(directory, ["source", "status", "email", "public_identifier", "confidence"], [{
            "source": "gmail_msgvault", "status": "found", "email": "jordan@example.test",
            "public_identifier": "jordan-old", "confidence": "1.00",
        }])
        merge = PeopleMerge(inputs=[source], output_dir=self.root / "merged", directory_csv=directory,
                            profile_cache_dir=self.root / "cache")
        merge.run()
        EnsureParents(db=self.db, people_csv=merge.people_csv).run()
        parent_id = links(self.db)[0].parent_id
        self.db.decide_worth(parent_id, "yes")
        self.db.decide_identity("jordan-old", "retarget", approved="yes",
                                replacement_url="https://www.linkedin.com/in/jordan-new",
                                replacement_public_identifier="jordan-new")
        ExportPeople(db=self.db, out_dir=self.root / "export").run()
        snapshot = canonical_snapshot(self.db)
        self.assertEqual(len(snapshot.parents), 1)
        self.assertEqual(snapshot.parents[0].human_worth, "yes")
        self.assertEqual([row.row_key for row in links(self.db)], ["jordan-old"])
        share = ShareList(db=self.db, out_dir=self.root / "share",
                          evidence=ShareEvidence(self.db)).run()
        self.assertEqual((share.status, share.people, share.share_yes), ("completed", 1, 1))
        self.assertEqual(self.db.query("PRAGMA foreign_key_check"), [])

    def test_parser_normalizes_jsonish_channels_and_deduplicates_rows(self) -> None:
        self.write(
            [
                {
                    "id": "PERSON-1",
                    "full_name": "Jordan Bravo",
                    "primary_email": "Jordan@Example.test",
                    "source_channels": '["gmail_msgvault", "imessage"]',
                },
                {
                    "id": "person-1",
                    "all_emails": '["other@example.test"]',
                    "source_channels": "whatsapp",
                    "headline": "CEO @ Example Labs",
                },
            ]
        )

        rows = read_imported_people(self.csv)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].person_id, "person-1")
        self.assertEqual(rows[0].headline, "CEO @ Example Labs")
        self.assertEqual(rows[0].emails, ("jordan@example.test", "other@example.test"))
        self.assertEqual(
            rows[0].source_channels,
            ("gmail_msgvault", "imessage", "whatsapp"),
        )

    def test_projection_gets_one_stable_parent_and_preserves_newer_evidence(self) -> None:
        self.write(
            [
                {
                    "id": "person-1",
                    "full_name": "Jordan Bravo",
                    "primary_email": "first@example.test",
                    "source_channels": "gmail_msgvault",
                }
            ]
        )
        project_imported_people(self.db, read_imported_people(self.csv))
        first = canonical_snapshot(self.db)
        parent_id = first.people[0].parent_id

        self.write(
            [
                {
                    "id": "person-1",
                    "full_name": "",
                    "primary_phone": "+15550100",
                    "source_channels": "imessage",
                }
            ]
        )
        project_imported_people(self.db, read_imported_people(self.csv))
        current = canonical_snapshot(self.db)

        self.assertEqual(current.people[0].parent_id, parent_id)
        self.assertEqual(current.people[0].display_name, "Jordan Bravo")
        self.assertEqual(
            {(row.kind, row.normalized_value) for row in current.identifiers},
            {("email", "first@example.test"), ("phone", "+15550100")},
        )
        self.assertEqual(
            {row.source for row in current.sources},
            {"gmail_msgvault", "imessage"},
        )

    def test_superseded_identity_absorbs_into_existing_parent(self) -> None:
        self.write(
            [
                {
                    "id": "candidate:email:jordan@example.test",
                    "full_name": "Jordan Bravo",
                    "primary_email": "jordan@example.test",
                    "source_channels": "gmail_msgvault",
                }
            ]
        )
        project_imported_people(self.db, read_imported_people(self.csv))
        prior = canonical_snapshot(self.db)
        parent_id = prior.people[0].parent_id

        self.write(
            [
                {
                    "id": "linkedin-person-1",
                    "full_name": "Jordan Bravo",
                    "primary_email": "jordan@example.test",
                    "source_channels": "linkedin_csv,gmail_msgvault",
                    "superseded_person_ids": '["candidate:email:jordan@example.test"]',
                }
            ]
        )
        project_imported_people(self.db, read_imported_people(self.csv))
        current = canonical_snapshot(self.db)

        self.assertEqual(len(current.parents), 1)
        self.assertEqual(
            {row.person_id: row.parent_id for row in current.people},
            {
                "candidate:email:jordan@example.test": parent_id,
                "linkedin-person-1": parent_id,
            },
        )

    def test_superseded_identities_merge_existing_families_incrementally(self) -> None:
        for person_id, email in (
            ("candidate:email:jordan@example.test", "jordan@example.test"),
            ("candidate:phone:+15550100", "other@example.test"),
        ):
            self.write(
                [
                    {
                        "id": person_id,
                        "full_name": "Jordan Bravo",
                        "primary_email": email,
                        "source_channels": "gmail_msgvault",
                    }
                ]
            )
            project_imported_people(self.db, read_imported_people(self.csv))
        self.assertEqual(len(canonical_snapshot(self.db).parents), 2)

        self.write(
            [
                {
                    "id": "linkedin-person-1",
                    "full_name": "Jordan Bravo",
                    "primary_email": "jordan@example.test",
                    "source_channels": "linkedin_csv,gmail_msgvault",
                    "superseded_person_ids": ('["candidate:email:jordan@example.test","candidate:phone:+15550100"]'),
                }
            ]
        )
        project_imported_people(self.db, read_imported_people(self.csv))

        current = canonical_snapshot(self.db)
        self.assertEqual(len(current.parents), 1)
        self.assertEqual(len({row.parent_id for row in current.people}), 1)
        self.assertEqual(len(current.people), 3)

    def test_skill_merges_current_sources_before_projecting_parents(self) -> None:
        skill = (Path(__file__).parents[1] / "packs/ingestion/skills/deep-context/SKILL.md").read_text()
        workflow = skill[skill.index("### 1. Scope and owner"):skill.index("### 3. Dossiers")]
        self.assertLess(workflow.index("index_contacts_pipeline.py fan-in"),
                        workflow.index("bin/deep-context ensure-parents"))

    def test_ensure_parents_projects_people_before_collection(self) -> None:
        self.write(
            [
                {
                    "id": "person-1",
                    "full_name": "Jordan Bravo",
                    "primary_phone": "+15550100",
                    "source_channels": "imessage",
                }
            ]
        )
        result = EnsureParents(db=self.db, people_csv=self.csv).run()

        self.assertEqual(result.people_projected, 1)
        self.assertEqual(canonical_snapshot(self.db).people[0].person_id, "person-1")

    def test_readiness_probes_people_input_before_sqlite_exists(self) -> None:
        self.write(
            [
                {
                    "id": "candidate:email:jordan@example.test",
                    "full_name": "Jordan Bravo",
                    "primary_email": "jordan@example.test",
                    "source_channels": "gmail_msgvault",
                }
            ]
        )
        wacli = self.root / "wacli.db"
        wacli.touch()
        missing_db = self.root / "fresh" / "deep-context.sqlite"
        with (
            mock.patch(
                "packs.ingestion.primitives.deep_context.shared.check_readiness.context_sources.probe_chat_db",
                return_value=ChatDbProbe(False, False, 0, 0, None),
            ),
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-key", "TYPESAFE_API_KEY": "synthetic-key"}),
        ):
            result = CheckReadiness(
                db_path=missing_db,
                people_csv=self.csv,
                msgvault_db=self.root / "missing-msgvault.db",
                chat_db=self.root / "missing-chat.db",
                wacli_db=wacli,
            ).run()

        self.assertFalse(result.ready)
        self.assertEqual(result.next_command, "bin/deep-context ensure-parents")
        self.assertEqual(result.message_people, 1)
        self.assertEqual(result.candidates.total, 1)
        self.assertFalse(missing_db.exists())


if __name__ == "__main__":
    unittest.main()
