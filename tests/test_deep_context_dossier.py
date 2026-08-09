"""Direct contracts for dossier fact reduction, rendering, and scoped composition."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.merge_candidates.build_parents import BuildParents
from packs.ingestion.primitives.deep_context.synthesis.compose_dossier import ComposeDossier
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    FactRow,
    OwnerContextRow,
    ParentRow,
    PersonRow,
    ProjectionStatus,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.synthesis.facts import (
    MAX_NOTABLE_EVENTS,
    headline,
    collapse_fact_records,
    merge_disjoint_fact_records,
)
from packs.ingestion.primitives.deep_context.synthesis.models import (
    FactRecord,
    SynthesizedFacts,
)
from packs.ingestion.primitives.deep_context.synthesis.rendering import render_dossier
from packs.ingestion.primitives.deep_context.synthesis.validate_dossiers import ValidateDossiers


class DossierFactsTest(unittest.TestCase):
    def test_merge_policy_and_headline_live_in_concrete_module(self) -> None:
        merged = merge_disjoint_fact_records(filter(None, (
            FactRecord.from_payload({"facts": {
                "canonical_name": "Jordan Bravo",
                "employers": [{"name": "Example Labs", "role": "Builder", "status": "past"}],
                "topics": ["Systems"],
                "network_worth": {"decision": "maybe", "reason": "early evidence"},
                "confidence": 0.6,
            }}),
            FactRecord.from_payload({"facts": {
                "canonical_name": "Jordan Bravo",
                "employers": [{"name": "Example Labs", "role": "", "status": "current"}],
                "title": "Engineer",
                "topics": ["systems", "Testing"],
                "network_worth": {"decision": "yes", "reason": "known collaborator"},
                "confidence": 0.9,
            }}),
        )))
        self.assertIsNotNone(merged)
        self.assertEqual(
            [row.to_payload() for row in merged.employers],
            [{"name": "Example Labs", "role": "Builder", "status": "current"}],
        )
        self.assertEqual(merged.topics, ("Systems", "Testing"))
        self.assertEqual(merged.network_worth.to_payload(), {
            "decision": "yes", "reason": "known collaborator",
        })
        self.assertEqual(headline(merged), "Engineer at Example Labs")

    def test_rendered_dossier_bytes_stay_pinned(self) -> None:
        meta = CollectionBundle.from_payload({
            "person_id": "person-a", "full_name": "Jordan Bravo",
            "emails": ["jordan@example.com"], "phones": [],
            "source_channels": ["gmail_msgvault"], "messages": [],
        })
        merged = SynthesizedFacts.from_payload({
            "canonical_name": "Jordan Bravo", "confidence": 0.9,
            "title": "Engineer", "employers": [], "topics": [],
            "identifiers": [], "network_worth": {},
        })
        self.assertIsNotNone(meta)
        self.assertIsNotNone(merged)
        with mock.patch(
            "packs.ingestion.primitives.deep_context.synthesis.rendering.now_iso",
            return_value="2026-01-02T03:04:05Z",
        ):
            rendered = render_dossier(meta, merged)
        self.assertEqual(rendered, (
            "---\n"
            "person_id: person-a\n"
            'name: "Jordan Bravo"\n'
            "slug: jordan-bravo-persona\n"
            'emails: ["jordan@example.com"]\n'
            "phones: []\n"
            'source_channels: ["gmail_msgvault"]\n'
            "message_count: 0\n"
            'last_interaction: ""\n'
            "confidence: 0.9\n"
            "generated_at: 2026-01-02T03:04:05Z\n"
            "---\n\n# Jordan Bravo\n\n## Summary\n\nEngineer\n\n"
            "## Who they are\n\n- **Title:** Engineer\n\n"
            "## Identifiers\n\n- jordan@example.com"
        ))


class BatchFactsMergeTest(unittest.TestCase):
    """collapse_fact_records: reducing ONE person's own batches, not several children.

    Reproduces the real failure a paid 550-person run exposed: many batches
    describing the same event in different words used to union into many
    timeline entries (one dossier had 87, 64 paraphrasing one hackathon), and
    is_owner/relationship_category were dropped entirely by the
    several-children merge policy these batches used to go through.
    """

    def test_collapses_paraphrased_event_but_keeps_a_distinct_one(self) -> None:
        short_paraphrase = (
            "Reconnected at the 2022 founders hackathon in Austin and paired "
            "up for the weekend"
        )
        detailed_paraphrase = (
            "Reconnected at the 2022 founders hackathon in Austin and paired "
            "up for the entire weekend on a demo"
        )
        other_short_paraphrase = (
            "Reconnected at the 2022 founders hackathon in Austin and paired "
            "up over the weekend"
        )
        distinct_summary = "Helped negotiate the Series A term sheet in March 2023"

        merged = collapse_fact_records(filter(None, (
            FactRecord.from_payload({"facts": {
                "canonical_name": "Jordan Bravo",
                "notable_events": [{"date": "2022-06-01", "summary": short_paraphrase}],
                "confidence": 0.5,
            }}),
            FactRecord.from_payload({"facts": {
                "canonical_name": "Jordan Bravo",
                "notable_events": [
                    {"date": "2022-06-01", "summary": detailed_paraphrase},
                    {"date": "2023-03-01", "summary": distinct_summary},
                ],
                "confidence": 0.7,
            }}),
            FactRecord.from_payload({"facts": {
                "canonical_name": "Jordan Bravo",
                "notable_events": [{"date": "2022-06-01", "summary": other_short_paraphrase}],
                "confidence": 0.4,
            }}),
        )))

        self.assertIsNotNone(merged)
        # Four raw entries (three paraphrases of one event + one genuinely
        # distinct event) collapse to two: one per real event.
        self.assertEqual(len(merged.notable_events), 2)
        summaries = {event.summary for event in merged.notable_events}
        self.assertEqual(summaries, {detailed_paraphrase, distinct_summary})
        # The surviving paraphrase is the most detailed one, not an arbitrary
        # first-wins pick.
        hackathon_event = next(e for e in merged.notable_events if "hackathon" in e.summary)
        self.assertEqual(hackathon_event.summary, detailed_paraphrase)

    def test_notable_events_are_capped(self) -> None:
        records = (
            FactRecord.from_payload({"facts": {
                "canonical_name": "Jordan Bravo",
                "notable_events": [{
                    "date": f"2022-01-{index:02d}",
                    "summary": (
                        f"Distinct unrelated event number {index} about "
                        "something else entirely"
                    ),
                }],
                "confidence": 0.5,
            }})
            for index in range(1, 30)
        )

        merged = collapse_fact_records(filter(None, records))

        self.assertIsNotNone(merged)
        self.assertEqual(len(merged.notable_events), MAX_NOTABLE_EVENTS)

    def test_is_owner_true_from_any_batch_survives_merge(self) -> None:
        merged = collapse_fact_records(filter(None, (
            FactRecord.from_payload({"facts": {
                "canonical_name": "Jordan Bravo",
                "is_owner": False,
                "confidence": 0.5,
            }}),
            FactRecord.from_payload({"facts": {
                "canonical_name": "Jordan Bravo",
                "is_owner": True,
                "confidence": 0.6,
            }}),
        )))

        self.assertIsNotNone(merged)
        self.assertIs(merged.is_owner, True)
        self.assertIn("is_owner", merged.present)
        self.assertIs(merged.to_payload()["is_owner"], True)

    def test_is_owner_absent_everywhere_stays_none(self) -> None:
        merged = collapse_fact_records(filter(None, (
            FactRecord.from_payload({"facts": {
                "canonical_name": "Jordan Bravo",
                "confidence": 0.5,
            }}),
        )))

        self.assertIsNotNone(merged)
        self.assertIsNone(merged.is_owner)
        # Still present in the payload (to_payload always coerces to bool),
        # but the merge itself never fabricated a True.
        self.assertIs(merged.to_payload()["is_owner"], False)

    def test_relationship_category_kept_and_majority_wins(self) -> None:
        merged = collapse_fact_records(filter(None, (
            FactRecord.from_payload({"facts": {
                "canonical_name": "Jordan Bravo",
                "relationship_category": "work",
                "confidence": 0.4,
            }}),
            FactRecord.from_payload({"facts": {
                "canonical_name": "Jordan Bravo",
                "relationship_category": "work",
                "confidence": 0.5,
            }}),
            FactRecord.from_payload({"facts": {
                "canonical_name": "Jordan Bravo",
                "relationship_category": "personal",
                "confidence": 0.6,
            }}),
        )))

        self.assertIsNotNone(merged)
        self.assertEqual(merged.relationship_category, "work")
        self.assertIn("relationship_category", merged.present)

    def test_merge_is_deterministic_across_repeated_calls(self) -> None:
        chunks = (
            FactRecord.from_payload({"facts": {
                "canonical_name": "Jordan Bravo",
                "topics": ["Systems", "Hiking"],
                "notable_events": [{"date": "2022-06-01", "summary": "Met at a hackathon"}],
                "is_owner": True,
                "confidence": 0.5,
            }}),
            FactRecord.from_payload({"facts": {
                "canonical_name": "Jordan Bravo",
                "topics": ["systems", "Skiing"],
                "notable_events": [{"date": "2022-06-01", "summary": "Met at a hackathon in 2022"}],
                "confidence": 0.6,
            }}),
        )
        first = collapse_fact_records(filter(None, chunks))
        second = collapse_fact_records(filter(None, chunks))

        self.assertEqual(first.to_payload(), second.to_payload())


class ComposeDossierTest(unittest.TestCase):
    def test_validation_omits_fact_without_its_facts_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-jordan", "parent-worth:parent-jordan"),
                ArtifactRow(
                    "wrong-kind:parent-jordan",
                    ArtifactKind.SOURCE_BUNDLE.value,
                    "parent-jordan",
                    str(root / "bundle.json"),
                    "bundle-fingerprint",
                    ProjectionStatus.PROJECTED.value,
                    payload_json=json.dumps({"messages": []}),
                ),
                FactRow(
                    "parent-jordan",
                    "parent-jordan",
                    "wrong-kind:parent-jordan",
                    facts_json=json.dumps({"canonical_name": "Jordan Bravo"}),
                ),
            ))

            result = ValidateDossiers(db=db, dossier_dir=root / "dossiers").run()

            self.assertEqual(result["status"], "empty")
            self.assertEqual(result["people"], 0)

    def test_composed_and_parent_dossier_artifacts_coexist_and_converge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                OwnerContextRow(
                    "owner",
                    json.dumps({
                        "name": "Mailbox Owner",
                        "emails": ["owner@example.com"],
                        "phones": ["+15550101"],
                    }),
                    str(root / "owner.json"),
                    "owner-fingerprint",
                ),
                ParentRow(
                    "parent-jordan", "parent-worth:parent-jordan",
                    "Jordan Bravo", "jordan",
                ),
                PersonRow(
                    "person-jordan", "parent-jordan", "jordan-child",
                    "jordan", "Jordan Bravo",
                ),
                ArtifactRow(
                    "source-bundle:parent-jordan", ArtifactKind.SOURCE_BUNDLE.value,
                    "parent-jordan", str(root / "raw" / "parent-jordan.json"),
                    "source-fingerprint", ProjectionStatus.PROJECTED.value,
                    payload_json=json.dumps({
                        "person_id": "parent-jordan",
                        "full_name": "Jordan Bravo",
                        "emails": ["jordan@example.com"],
                        "phones": [],
                        "source_channels": ["gmail_msgvault"],
                        "messages": [],
                    }),
                ),
                ArtifactRow(
                    "facts:parent-jordan", ArtifactKind.FACTS.value,
                    "parent-jordan", str(root / "facts" / "parent-jordan.jsonl"),
                    "facts-fingerprint", ProjectionStatus.PROJECTED.value,
                    payload_json=json.dumps({"facts": {
                        "canonical_name": "Jordan Bravo",
                        "title": "Engineer",
                        "confidence": 0.9,
                    }}),
                ),
                FactRow(
                    "parent-jordan", "parent-jordan", "facts:parent-jordan",
                    confidence=0.9,
                    facts_json=json.dumps({
                        "canonical_name": "Jordan Bravo",
                        "title": "Engineer",
                        "confidence": 0.9,
                    }),
                ),
            ))
            dossiers = root / "dossiers"
            parents = root / "parents"

            ComposeDossier(
                db=db, dossier_dir=dossiers, index_md=root / "index.md",
            ).execute()
            first = BuildParents(db=db, parents_dir=parents).execute()
            with mock.patch(
                "packs.ingestion.primitives.deep_context.merge_candidates.rendering.render_singleton",
                side_effect=AssertionError("healed parent artifact must converge"),
            ):
                second = BuildParents(db=db, parents_dir=parents).execute()
            ComposeDossier(
                db=db, dossier_dir=dossiers, index_md=root / "index.md",
            ).execute()

            self.assertEqual((first.parents_changed, second.parents_changed), (1, 0))
            rows = db.query(
                "SELECT artifact_key, path FROM artifacts "
                "WHERE kind='dossier' AND parent_id='parent-jordan' "
                "AND person_id IS NULL ORDER BY artifact_key"
            )
            self.assertEqual([row["artifact_key"] for row in rows], [
                "dossier-parent:parent-jordan",
                "dossier:parent-jordan",
            ])
            self.assertEqual(
                {Path(row["path"]) for row in rows},
                {
                    (parents / "jordan-bravo-jordan.md").resolve(),
                    (dossiers / "jordan.md").resolve(),
                },
            )


def _seed_parent(
    root: Path, db: Db, parent_id: str, slug: str,
    *, name: str = "Jordan Bravo", broken: bool = False,
) -> None:
    """Project one composable parent: canonical-graph row, source bundle,
    facts artifact, and fact row. ``broken=True`` seeds a NULL ``facts_json``
    to make this one parent unparseable — the `facts` table's own CHECK
    constraint (`facts_json IS NULL OR json_valid(facts_json)`) rejects
    malformed JSON text outright, so NULL is the realistic way to reach
    ``ComposeDossier``'s ``json.loads(fact.facts_json or "")`` parse-failure
    branch through the store rather than around it.
    """
    facts_json = None if broken else json.dumps({
        "canonical_name": name, "title": "Engineer", "confidence": 0.9,
    })
    db.project_rows((
        ParentRow(parent_id, f"parent-worth:{parent_id}", name, slug),
        ArtifactRow(
            f"source-bundle:{parent_id}", ArtifactKind.SOURCE_BUNDLE.value,
            parent_id, str(root / "raw" / f"{parent_id}.json"),
            "source-fingerprint", ProjectionStatus.PROJECTED.value,
            payload_json=json.dumps({
                "person_id": parent_id,
                "full_name": name,
                "emails": [],
                "phones": [],
                "source_channels": ["gmail_msgvault"],
                "messages": [],
            }),
        ),
        ArtifactRow(
            f"facts:{parent_id}", ArtifactKind.FACTS.value,
            parent_id, str(root / "facts" / f"{parent_id}.jsonl"),
            "facts-fingerprint", ProjectionStatus.PROJECTED.value,
            payload_json=json.dumps({"facts": {
                "canonical_name": name, "title": "Engineer", "confidence": 0.9,
            }}),
        ),
        FactRow(
            parent_id, parent_id, f"facts:{parent_id}",
            confidence=0.9, facts_json=facts_json,
        ),
    ))


class ComposeDossierSkipTest(unittest.TestCase):
    """Per-parent isolation: one bad row is skipped and reported, never fatal."""

    def _seed_owner(self, root: Path, db: Db) -> None:
        db.project_rows((
            OwnerContextRow(
                "owner",
                json.dumps({"name": "Mailbox Owner", "emails": [], "phones": []}),
                str(root / "owner.json"), "owner-fingerprint",
            ),
        ))

    def test_broken_parent_is_skipped_not_fatal_to_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            self._seed_owner(root, db)
            _seed_parent(root, db, "parent-good", "good-slug")
            _seed_parent(root, db, "parent-bad", "bad-slug", broken=True)

            manifest = ComposeDossier(
                db=db, dossier_dir=root / "dossiers", index_md=root / "index.md",
            ).execute()

            self.assertEqual(manifest.status, "completed")
            self.assertEqual(manifest.dossiers_written, 1)
            self.assertEqual(manifest.skipped, 1)
            self.assertEqual(
                [(skip.parent_id) for skip in manifest.skip_reasons],
                ["parent-bad"],
            )
            self.assertIn("parent-bad", manifest.skip_reasons[0].reason)
            self.assertTrue((root / "dossiers" / "good-slug.md").exists())
            self.assertFalse((root / "dossiers" / "bad-slug.md").exists())

    def test_skip_preserves_prior_good_dossier_file_and_artifact_row(self) -> None:
        """THE TRAP: a parent that composed cleanly on run 1 and then breaks on
        run 2 must keep its run-1 dossier file AND artifact row byte-for-byte —
        orphan cleanup and stale-artifact retraction must both leave it alone.
        Remove either guard in compose_dossier.py and this test fails.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            self._seed_owner(root, db)
            _seed_parent(root, db, "parent-jordan", "jordan")
            dossiers = root / "dossiers"

            first = ComposeDossier(
                db=db, dossier_dir=dossiers, index_md=root / "index.md",
            ).execute()
            self.assertEqual((first.dossiers_written, first.skipped), (1, 0))
            dossier_path = dossiers / "jordan.md"
            self.assertTrue(dossier_path.exists())
            original_body = dossier_path.read_text(encoding="utf-8")
            original_row = dict(db.query(
                "SELECT artifact_key, path, content_fingerprint, payload_json "
                "FROM artifacts WHERE kind='dossier' AND artifact_key='dossier:parent-jordan'"
            )[0])

            # Same parent, same slug, but this run's facts are corrupt — a
            # transient bad row, not a reason to lose what run 1 produced.
            _seed_parent(root, db, "parent-jordan", "jordan", broken=True)

            second = ComposeDossier(
                db=db, dossier_dir=dossiers, index_md=root / "index.md",
            ).execute()

            self.assertEqual((second.dossiers_written, second.skipped), (0, 1))
            self.assertEqual(second.skip_reasons[0].parent_id, "parent-jordan")
            # The file survives the orphan-cleanup pass untouched.
            self.assertTrue(dossier_path.exists())
            self.assertEqual(dossier_path.read_text(encoding="utf-8"), original_body)
            # The artifact row survives the stale-retraction pass untouched —
            # not retracted, not rewritten (still the exact run-1 row).
            preserved_rows = db.query(
                "SELECT artifact_key, path, content_fingerprint, payload_json "
                "FROM artifacts WHERE kind='dossier' AND artifact_key='dossier:parent-jordan'"
            )
            self.assertEqual(len(preserved_rows), 1)
            self.assertEqual(dict(preserved_rows[0]), original_row)


if __name__ == "__main__":
    unittest.main()
