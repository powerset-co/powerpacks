"""Direct contracts for dossier fact reduction, rendering, and scoped composition."""
from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.build_parents import BuildParents
from packs.ingestion.primitives.deep_context.compose_dossier import ComposeDossier
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    FactRow,
    ParentRow,
    PersonRow,
    ProjectionStatus,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier.facts import headline, merge_facts
from packs.ingestion.primitives.deep_context.dossier.rendering import render_dossier
from packs.ingestion.primitives.deep_context.validate_dossiers import ValidateDossiers


class DossierFactsTest(unittest.TestCase):
    def test_merge_policy_and_headline_live_in_concrete_module(self) -> None:
        merged = merge_facts([
            {"facts": {
                "canonical_name": "Jordan Bravo",
                "employers": [{"name": "Example Labs", "role": "Builder", "status": "past"}],
                "topics": ["Systems"],
                "network_worth": {"decision": "maybe", "reason": "early evidence"},
                "confidence": 0.6,
            }},
            {"facts": {
                "canonical_name": "Jordan Bravo",
                "employers": [{"name": "Example Labs", "role": "", "status": "current"}],
                "title": "Engineer",
                "topics": ["systems", "Testing"],
                "network_worth": {"decision": "yes", "reason": "known collaborator"},
                "confidence": 0.9,
            }},
        ])
        self.assertEqual(merged["employers"], [
            {"name": "Example Labs", "role": "Builder", "status": "current"},
        ])
        self.assertEqual(merged["topics"], ["Systems", "Testing"])
        self.assertEqual(merged["network_worth"], {
            "decision": "yes", "reason": "known collaborator",
        })
        self.assertEqual(headline(merged), "Engineer at Example Labs")

    def test_rendered_dossier_bytes_stay_pinned(self) -> None:
        meta = {
            "person_id": "person-a", "full_name": "Jordan Bravo",
            "emails": ["jordan@example.com"], "phones": [],
            "source_channels": ["gmail_msgvault"], "messages": [],
        }
        merged = {
            "canonical_name": "Jordan Bravo", "confidence": 0.9,
            "title": "Engineer", "employers": [], "topics": [],
            "identifiers": [], "network_worth": {},
        }
        with mock.patch(
            "packs.ingestion.primitives.deep_context.dossier.rendering.now_iso",
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
                "packs.ingestion.primitives.deep_context.parents.rendering.render_singleton",
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

    def test_scoped_person_preserves_other_sqlite_dossiers_and_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, facts, dossiers = root / "raw", root / "facts", root / "dossiers"
            for path in (raw, facts, dossiers):
                path.mkdir()
            (raw / "parent-jordan.json").write_text(json.dumps({
                "person_id": "parent-jordan", "full_name": "Jordan Bravo",
                "emails": ["jordan@example.com"], "phones": [],
                "source_channels": ["gmail_msgvault"], "messages": [],
            }))
            (facts / "parent-jordan.jsonl").write_text(json.dumps({"facts": {
                "canonical_name": "Jordan Bravo", "title": "Engineer", "confidence": 0.9,
            }}) + "\n")
            casey_path = dossiers / "casey-delta.md"
            casey_path.write_text("# Casey Delta\n")
            casey_data = casey_path.read_bytes()
            db = Db(root / "deep-context.sqlite")
            db.project_rows((
                ParentRow("parent-jordan", "parent-worth:parent-jordan", "Jordan Bravo", "jordan"),
                ParentRow("parent-casey", "parent-worth:parent-casey", "Casey Delta", "casey-parent"),
                PersonRow("person-a", "parent-jordan", "old-jordan", "jordan", "Jordan Bravo"),
                PersonRow("person-b", "parent-casey", "casey-delta", "casey-parent", "Casey Delta"),
                ArtifactRow(
                    "source-bundle:parent-jordan", ArtifactKind.SOURCE_BUNDLE.value,
                    "parent-jordan", str(raw / "parent-jordan.json"), "source-fingerprint",
                    ProjectionStatus.PROJECTED.value,
                    payload_json=json.dumps({
                        "person_id": "parent-jordan", "full_name": "Jordan Bravo",
                        "emails": ["jordan@example.com"], "phones": [],
                        "source_channels": ["gmail_msgvault"], "messages": [],
                    }),
                ),
                ArtifactRow(
                    "facts:parent-jordan", ArtifactKind.FACTS.value, "parent-jordan",
                    str(facts / "parent-jordan.jsonl"), "facts-fingerprint",
                    ProjectionStatus.PROJECTED.value,
                    payload_json=json.dumps({"facts": {
                        "canonical_name": "Jordan Bravo", "title": "Engineer",
                        "confidence": 0.9,
                    }}),
                ),
                FactRow(
                    "parent-jordan", "parent-jordan", "facts:parent-jordan",
                    confidence=0.9,
                    facts_json=json.dumps({
                        "canonical_name": "Jordan Bravo", "title": "Engineer",
                        "confidence": 0.9,
                    }),
                ),
                ArtifactRow(
                    "dossier:parent-casey", "dossier", "parent-casey",
                    str(casey_path), hashlib.sha256(casey_data).hexdigest(), "projected",
                    payload_json=json.dumps({
                        "parent_id": "parent-casey", "name": "Casey Delta",
                        "path": "dossiers/casey-delta.md", "headline": "Friend",
                        "full_name": "Casey Delta", "emails": [], "phones": [],
                    }),
                ),
            ))
            catalog = root / "index.md"
            (raw / "parent-jordan.json").unlink()
            (facts / "parent-jordan.jsonl").unlink()
            with mock.patch(
                "packs.ingestion.primitives.deep_context.dossier.rendering.now_iso",
                return_value="2026-01-02T03:04:05Z",
            ):
                result = ComposeDossier(
                    db=db,
                    dossier_dir=dossiers,
                    index_md=catalog, person="person-a",
                ).execute()
            self.assertEqual(result.dossiers_written, 1)
            self.assertEqual((dossiers / "casey-delta.md").read_text(), "# Casey Delta\n")
            self.assertEqual(catalog.read_text(), (
                "# Deep-context dossiers (2)\n\n"
                "_Generated 2026-01-02T03:04:05Z._\n\n"
                "- [[casey-parent]] **Casey Delta** — Friend\n"
                "- [[jordan]] **Jordan Bravo** — Engineer\n"
            ))
            validation = ValidateDossiers(db=db, dossier_dir=dossiers).run()
            self.assertEqual(validation["people"], 1)
            self.assertEqual(validation["confidence_mean"], 0.9)


if __name__ == "__main__":
    unittest.main()
