from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deep_context_sqlite_test_helpers import query
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    CandidatePeopleProjection,
    CandidatePersonRow,
    FactRow,
    LinkRow,
    ParentRow,
    PersonRow,
    SyntheticProfileRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.heal_review import HealCandidate, HealReview
from packs.ingestion.primitives.deep_context.identity_reconcile.judgment_policy import (
    NO_PROFILE_REASON,
)
from packs.ingestion.primitives.enrich.rapidapi_client import (
    PROFILE_CONTENT,
    PROFILE_EMPTY,
    PROFILE_ERROR,
)


class HealReviewSqliteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.manifest = self.root / "review" / "manifest.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_candidate(self, slug: str, *, key: str | None = None, **updates: object) -> str:
        candidate_key = key or slug
        parent_id = f"parent-{slug}"
        person_id = f"person-{slug}"
        artifact_key = f"facts:{person_id}"
        base = {
            "linkedin_url": f"https://www.linkedin.com/in/{slug}",
            "machine_judgment": "needs_review",
            "machine_confidence": 0.0,
            "machine_reason": NO_PROFILE_REASON,
            "paid_profile": 1,
        }
        base.update(updates)
        self.db.project_rows(
            (
                ParentRow(parent_id, slug, f"Jordan {slug.title()}", slug),
                PersonRow(person_id, parent_id, display_name=f"Jordan {slug.title()}"),
                LinkRow(candidate_key, parent_id, slug, "pub", **base),
                CandidatePeopleProjection(
                    candidate_key, (CandidatePersonRow(candidate_key, person_id, parent_id),)
                ),
                ArtifactRow(
                    artifact_key, "facts", parent_id, f"/facts/{person_id}.jsonl",
                    f"sha-{slug}", "projected", person_id=person_id,
                ),
                FactRow(
                    person_id, parent_id, artifact_key, person_id=person_id,
                    machine_worth="yes",
                    facts_json=json.dumps({
                        "canonical_name": f"Jordan {slug.title()}",
                        "title": "Engineer",
                        "employers": [{"name": "Acme", "status": "current"}],
                    }),
                ),
            )
        )
        return candidate_key

    def heal(self, **kwargs: object) -> HealReview:
        return HealReview(
            db=self.db,
            profile_cache_dir=self.root / "profiles",
            review_manifest=self.manifest,
            **kwargs,
        )

    def test_selects_only_sql_judge_skips_and_reports_cap_and_retarget(self) -> None:
        self.add_candidate("alpha")
        self.add_candidate("decided", machine_approved="auto")
        self.add_candidate("judged", machine_judgment="confirmed", machine_confidence=0.9)
        self.add_candidate(
            "retarget",
            machine_action="retarget",
            machine_proposed_public_identifier="retarget-new",
        )

        selected, skipped, uncapped = self.heal(cap=1).select_candidates()

        self.assertEqual([row.candidate_key for row in selected], ["alpha"])
        self.assertEqual((skipped, uncapped), (1, 1))

    @patch("packs.ingestion.primitives.deep_context.identity_evidence.judge_batch")
    @patch(
        "packs.ingestion.primitives.deep_context.profile_projection."
        "hydrate_profiles"
    )
    def test_run_hydrates_from_sql_judges_content_and_preserves_payload(
        self, hydrate, judge,
    ) -> None:
        self.add_candidate("content")
        self.add_candidate("empty")
        self.add_candidate("cached-empty")
        self.add_candidate("error")
        states = {
            "content": {"state": PROFILE_CONTENT, "fetched": True, "from_cache": False},
            "empty": {"state": PROFILE_EMPTY, "fetched": True, "from_cache": False},
            "cached-empty": {"state": PROFILE_EMPTY, "fetched": False, "from_cache": True},
            "error": {"state": PROFILE_ERROR, "fetched": True, "from_cache": False},
        }
        def hydrate_results(targets, _cache_dir, *, on_result, **_kwargs):
            for target in targets:
                on_result(target, dict(states[target["public_identifier"]]))
            return (
                {"wanted": len(targets), "ok": 1, "failed": 3, "skipped_no_key": 0},
                {},
            )

        hydrate.side_effect = hydrate_results
        judge.return_value = [{"verdict": {
            "verdict": "confirmed", "confidence": 0.95, "reason": "facts agree",
        }}]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            summary = self.heal().run()

        self.assertEqual(summary["profiles"], {
            "content": 1, "empty_fetched": 1, "empty_unfetched": 1,
            "error": 1, "fetched": 3, "from_cache": 1,
        })
        self.assertEqual(summary["rejudge"]["verified"], 1)
        self.assertEqual(summary["terminated"]["detached"], 1)
        task = judge.call_args.args[0][0]
        self.assertEqual(task["dossier"]["title"], "Engineer")
        self.assertEqual(task["dossier"]["employers"], ["Acme"])
        rows = {row["row_key"]: row for row in query(self.db, "SELECT * FROM links")}
        self.assertEqual((rows["content"]["machine_action"], rows["content"]["machine_approved"]),
                         ("verify", "auto"))
        self.assertEqual((rows["empty"]["machine_action"], rows["empty"]["authoritative_detach"]),
                         ("detach", 1))
        self.assertIsNone(rows["cached-empty"]["machine_approved"])
        self.assertIsNone(rows["error"]["machine_approved"])
        self.assertEqual(set(summary), {
            "primitive", "status", "owner_phones_backfilled", "legacy_scrub",
            "queue_pending_before", "queue_pending_after", "candidates",
            "candidates_uncapped", "capped", "cap", "skipped_pending_retarget",
            "profiles", "rejudge", "terminated", "elapsed_ms",
        })
        self.assertIn("heal", json.loads(self.manifest.read_text()))

    def test_dead_link_stands_existing_synthetic_without_files(self) -> None:
        key = self.add_candidate("dead")
        self.db.project_rows(
            (
                LinkRow("synthetic:dead", "parent-dead", "synthetic:dead", "synthetic"),
                SyntheticProfileRow("synthetic:dead", "synthetic:dead", "{}"),
            )
        )
        candidate = HealCandidate(
            "parent-dead", "dead", "Jordan Dead", key, "dead",
            "https://www.linkedin.com/in/dead",
        )

        summary = self.heal().terminate([candidate])

        rows = {row["row_key"]: row for row in query(self.db, "SELECT * FROM links")}
        self.assertEqual(summary["detached"], 1)
        self.assertEqual(summary["stood_synthetic"], 1)
        self.assertEqual(summary["minted_synthetic"], 0)
        self.assertIsNone(summary["assemble"])
        self.assertEqual(rows["synthetic:dead"]["machine_approved"], "auto")

    def test_human_decision_racing_termination_is_preserved(self) -> None:
        key = self.add_candidate("human")
        candidate = self.heal().select_candidates()[0][0]
        self.db.decide_identity(key, "verify", approved="yes")

        summary = self.heal().terminate([candidate])

        row = query(self.db, "SELECT * FROM links WHERE row_key=?", (key,))[0]
        self.assertEqual(summary["skipped_human_decided"], 1)
        self.assertEqual(summary["detached"], 0)
        self.assertEqual((row["decision_action"], row["decision_approved"]), ("verify", "yes"))


if __name__ == "__main__":
    unittest.main()
