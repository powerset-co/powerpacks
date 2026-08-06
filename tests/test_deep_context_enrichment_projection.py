"""File-first Parallel receipts projected explicitly into Deep Context SQLite."""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context import deep_research_contacts as research
from packs.ingestion.primitives.deep_context import reconcile_deep_research as reconcile
from packs.ingestion.primitives.deep_context.db.models import ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db
from deep_context_sqlite_test_helpers import query


class EnrichmentProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / "deep-research"
        self.out.mkdir()
        self.manifest = self.out / "manifest.json"
        self.queue = self.out / "research_queue.csv"
        self.db = Db(self.root / "deep-context.sqlite")
        self.db.project_rows(
            (
                ParentRow("parent-1", "parent-worth:parent-1", "Jordan Bravo"),
                PersonRow("person-a", "parent-1", display_name="Jordan Bravo"),
            )
        )
        self.queue_row = {
            "handle": "jordan-bravo",
            "source_parent_slug": "jordan-bravo",
            "source_person_ids": json.dumps(["person-a"]),
            "source_candidate_public_identifier": "candidate:email:jordan@example.com",
            "display_name": "Jordan Bravo",
            "bio": "Known collaborator",
            "known_info": "Synthetic fixture",
            "primary_email": "jordan@example.com",
            "phone_e164": "",
            "area_code": "",
            "source_channel": "email",
            "retarget_hint": "Find the correct profile",
        }
        self._write_queue([self.queue_row])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_queue(self, rows: list[dict[str, str]]) -> None:
        with self.queue.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=reconcile.QUEUE_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def _write_result(self, suffix: str = "one") -> tuple[Path, Path]:
        person = self.out / "jordan-bravo"
        person.mkdir(exist_ok=True)
        raw = person / "00_parallel_raw.json"
        result = person / "01_research_parallel.json"
        raw.write_text(json.dumps({"provider_result": suffix}), encoding="utf-8")
        result.write_text(
            json.dumps(
                {
                    "person": {"full_name": "Jordan Bravo", "confidence": 0.91},
                    "social": {"linkedin_url": f"https://www.linkedin.com/in/jordan-{suffix}"},
                    "metadata": {"research_notes": "fixture"},
                }
            ),
            encoding="utf-8",
        )
        return raw, result

    def _params(self) -> research.ResearchRunParams:
        return research.ResearchRunParams(
            input_csv=self.queue, output_dir=self.out, manifest=str(self.manifest), db=self.db
        )

    def test_inventory_names_exact_paths_and_hashes(self) -> None:
        raw, result = self._write_result()
        (entry,) = research.research_artifact_inventory(self._params())
        self.assertEqual(entry["path"], "jordan-bravo/01_research_parallel.json")
        self.assertEqual(entry["raw_path"], "jordan-bravo/00_parallel_raw.json")
        self.assertEqual(entry["sha256"], hashlib.sha256(result.read_bytes()).hexdigest())
        self.assertEqual(entry["raw_sha256"], hashlib.sha256(raw.read_bytes()).hexdigest())
        self.assertEqual((entry["parent_id"], entry["person_ids"]), ("parent-1", ["person-a"]))

    def test_running_terminal_and_changed_projection_preserve_human_decision(self) -> None:
        self._write_result()
        params = self._params()
        research._report_progress(
            params,
            "running",
            {"total": 1, "completed": 0, "pending": 1, "failed": 0},
            selection={"fingerprint": "selection-1"},
        )
        self.assertEqual(query(self.db, "SELECT status FROM stage_state")[0][0], "running")
        self.db.decide_identity("candidate:email:jordan@example.com", "verify")

        research._report_progress(
            params,
            "research_complete",
            {"total": 1, "completed": 1, "pending": 0, "failed": 0},
            selection={"fingerprint": "selection-1"},
        )
        first_artifacts = query(self.db, "SELECT count(*) FROM artifacts")[0][0]
        self.assertEqual(first_artifacts, 2)
        self.assertEqual(query(self.db, "SELECT status FROM stage_state")[0][0], "complete")

        self._write_result("two")
        research._report_progress(
            params,
            "research_complete",
            {"total": 1, "completed": 1, "pending": 0, "failed": 0},
            selection={"fingerprint": "selection-1"},
        )
        research._report_progress(
            params,
            "research_complete",
            {"total": 1, "completed": 1, "pending": 0, "failed": 0},
            selection={"fingerprint": "selection-1"},
        )
        link = query(
            self.db,
            "SELECT machine_proposed_public_identifier, decision_action, decision_approved "
            "FROM links WHERE row_key='candidate:email:jordan@example.com'",
        )[0]
        self.assertEqual(tuple(link), ("jordan-two", "verify", "yes"))
        self.assertEqual(query(self.db, "SELECT count(*) FROM artifacts")[0][0], first_artifacts)

    def test_failure_transition_projects_error_without_erasing_artifacts(self) -> None:
        self._write_result()
        research._report_progress(
            self._params(),
            "failed",
            {"total": 1, "completed": 0, "pending": 0, "failed": 1},
            selection={"fingerprint": "selection-1"},
            error="provider failed",
        )
        stage = query(self.db, "SELECT status, error FROM stage_state")[0]
        job = query(self.db, "SELECT status, error FROM jobs")[0]
        self.assertEqual(tuple(stage), ("failed", "provider failed"))
        self.assertEqual(tuple(job), ("failed", "provider failed"))
        self.assertEqual(query(self.db, "SELECT count(*) FROM artifacts")[0][0], 2)

    def test_zero_work_terminal_projects_empty_inventory(self) -> None:
        self._write_queue([])
        research._report_progress(
            self._params(),
            "research_complete",
            {"total": 0, "completed": 0, "pending": 0, "failed": 0},
            selection={"fingerprint": "selection-empty"},
        )
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(payload["artifacts"], [])
        self.assertEqual(query(self.db, "SELECT status FROM stage_state")[0][0], "complete")

    def test_reconcile_needs_approval_writes_then_projects_without_spend(self) -> None:
        verdicts = self.root / "verdicts.jsonl"
        verdicts.write_text("", encoding="utf-8")
        review = self.root / "review.csv"
        people = self.root / "people.csv"
        facts = self.root / "facts"
        raw = self.root / "raw"
        facts.mkdir()
        raw.mkdir()
        subset = [
            {
                "parent_slug": "jordan-bravo",
                "person_ids": ["person-a"],
                "candidate_key": "candidate:email:jordan@example.com",
                "name": "Jordan Bravo",
                "linkedin": {},
                "verdict": {},
                "match_emails": [],
                "match_phones": [],
            }
        ]
        with (
            mock.patch.object(
                reconcile.views,
                "workflow_state",
                return_value={
                    "selection": {
                        "sha256": "selection-1",
                        "review_revision": "revision-1",
                    }
                },
            ),
            mock.patch.object(reconcile.views, "linkedin_review", return_value=subset),
            mock.patch.object(reconcile, "build_queue", return_value=[self.queue_row]),
            mock.patch.object(reconcile, "run_research") as paid,
        ):
            node = reconcile.ReconcileDeepResearch(
                verdicts_jsonl=verdicts,
                overrides_csv=review,
                people_csv=people,
                facts_dir=facts,
                raw_dir=raw,
                manifest=self.manifest,
                out_dir=self.out,
                queue_csv=self.queue,
                budget=1.0,
                approve=False,
                db=self.db,
            )
            node.run()
        paid.assert_not_called()
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "needs_approval")
        self.assertEqual(payload["artifacts"], [])
        self.assertEqual(query(self.db, "SELECT status FROM stage_state")[0][0], "needs_approval")


if __name__ == "__main__":
    unittest.main()
