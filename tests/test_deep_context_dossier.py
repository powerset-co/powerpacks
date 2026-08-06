"""Direct contracts for dossier fact reduction, rendering, and scoped composition."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context.compose_dossier import ComposeDossier
from packs.ingestion.primitives.deep_context.dossier.facts import headline, merge_facts
from packs.ingestion.primitives.deep_context.dossier.rendering import render_dossier


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
    def test_scoped_person_preserves_other_slugs_parents_and_catalog(self) -> None:
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
            index.write_text(json.dumps({
                "slugs": {"casey-delta": {
                    "person_id": "person-b", "name": "Casey Delta",
                    "path": "dossiers/casey-delta.md", "headline": "Friend",
                    "full_name": "Casey Delta", "emails": [], "phones": [],
                }},
                "parents": {"casey-parent": {
                    "parent_id": "parent-casey", "name": "Casey Delta",
                    "path": "parents/casey-parent.md", "children": ["casey-delta"],
                    "needs_review": [],
                }},
            }))
            (dossiers / "casey-delta.md").write_text("# Casey Delta\n")
            catalog = root / "index.md"
            with mock.patch(
                "packs.ingestion.primitives.deep_context.dossier.rendering.now_iso",
                return_value="2026-01-02T03:04:05Z",
            ):
                result = ComposeDossier(
                    raw_dir=raw, facts_dir=facts, dossier_dir=dossiers,
                    index_json=index, index_md=catalog, person="person-a",
                ).execute()
            document = json.loads(index.read_text())
            self.assertEqual(result.dossiers_written, 1)
            self.assertEqual(set(document["slugs"]), {"casey-delta", "jordan-bravo-persona"})
            self.assertEqual(set(document["parents"]), {"casey-parent"})
            self.assertEqual((dossiers / "casey-delta.md").read_text(), "# Casey Delta\n")
            self.assertEqual(catalog.read_text(), (
                "# Deep-context dossiers (2)\n\n"
                "_Generated 2026-01-02T03:04:05Z._\n\n"
                "- [[casey-delta]] **Casey Delta** — Friend\n"
                "- [[jordan-bravo-persona]] **Jordan Bravo** — Engineer\n"
            ))


if __name__ == "__main__":
    unittest.main()
