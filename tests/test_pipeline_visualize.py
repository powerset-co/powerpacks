"""The DAG renderer stays truthful to the declarations.

Created: 2026-07-26
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from packs.ingestion.primitives.pipeline import visualize  # noqa: E402
from packs.ingestion.primitives.pipeline.graph import node_subclasses  # noqa: E402


class VisualizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.nodes = [n for n in node_subclasses() if n.__module__.startswith("packs.")]
        cls.document = visualize.render(cls.nodes)

    def test_every_declared_node_is_rendered(self) -> None:
        for node in self.nodes:
            self.assertIn(node.name, self.document)

    def test_mermaid_labels_are_safe(self) -> None:
        # A curly brace (the per-account template) or a home-dir path would
        # break the diagram or leak the username into a shareable document.
        mermaid_block = self.document.split("```mermaid")[1].split("```")[0]
        self.assertNotIn("{account_slug}", mermaid_block)
        self.assertNotIn(str(Path.home()), self.document)

    def test_a_two_writer_file_keeps_both_producers_edges(self) -> None:
        # messages/contacts.csv has two declared writers (discovery merge +
        # matcher); a last-wins producer map dropped the first writer's edges
        # once. Both must emit.
        self.assertIn('messages_stage_merge -->|"messages/contacts.csv"| messages_match_local', self.document)
        self.assertIn('messages_stage_merge -->|"messages/contacts.csv"| messages_import', self.document)

    def test_dead_outputs_are_rendered_as_leaves(self) -> None:
        self.assertIn("no reader yet", self.document)


if __name__ == "__main__":
    unittest.main()
