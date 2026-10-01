"""Batched research selection preserves the paid provider request contract."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow, FactRow, OwnerContextRow, ParentRow, PersonRow,
)
from packs.ingestion.primitives.deep_context.db import context_queries, queries
from packs.ingestion.primitives.deep_context.db.workflow_views import ReviewSelection
from packs.ingestion.primitives.deep_context.enrich.research_reconcile import selection
from packs.ingestion.primitives.deep_context.db.store import Db
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


class ResearchSelectionBatchTests(unittest.TestCase):
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
