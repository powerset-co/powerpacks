"""Worth tagging reuses paid synthesis and never sends worth back to GPT.

Adapted from main's monolithic-module version after the SQLite rewrite split
``deep_context.synthesize_person_context`` into
``deep_context/synthesis/{prompting,selection,runner}`` over the canonical
SQLite store: bundles now come from projected SOURCE_BUNDLE rows and worth is
read back from ``facts.facts_json``, not a review.csv.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    OwnerContextRow,
    ParentRow,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.shared import openai_responses
from packs.ingestion.primitives.deep_context.synthesis import (
    prompting,
    runner,
    selection,
)
from packs.ingestion.primitives.deep_context.synthesis.synthesize_person_context import (
    SynthesizePersonContext,
)
from deep_context_sqlite_test_helpers import message_payload


BUNDLE = {
    "person_id": "p1",
    "full_name": "Jordan Bravo",
    "source_channels": ["gmail_msgvault"],
    "messages": [
        message_payload(
            "Ready.",
            channel="gmail",
            at="2026-01-02T03:04:05Z",
            subject="Launch",
        )
    ],
}


class SynthesisJevTests(unittest.TestCase):
    def test_gpt_prompt_and_schema_do_not_judge_worth(self) -> None:
        self.assertNotIn("network_worth", prompting.SYSTEM_PROMPT)
        self.assertNotIn("network_worth", prompting.FACT_SCHEMA["properties"])
        bundle = CollectionBundle.from_payload(BUNDLE)
        self.assertNotIn(
            "WORTH SOURCE POLICY",
            prompting.render_batch(bundle, list(bundle.messages), None),
        )

    def test_only_untagged_or_uncached_saved_facts_are_tagging_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node, database = self._node(root)
            path = self._write_facts(root, facts={"canonical_name": "Jordan Bravo"})
            owner = {"name": "Mailbox Owner"}
            bundles = selection.effective_parent_bundles(database)

            with patch.object(
                runner.jev_worth, "estimate", return_value={"cached": True, "cost_usd": 0}
            ):
                # Untagged facts are always a tagging target, even when the
                # request is already cached.
                self.assertEqual(runner._tagging_paths(node.config, bundles, owner), [path])
                tagged = json.loads(path.read_text(encoding="utf-8"))
                tagged["facts"]["labels"] = {"is_professional": 0.9}
                path.write_text(json.dumps(tagged) + "\n", encoding="utf-8")
                # Saved labels + a cached request: nothing to redo.
                self.assertEqual(runner._tagging_paths(node.config, bundles, owner), [])
            with patch.object(
                runner.jev_worth, "estimate", return_value={"cached": False, "cost_usd": 0.01}
            ):
                # Saved labels but a changed request (cache miss) relabels anyway.
                self.assertEqual(runner._tagging_paths(node.config, bundles, owner), [path])

    def test_existing_facts_are_tagged_without_gpt_and_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node, database = self._node(root)
            path = self._write_facts(root, facts={"canonical_name": "Jordan Bravo"})
            self._mark_facts_cached(root, node)

            with patch.object(
                openai_responses, "AsyncOpenAI", side_effect=AssertionError("must reuse GPT facts")
            ), patch.object(
                runner.jev_worth, "classify", AsyncMock(return_value=self._answer())
            ) as classify, patch.object(
                runner.jev_worth, "estimate", return_value={"cached": True, "cost_usd": 0}
            ):
                result = node.execute()
                node.execute()
            classify.assert_awaited_once()
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["facts"]["network_worth"]["decision"], "yes")
            self.assertEqual(record["facts"]["labels"], {"is_professional": 0.9})
            backup = json.loads(path.with_suffix(".jsonl.bkup").read_text(encoding="utf-8"))
            self.assertEqual(backup["facts"], {"canonical_name": "Jordan Bravo"})
            self.assertEqual(result.jev.people, 1)
            self.assertGreater(result.estimated_cost_usd, 0)
            # The share stage reads labels from SQLite's facts.facts_json, not the
            # jsonl, so tagging must re-project each record.
            facts_json = database.query(
                "SELECT facts_json, machine_worth FROM facts WHERE subject_key='p1'"
            )[0]
            self.assertEqual(json.loads(facts_json["facts_json"])["labels"], {"is_professional": 0.9})
            self.assertEqual(facts_json["machine_worth"], "yes")

    def test_jev_failure_preserves_new_gpt_checkpoint_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node, _ = self._node(root, bundle=None)
            path = self._write_facts(root, facts={"canonical_name": "Jordan Bravo"}, artifact=False)
            self.assertFalse(path.with_suffix(".jsonl.bkup").exists())

            with patch.object(
                runner.jev_worth, "classify", AsyncMock(side_effect=RuntimeError("JEV unavailable"))
            ):
                with self.assertRaisesRegex(RuntimeError, "JEV unavailable"):
                    node.execute()

            # A failed label request never rewrites or hides the GPT checkpoint.
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["facts"]["canonical_name"],
                "Jordan Bravo",
            )
            self.assertFalse(path.with_suffix(".jsonl.bkup").exists())

    def test_rejudge_only_replays_jev_and_keeps_the_reference_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node, _ = self._node(root)
            path = self._write_facts(root, facts={"canonical_name": "Jordan Bravo"})
            self._mark_facts_cached(root, node)

            with patch.object(
                runner.jev_worth, "classify", AsyncMock(return_value=self._answer())
            ) as classify:
                node.execute()
                timestamp = json.loads(path.read_text(encoding="utf-8"))["updated_at"]
                rejudge = self._node(root, rejudge=True)[0].execute()

            self.assertEqual(classify.await_count, 2)
            # --rejudge reproduces the same reference_date, so the cached request
            # key is stable instead of silently re-billing every person.
            self.assertEqual(classify.await_args.kwargs["reference_date"], timestamp[:10])
            self.assertEqual(rejudge.jev.people, 1)

    def test_estimate_includes_facts_only_tagging_without_gpt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node, _ = self._node(root)
            self._write_facts(root, facts={"canonical_name": "Jordan Bravo"})
            self._mark_facts_cached(root, node)

            with patch.object(
                runner.jev_worth, "estimate", return_value={"cost_usd": 0.01, "cached": False}
            ):
                estimate = node.estimate()
            self.assertEqual(estimate["people"], 0)
            self.assertEqual(estimate["jev_people"], 1)
            self.assertEqual(estimate["estimated_cost_floor_usd"], 0.01)
            self.assertEqual(estimate["estimated_cost_ceiling_usd"], 0.01)

    def test_changed_request_relabels_even_with_saved_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node, _ = self._node(root)
            path = self._write_facts(
                root,
                facts={"canonical_name": "Jordan Bravo", "labels": {"is_professional": 0.1}},
            )
            self._mark_facts_cached(root, node)

            with patch.object(
                runner.jev_worth, "estimate", return_value={"cached": False, "cost_usd": 0.01}
            ), patch.object(
                runner.jev_worth, "classify", AsyncMock(return_value=self._answer())
            ) as classify:
                node.execute()
            classify.assert_awaited_once()
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["facts"]["labels"],
                {"is_professional": 0.9},
            )

    def test_rejudge_does_not_synthesize_unprocessed_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node, _ = self._node(root)
            self.assertEqual(self._node(root, rejudge=True)[0]._plan().bundles, ())
            # Plain rejudge leaves the paid synthesis population empty even when
            # the parent has no cached facts yet.
            self.assertEqual(node._plan().bundles, (CollectionBundle.from_payload(BUNDLE),))

    # --- fixtures ---------------------------------------------------------

    def _answer(self) -> dict:
        return {
            "network_worth": {"decision": "yes", "reason": "professional context"},
            "labels": {"is_professional": 0.9},
            "usage": {"input_tokens": 200, "output_tokens": 50, "cached": False},
        }

    def _node(self, root: Path, *, bundle: dict | None = BUNDLE, rejudge: bool = False):
        database = Db(root / "deep-context.sqlite")
        rows = [
            OwnerContextRow(
                "owner",
                json.dumps({"name": "Mailbox Owner"}),
                str(root / "owner.json"),
                "0" * 64,
            ),
            ParentRow("p1", "p1"),
            PersonRow("person-1", "p1"),
        ]
        if bundle is not None:
            rows.append(
                ArtifactRow(
                    "source-bundle:p1",
                    "source_bundle",
                    "p1",
                    str(root / "raw" / "p1.json"),
                    "1" * 64,
                    "projected",
                    payload_json=json.dumps(bundle),
                )
            )
        database.project_rows(tuple(rows))
        (root / "raw").mkdir(exist_ok=True)
        node = SynthesizePersonContext(
            db=database,
            raw_dir=root / "raw",
            out_dir=root / "facts",
            concurrency=1,
            rejudge=rejudge,
        )
        return node, database

    def _write_facts(
        self,
        root: Path,
        *,
        facts: dict,
        artifact: bool = True,
    ) -> Path:
        facts_dir = root / "facts"
        facts_dir.mkdir(exist_ok=True)
        path = facts_dir / "p1.jsonl"
        path.write_text(json.dumps({"synthesis_version": "old", "facts": facts}) + "\n", encoding="utf-8")
        if artifact:
            self._project(root, path)
        return path

    def _mark_facts_cached(self, root: Path, node) -> None:
        """Rewrite the record so selection's fingerprint/version check skips GPT."""
        path = root / "facts" / "p1.jsonl"
        bundle = next(iter(selection.effective_parent_bundles(node.db).values()))
        record = json.loads(path.read_text(encoding="utf-8"))
        record["synthesis_version"] = prompting.SYNTHESIS_VERSION
        record["input_evidence_fingerprint"] = prompting.input_evidence_fingerprint(
            bundle,
            system_prompt=selection.build_system_prompt(node.db),
            chunk_chars=node.config.chunk_chars,
            max_batches=node.config.max_batches,
        )
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        self._project(root, path)

    def _project(self, root: Path, path: Path) -> None:
        database = Db(root / "deep-context.sqlite")
        project_parent_fact(database, path, "p1")


if __name__ == "__main__":
    unittest.main()
