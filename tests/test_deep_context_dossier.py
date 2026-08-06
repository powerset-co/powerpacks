"""Direct contracts for dossier fact reduction, rendering, and scoped composition."""
from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
            "## Identifiers\n\n- jordan@example.com\n\n"
            "## Possible same person\n\n_None detected yet._\n"
        ))


class ComposeDossierTest(unittest.TestCase):
    def test_scoped_person_preserves_other_sqlite_dossiers_and_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, facts, dossiers = root / "raw", root / "facts", root / "dossiers"
            for path in (raw, facts, dossiers):
                path.mkdir()
            (raw / "person-a.json").write_text(json.dumps({
                "person_id": "person-a", "full_name": "Jordan Bravo",
                "emails": ["jordan@example.com"], "phones": [],
                "source_channels": ["gmail_msgvault"], "messages": [],
            }))
            (facts / "person-a.jsonl").write_text(json.dumps({"facts": {
                "canonical_name": "Jordan Bravo", "title": "Engineer", "confidence": 0.9,
            }}) + "\n")
            index = root / "index.json"
            index.write_bytes(b"legacy index must stay untouched\n")
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
                    "source-bundle:person-a", ArtifactKind.SOURCE_BUNDLE.value,
                    "parent-jordan", str(raw / "person-a.json"), "source-fingerprint",
                    ProjectionStatus.PROJECTED.value, person_id="person-a",
                    payload_json=json.dumps({
                        "person_id": "person-a", "full_name": "Jordan Bravo",
                        "emails": ["jordan@example.com"], "phones": [],
                        "source_channels": ["gmail_msgvault"], "messages": [],
                    }),
                ),
                ArtifactRow(
                    "facts:person-a", ArtifactKind.FACTS.value, "parent-jordan",
                    str(facts / "person-a.jsonl"), "facts-fingerprint",
                    ProjectionStatus.PROJECTED.value, person_id="person-a",
                    payload_json=json.dumps({"facts": {
                        "canonical_name": "Jordan Bravo", "title": "Engineer",
                        "confidence": 0.9,
                    }}),
                ),
                FactRow(
                    "person-a", "parent-jordan", "facts:person-a", "person-a",
                    confidence=0.9,
                    facts_json=json.dumps({
                        "canonical_name": "Jordan Bravo", "title": "Engineer",
                        "confidence": 0.9,
                    }),
                ),
                ArtifactRow(
                    "dossier-person:person-b", "dossier", "parent-casey",
                    str(casey_path), hashlib.sha256(casey_data).hexdigest(), "projected",
                    person_id="person-b", payload_json=json.dumps({
                        "person_id": "person-b", "name": "Casey Delta",
                        "path": "dossiers/casey-delta.md", "headline": "Friend",
                        "full_name": "Casey Delta", "emails": [], "phones": [],
                    }),
                ),
            ))
            catalog = root / "index.md"
            (raw / "person-a.json").unlink()
            (facts / "person-a.jsonl").unlink()
            with mock.patch(
                "packs.ingestion.primitives.deep_context.dossier.rendering.now_iso",
                return_value="2026-01-02T03:04:05Z",
            ):
                result = ComposeDossier(
                    db=db,
                    raw_dir=raw, facts_dir=facts, dossier_dir=dossiers,
                    index_json=index, index_md=catalog, person="person-a",
                ).execute()
            self.assertEqual(result.dossiers_written, 1)
            self.assertEqual(index.read_bytes(), b"legacy index must stay untouched\n")
            self.assertEqual((dossiers / "casey-delta.md").read_text(), "# Casey Delta\n")
            self.assertEqual(catalog.read_text(), (
                "# Deep-context dossiers (2)\n\n"
                "_Generated 2026-01-02T03:04:05Z._\n\n"
                "- [[casey-delta]] **Casey Delta** — Friend\n"
                "- [[jordan-bravo-persona]] **Jordan Bravo** — Engineer\n"
            ))
            validation = ValidateDossiers(db=db, dossier_dir=dossiers).run()
            self.assertEqual(validation["people"], 1)
            self.assertEqual(validation["confidence_mean"], 0.9)


if __name__ == "__main__":
    unittest.main()
