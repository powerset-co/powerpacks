"""Batched research selection preserves the paid provider request contract."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow, FactRow, OwnerContextRow, ParentRow, PersonRow,
    PersonIdentifierRow, PersonIdentifiersProjection, ResearchRow,
)
from packs.ingestion.primitives.deep_context.db import context_queries, queries
from packs.ingestion.primitives.deep_context.db.identity_views import enrichment_queue, lookups_pending
from packs.ingestion.primitives.deep_context.db.workflow_views import ReviewSelection, workflow_state
from packs.ingestion.primitives.deep_context.enrich.research_reconcile import selection, coordinator
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.manifests.receipt_status import ReceiptStatus
from packs.ingestion.primitives.deep_context.db.view_models import EnrichmentQueueRow
from packs.ingestion.primitives.deep_context.enrich.parallel_research.config import DEFAULT_PROCESSOR
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import (
    build_input, input_fingerprint, request_plan_fingerprint,
)
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.selection import (
    build_queue, build_queue_row, select_research,
)
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import (
    DossierEvidence, owner_background,
)
from packs.ingestion.primitives.pipeline.contract import PeopleRow


class ResearchSelectionBatchTests(unittest.TestCase):
    def test_corrected_source_name_holds_incompatible_retained_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / "context.sqlite")
            db.project_rows((
                ParentRow("parent-a", "jordan-north", display_name="Jordan North"),
                PersonRow("person-a", "parent-a"),
                ArtifactRow("facts:a", "facts", "parent-a", "/facts.json", "sha", "projected"),
                FactRow("parent-a", "parent-a", "facts:a", machine_worth="yes",
                        facts_json='{"canonical_name":"Jordan North","employers":[{"name":"North Labs"}]}'),
            ))
            db.replace_imported_people((PeopleRow(id="person-a", full_name="Casey Bravo"),))
            facts = queries.facts(db)
            self.assertEqual(enrichment_queue(db), [])
            self.assertEqual(lookups_pending(db), ())
            self.assertEqual(workflow_state(db).progress.lookups_pending, 0)
            plan = select_research(db, processor=DEFAULT_PROCESSOR)
            self.assertEqual((plan.eligible, plan.pending, plan.estimated_usd), ((), (), 0))
            self.assertEqual(queries.facts(db), facts)
            self.assertEqual(queries.imported_people(db)[0].full_name, "Casey Bravo")

    def test_compatible_source_names_ignore_owner_and_ghost_members(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / "context.sqlite")
            db.project_rows((
                ParentRow("parent-a", "casey-bravo"),
                PersonRow("person-a", "parent-a"), PersonRow("person-b", "parent-a"),
                PersonRow("owner", "parent-a", is_owner=True),
                PersonRow("ghost", "parent-a", is_ghost=True),
                ArtifactRow("facts:a", "facts", "parent-a", "/facts.json", "sha", "projected"),
                FactRow("parent-a", "parent-a", "facts:a", machine_worth="yes", facts_json="{}"),
            ))
            db.replace_imported_people(tuple(PeopleRow(id=person, full_name=name) for person, name in (
                ("person-a", "Casey Bravo"), ("person-b", "C. Bravo"),
                ("owner", "Jordan North"), ("ghost", ""),
            )))
            self.assertEqual([row.parent_id for row in enrichment_queue(db)], ["parent-a"])
            self.assertEqual(lookups_pending(db), ("parent-a",))
            self.assertEqual(workflow_state(db).progress.lookups_pending, 1)
            plan = select_research(db, processor=DEFAULT_PROCESSOR)
            self.assertEqual((len(plan.eligible), len(plan.pending)), (1, 1))

    def test_missing_or_conflicting_source_names_hold_automatic_research(self):
        for names in (("",), ("Casey Bravo", "Jordan North"), (None,)):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                raw = root / "raw.json"
                raw.write_text('{"messages":["retained source evidence"]}')
                original = raw.read_bytes()
                db = Db(root / "context.sqlite")
                db.project_rows((
                    ParentRow("parent-a", "casey-bravo", display_name="Model Selected Casey Bravo"),
                    ArtifactRow("facts:a", "facts", "parent-a", "/facts.json", "sha", "projected"),
                    FactRow("parent-a", "parent-a", "facts:a", machine_worth="yes",
                            facts_json='{"canonical_name":"Model Selected Casey Bravo"}'),
                    *(PersonRow(f"person-{index}", "parent-a") for index in range(len(names))),
                ))
                db.replace_imported_people(tuple(
                    PeopleRow(id=f"person-{index}", full_name=name)
                    for index, name in enumerate(names) if name is not None
                ))
                facts = queries.facts(db)
                self.assertEqual(enrichment_queue(db), [])
                self.assertEqual(lookups_pending(db), ())
                self.assertEqual(workflow_state(db).progress.lookups_pending, 0)
                plan = select_research(db, processor=DEFAULT_PROCESSOR)
                self.assertEqual((plan.eligible, plan.pending, plan.estimated_usd), ((), (), 0))
                self.assertEqual(queries.facts(db), facts)
                self.assertEqual(raw.read_bytes(), original)
                self.assertEqual(len(queries.people(db)), len(names))

    def test_ghost_and_non_source_identifiers_never_enter_source_research_input(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / "context.sqlite")
            db.project_rows((
                ParentRow("parent-a", "casey-bravo", display_name="Casey Bravo"),
                PersonRow("person-a", "parent-a"),
                PersonRow("ghost", "parent-a", is_ghost=True),
                PersonRow("model", "parent-a"),
                PersonRow("owner", "parent-a", is_owner=True),
                ArtifactRow("facts:person-a", "facts", "parent-a", "/facts.json", "sha", "projected"),
                FactRow("person-a", "parent-a", "facts:person-a", person_id="person-a", machine_worth="yes",
                        facts_json='{"relationship_to_owner":"colleague"}'),
            ))
            db.replace_imported_people(tuple(
                PeopleRow(id=person, full_name="Casey Bravo") for person in ("person-a", "ghost", "owner")
            ))
            db.project_rows(tuple(
                PersonIdentifiersProjection(person, (
                    PersonIdentifierRow(person, "email", email),
                    PersonIdentifierRow(person, "phone", phone),
                ))
                for person, email, phone in (
                    ("person-a", "casey@example.com", "15550100999"),
                    ("ghost", "aaa-ghost@example.com", "15550100001"),
                    ("model", "aab-model@example.com", "15550100002"),
                    ("owner", "aac-owner@example.com", "15550100003"),
                )
            ))
            self.assertEqual(select_research(db, processor=DEFAULT_PROCESSOR).pending, ())
            explicit = EnrichmentQueueRow(
                "parent-a", "casey-bravo", "Casey Bravo", ("person-a", "model"),
                "research:parent-a", False, "", "", "", (), (), False,
            )
            row = build_queue([explicit], db)[0]
            self.assertEqual(row.primary_email, "casey@example.com")
            self.assertEqual(row.phone_e164, "15550100999")
            dossier = build_input(row)["dossier"]
            self.assertIn('Source contact emails: ["casey@example.com"]', dossier)
            self.assertIn('Source contact phones: ["15550100999"]', dossier)
            for value in ("aaa-ghost", "aab-model", "aac-owner", "15550100001", "15550100002", "15550100003"):
                self.assertNotIn(value, dossier)
            db.replace_imported_people(())
            self.assertEqual(select_research(db, processor=DEFAULT_PROCESSOR).pending, ())
            unknown = build_queue([explicit], db)[0]
            self.assertEqual((unknown.display_name, unknown.primary_email, unknown.phone_e164), ("", "", ""))
            self.assertIn("Source contact names missing or conflicting", build_input(unknown)["dossier"])
            self.assertIn("Source contact emails: []", build_input(unknown)["dossier"])
            self.assertIn("Source contact phones: []", build_input(unknown)["dossier"])

    def test_source_name_change_under_same_parent_invalidates_saved_research(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "context.sqlite")
            db.project_rows((
                ParentRow("parent-a", "casey-bravo", display_name="Casey Bravo"),
                PersonRow("person-a", "parent-a", display_name="Casey Bravo"),
                PersonIdentifiersProjection("person-a", (
                    PersonIdentifierRow("person-a", "email", "casey@example.com"),
                )),
                ArtifactRow("facts:person-a", "facts", "parent-a", "/facts.json", "sha", "projected"),
                FactRow("person-a", "parent-a", "facts:person-a", person_id="person-a", machine_worth="yes",
                        facts_json='{"relationship_to_owner":"colleague"}'),
            ))
            db.replace_imported_people((PeopleRow(id="person-a", full_name="Casey Bravo",
                                                primary_email="casey@example.com"),))
            db.decide_worth("parent-a", "yes")
            row = select_research(db, processor=DEFAULT_PROCESSOR).pending[0]
            document = root / "paid-original.json"
            document.write_text('{"type":"json","content":{"linkedin_url":null}}')
            before = document.read_bytes()
            db.project_rows((
                ArtifactRow(f"research:{row.handle}", "research", "parent-a", str(document), "paid", "projected",
                            input_fingerprint=input_fingerprint(row), payload_json=document.read_text()),
                ResearchRow(row.handle, "parent-a", "no_match", artifact_key=f"research:{row.handle}",
                            result_json=document.read_text()),
            ))
            exact = select_research(db, processor=DEFAULT_PROCESSOR)
            self.assertEqual((len(exact.pending), exact.reused_completed), (0, 1))
            for name in ("Jordan North", ""):
                with self.subTest(name=name):
                    db.replace_imported_people((PeopleRow(id="person-a", full_name=name,
                                                        primary_email="casey@example.com"),))
                    changed = select_research(db, processor=DEFAULT_PROCESSOR)
                    if not name:
                        self.assertEqual((changed.eligible, changed.pending), ((), ()))
                        self.assertEqual(document.read_bytes(), before)
                        continue
                    self.assertEqual((len(changed.pending), changed.reused_completed), (1, 0))
                    request = changed.pending[0]
                    dossier = build_input(request)["dossier"]
                    self.assertIn("Source contact names: " + json.dumps((name,)), dossier)
                    self.assertNotIn("Name: Casey Bravo", dossier)
                    self.assertEqual(request.display_name, name)
                    self.assertEqual(request.handle, row.handle)
                    self.assertEqual(request.primary_email, row.primary_email)
                    self.assertNotEqual(input_fingerprint(request), input_fingerprint(row))
                    self.assertEqual(document.read_bytes(), before)

    def test_conflicting_source_names_remain_visible_without_a_selected_name(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / "context.sqlite")
            db.project_rows((
                ParentRow("parent-a", "casey-bravo", display_name="Casey Bravo"),
                PersonRow("person-a", "parent-a"), PersonRow("person-b", "parent-a"),
            ))
            db.replace_imported_people((
                PeopleRow(id="person-a", full_name="Casey Bravo"),
                PeopleRow(id="person-b", full_name="Jordan North"),
            ))
            eligible = EnrichmentQueueRow(
                "parent-a", "casey-bravo", "Casey Bravo", ("person-a", "person-b"),
                "research:parent-a", False, "", "", "", (), (), False,
            )
            row = build_queue([eligible], db)[0]
            self.assertEqual(row.display_name, "")
            dossier = build_input(row)["dossier"]
            self.assertIn('Source contact names: ["Casey Bravo", "Jordan North"]', dossier)
            self.assertNotIn("Name:", dossier)

    def test_non_primary_source_endpoint_change_invalidates_saved_research(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / "context.sqlite")
            db.project_rows((
                ParentRow("parent-a", "casey-bravo", display_name="Casey Bravo"),
                PersonRow("person-a", "parent-a"),
                PersonRow("person-b", "parent-a"),
                PersonIdentifiersProjection("person-a", (
                    PersonIdentifierRow("person-a", "email", "a-casey@example.com"),
                )),
                PersonIdentifiersProjection("person-b", (
                    PersonIdentifierRow("person-b", "email", "b-casey@example.com"),
                )),
                ArtifactRow("facts:person-a", "facts", "parent-a", "/facts.json", "sha", "projected"),
                FactRow("person-a", "parent-a", "facts:person-a", person_id="person-a", machine_worth="yes",
                        facts_json='{"relationship_to_owner":"colleague"}'),
            ))
            db.replace_imported_people(tuple(
                PeopleRow(id=person, full_name="Casey Bravo") for person in ("person-a", "person-b")
            ))
            row = select_research(db, processor=DEFAULT_PROCESSOR).pending[0]
            self.assertIn('Source contact emails: ["a-casey@example.com", "b-casey@example.com"]',
                          build_input(row)["dossier"])
            db.project_rows((ArtifactRow(f"research:{row.handle}", "research", "parent-a", "/paid.json", "paid",
                                       "projected", input_fingerprint=input_fingerprint(row)),))
            db.project_rows((PersonIdentifiersProjection("person-b", (
                PersonIdentifierRow("person-b", "email", "c-casey@example.com"),
            )),))
            changed = select_research(db, processor=DEFAULT_PROCESSOR)
            self.assertEqual((len(changed.pending), changed.reused_completed), (1, 0))
            self.assertEqual(changed.pending[0].primary_email, row.primary_email)
            dossier = build_input(changed.pending[0])["dossier"]
            self.assertIn("c-casey@example.com", dossier)
            self.assertNotIn("b-casey@example.com", dossier)

    def test_saved_no_match_rechecks_current_input_and_keeps_cost_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "context.sqlite")
            db.project_rows((
                ParentRow("parent-a", "casey-bravo", display_name="Casey Bravo"),
                PersonRow("person-a", "parent-a", display_name="Casey Bravo"),
                PersonIdentifiersProjection("person-a", (PersonIdentifierRow("person-a", "email", "casey@example.com"),)),
                ArtifactRow("facts:person-a", "facts", "parent-a", "/facts.json", "sha", "projected"),
                FactRow("person-a", "parent-a", "facts:person-a", person_id="person-a", machine_worth="yes",
                        facts_json='{"relationship_to_owner":"colleague"}'),
            ))
            db.replace_imported_people((PeopleRow(id="person-a", full_name="Casey Bravo",
                                                primary_email="casey@example.com"),))
            db.decide_worth("parent-a", "yes")
            initial = select_research(db, processor=DEFAULT_PROCESSOR)
            self.assertEqual(len(initial.pending), 1)
            row = initial.pending[0]
            document = root / "paid-original.json"
            document.write_text('{"type":"json","content":{"linkedin_url":null}}')
            original = document.read_bytes()
            db.project_rows((
                ArtifactRow(f"research:{row.handle}", "research", "parent-a", str(document), "paid-content", "projected",
                            input_fingerprint=input_fingerprint(row), payload_json=document.read_text()),
                ResearchRow(row.handle, "parent-a", "no_match", artifact_key=f"research:{row.handle}",
                            result_json=document.read_text()),
            ))
            exact = select_research(db, processor=DEFAULT_PROCESSOR)
            self.assertEqual((len(exact.eligible), len(exact.pending), exact.reused_completed, exact.estimated_usd),
                             (1, 0, 1, 0))
            db.project_rows((PersonIdentifiersProjection("person-a", (
                PersonIdentifierRow("person-a", "email", "new-casey@example.com"),
            )),))
            changed = select_research(db, processor=DEFAULT_PROCESSOR)
            self.assertEqual((len(changed.pending), changed.reused_completed), (1, 0))
            self.assertEqual(changed.pending[0].handle, row.handle)
            self.assertEqual(changed.pending[0].primary_email, "new-casey@example.com")
            self.assertGreater(changed.estimated_usd, 0)
            with patch.object(coordinator.driver, "run_research") as provider:
                outcome = coordinator.ReconcileDeepResearch(db, out_dir=root / "new-research").run()
            self.assertEqual(outcome.status, ReceiptStatus.NEEDS_APPROVAL)
            provider.assert_not_called()
            self.assertEqual(document.read_bytes(), original)
            self.assertEqual(queries.artifacts(db, kind="research")[0].payload_json, document.read_text())

    def test_multiple_parent_inputs_stay_identical_with_one_evidence_read(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / "context.sqlite")
            db.project_rows((OwnerContextRow(
                "owner", json.dumps({"name": "Casey Delta", "notes": "Robotics colleague"}),
                "/owner.json", "sha",
            ),))
            subset = []
            for index in range(4):
                parent = f"parent-{index}"
                person = f"person-{index}"
                other = f"other-{index}"
                db.project_rows((
                    ParentRow(parent, f"jordan-bravo-{index}", display_name="Jordan Bravo"),
                    PersonRow(person, parent), PersonRow(other, parent),
                ))
                subjects = [(person, person), (other, other)]
                if index % 2:
                    subjects.append((parent, None))
                for subject, person_id in subjects:
                    facts = {
                        "aliases": [subject], "relationship_to_owner": "colleague",
                        "employers": [{"name": f"Example Robotics {index}"}],
                        "school": "Example University", "location": "Example City",
                        "topics": [subject],
                    }
                    db.project_rows((
                        ArtifactRow(
                            f"facts:{subject}", "facts", parent,
                            f"/{subject}.json", "sha", "projected",
                        ),
                        FactRow(
                            subject, parent, f"facts:{subject}",
                            person_id=person_id, facts_json=json.dumps(facts),
                        ),
                    ))
                subset.append(EnrichmentQueueRow(
                    parent, f"jordan-bravo-{index}", "Jordan Bravo", (person,),
                    f"candidate-{index}", True, "https://linkedin.com/in/example", "rejected",
                    "Different employer", ("invalid", f"jordan{index}@example.com"),
                    (f"+15550100{index}",), False,
                ))
            owner = owner_background(db)
            expected = [
                build_queue_row(
                    DossierEvidence.from_db(db, row.person_ids), row,
                    source_names=queries.source_names(db, row.parent_id),
                    owner_context=owner, guidance="  robotics  ",
                )
                for row in subset
            ]
            with patch.object(db, "query", wraps=db.query) as query:
                actual = build_queue(subset, db, guidance="  robotics  ")
            self.assertEqual(actual, expected)
            self.assertEqual(
                [build_input(row) for row in actual],
                [build_input(row) for row in expected],
            )
            self.assertEqual(
                [input_fingerprint(row) for row in actual],
                [input_fingerprint(row) for row in expected],
            )
            self.assertEqual(
                request_plan_fingerprint(actual), request_plan_fingerprint(expected),
            )
            self.assertLessEqual(query.call_count, 9)
            with patch.object(selection, "RESEARCH_BATCH", 2), patch.object(
                context_queries, "dossier_evidence_rows", wraps=context_queries.dossier_evidence_rows,
            ) as evidence_read:
                self.assertEqual(build_queue(subset, db, guidance="  robotics  "), expected)
            self.assertEqual(evidence_read.call_count, 2)
            self.assertTrue(all(len(call.args[1]) <= 2 for call in evidence_read.call_args_list))

            db.project_rows(tuple(
                ArtifactRow(
                    f"research:{row.handle}", "research", row.parent_id,
                    f"/{row.handle}.json", "sha", "projected",
                    input_fingerprint=input_fingerprint(
                        build_queue([subset[index]], db)[0],
                    ),
                )
                for index, row in enumerate(actual)
            ))
            review = ReviewSelection("test", 4, 4, 0, 0, "revision")
            for eligible in (subset[:1], []):
                with patch.object(selection, "enrichment_queue", return_value=eligible), patch.object(
                    queries, "artifacts", wraps=queries.artifacts,
                ) as artifact_read:
                    result = select_research(db, processor=DEFAULT_PROCESSOR, fingerprint=review)
                self.assertEqual(result.eligible, tuple(eligible))
                self.assertEqual(artifact_read.call_count, int(bool(eligible)))
                self.assertTrue(all(
                    call.kwargs["parent_ids"] == tuple(row.parent_id for row in eligible)
                    for call in artifact_read.call_args_list
                ))
                self.assertEqual(result.reused_completed, len(eligible))
                self.assertEqual(result.pending, ())
            with patch.object(db, "query", wraps=db.query) as query:
                self.assertEqual(queries.artifacts(db, kind="research", parent_ids=()), ())
            self.assertIn("parent_id IN", query.call_args.args[0])
            self.assertEqual(query.call_args.args[1][-1], "[]")


if __name__ == "__main__":
    unittest.main()
