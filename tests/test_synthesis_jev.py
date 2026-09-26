"""Worth tagging reuses paid synthesis and never sends worth back to GPT."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    OwnerContextRow,
    OwnerProfile,
    ParentRow,
    PersonRow,
)
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_fact
from packs.ingestion.primitives.deep_context.jev_worth.models import WorthResult, WorthEstimate
from packs.ingestion.primitives.deep_context.synthesis.models import JevUsage, NetworkWorthFact
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
            owner = OwnerProfile("Mailbox Owner")
            bundles = selection.effective_parent_bundles(database)

            with patch.object(
                runner.jev_worth, "estimate", return_value=WorthEstimate(cached=True)
            ):
                # Untagged facts are always a tagging target, even when the
                # request is already cached.
                self.assertEqual(runner._tagging_paths(database, node.config, bundles, owner, headlines={}), [("p1", path.resolve())])
                tagged = json.loads(path.read_text(encoding="utf-8"))
                tagged["facts"]["labels"] = {"is_professional": 0.9}
                path.write_text(json.dumps(tagged) + "\n", encoding="utf-8")
                # Saved labels + a cached request: nothing to redo.
                self.assertEqual(runner._tagging_paths(database, node.config, bundles, owner, headlines={}), [])
            with patch.object(
                runner.jev_worth, "estimate", return_value=WorthEstimate(cost_usd=0.01)
            ):
                # Saved labels but a changed request (cache miss) relabels anyway.
                self.assertEqual(runner._tagging_paths(database, node.config, bundles, owner, headlines={}), [("p1", path.resolve())])

    def test_tagging_reads_the_projected_artifact_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node, database = self._node(root)
            path = root / "facts" / "renamed.jsonl"
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"facts": {"canonical_name": "Jordan Bravo"}}) + "\n", encoding="utf-8")
            project_parent_fact(database, path, "p1")
            bundles = selection.effective_parent_bundles(database)

            with patch.object(
                runner.jev_worth, "estimate", return_value=WorthEstimate(cached=True)
            ):
                # The path comes from the projected artifact, not from the parent id.
                self.assertEqual(
                    runner._tagging_paths(database, node.config, bundles, OwnerProfile("Mailbox Owner"), headlines={}),
                    [("p1", path.resolve())],
                )

    def test_tagging_ignores_stale_and_missing_fact_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node, database = self._node(root)
            current = self._write_facts(root, facts={"canonical_name": "Jordan Bravo"})
            self._mark_facts_cached(root, node)
            stale = root / "facts" / "merged-away.jsonl"
            stale.write_text(json.dumps({"facts": {"canonical_name": "Merged Away"}}) + "\n")
            database.project_rows((ParentRow("p2", "p2"), PersonRow("person-2", "p2")))
            missing = root / "facts" / "p2.jsonl"
            missing.write_text(json.dumps({"facts": {"canonical_name": "Missing File"}}) + "\n")
            project_parent_fact(database, missing, "p2")
            missing.unlink()

            with patch.object(
                runner.jev_worth, "classify", AsyncMock(return_value=self._answer())
            ) as classify, patch.object(
                runner.jev_worth, "estimate", return_value=WorthEstimate(cached=True)
            ):
                self.assertEqual(node.estimate()["jev_people"], 1)
                result = node.execute()

            classify.assert_awaited_once()
            self.assertEqual(result.jev.people, 1)
            self.assertEqual(classify.await_args.kwargs["facts"].facts.canonical_name, "Jordan Bravo")
            self.assertTrue(stale.exists())
            self.assertFalse(missing.exists())
            self.assertEqual(json.loads(current.read_text())["facts"]["labels"], {"is_professional": 0.9})

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
                runner.jev_worth, "estimate", return_value=WorthEstimate(cached=True)
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
            path = self._write_facts(root, facts={"canonical_name": "Jordan Bravo"})
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

    def test_estimate_includes_facts_only_tagging_without_gpt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node, _ = self._node(root)
            self._write_facts(root, facts={"canonical_name": "Jordan Bravo"})
            self._mark_facts_cached(root, node)

            with patch.object(
                runner.jev_worth, "estimate", return_value=WorthEstimate(cost_usd=0.01)
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
                runner.jev_worth, "estimate", return_value=WorthEstimate(cost_usd=0.01)
            ), patch.object(
                runner.jev_worth, "classify", AsyncMock(return_value=self._answer())
            ) as classify:
                node.execute()
            classify.assert_awaited_once()
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["facts"]["labels"],
                {"is_professional": 0.9},
            )

    def test_tagging_preserves_historical_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node, db = self._node(root, bundle=None)
            path = self._write_facts(root, facts={"title": "Engineer", "canonical_name": "Jordan Bravo"})
            record = json.loads(path.read_text())
            record.update(input_evidence_fingerprint="seed:historical", updated_at="2026-01-01T00:00:00Z",
                          historical_note={"keep": True}, jev_usage={"input_tokens": 1})
            path.write_text(json.dumps(record) + "\n")
            project_parent_fact(db, path, "p1")
            with patch.object(runner.jev_worth, "classify", AsyncMock(return_value=self._answer())):
                node.execute()
            saved = json.loads(path.read_text())
            for key in ("synthesis_version", "input_evidence_fingerprint", "updated_at", "historical_note"):
                self.assertEqual(saved[key], record[key])
            self.assertEqual(list(saved), list(record))
            self.assertEqual(list(saved["facts"])[:2], ["title", "canonical_name"])
            self.assertEqual(saved["jev_usage"], self._answer().usage_payload())

    # --- fixtures ---------------------------------------------------------

    def _answer(self) -> WorthResult:
        return WorthResult(NetworkWorthFact("yes", "professional context"), {"is_professional": 0.9},
                           JevUsage(people=1, input_tokens=200, output_tokens=50, cost_usd=0.0000084), 200, 50)

    def test_notable_roster_headline_retags_a_non_yes_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            people_csv = root / "people.csv"
            people_csv.write_text(
                "id,full_name,headline,public_identifier\nperson-1,Jordan Bravo,CEO @ Example Labs,jordan-bravo\n",
                encoding="utf-8",
            )
            node, database = self._node(root, people_csv=people_csv)
            path = self._write_facts(root, facts={
                "canonical_name": "Jordan Bravo",
                "labels": {"is_professional": 0.9},
                "network_worth": {"decision": "maybe", "reason": "thin"},
            })
            self._mark_facts_cached(root, node)
            owner = OwnerProfile("Mailbox Owner")
            bundles = selection.effective_parent_bundles(database)
            headlines = runner.parent_headlines(database, people_csv)
            self.assertEqual(headlines, {"p1": "CEO @ Example Labs"})

            with patch.object(runner.jev_worth, "estimate", return_value=WorthEstimate(cached=True)):
                # Tagged, cached, but a notable title and a non-yes verdict: re-tag at $0.
                self.assertEqual(
                    runner._tagging_paths(database, node.config, bundles, owner, headlines=headlines),
                    [("p1", path.resolve())],
                )
                tagged = json.loads(path.read_text(encoding="utf-8"))
                tagged["facts"]["network_worth"] = {"decision": "yes", "reason": "fine"}
                path.write_text(json.dumps(tagged) + "\n", encoding="utf-8")
                self.assertEqual(
                    runner._tagging_paths(database, node.config, bundles, owner, headlines=headlines),
                    [],
                )
                tagged["facts"]["network_worth"] = {"decision": "maybe", "reason": "thin"}
                path.write_text(json.dumps(tagged) + "\n", encoding="utf-8")
                self._project(root, path)

            # The real tagging pass, answers from cache, model still says maybe: the
            # saved record and the store end up yes with the notable reason.
            async def cached_answers(requests, **kwargs):
                return {
                    key: SimpleNamespace(
                        response={"answers": {"is_professional": {"type": "noul", "noul": 0.9}},
                                  "usage": {"input_tokens": 0, "output_tokens": 0}},
                        cached=True,
                    )
                    for key in requests
                }

            with patch.object(
                openai_responses, "AsyncOpenAI", side_effect=AssertionError("must reuse GPT facts")
            ), patch.object(runner.jev_worth, "estimate", return_value=WorthEstimate(cached=True)), \
                    patch.object(runner.jev_worth, "answer_requests", cached_answers), \
                    patch.object(runner.jev_worth, "predict", return_value="maybe"):
                result = node.execute()
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["facts"]["network_worth"],
                {"decision": "yes", "reason": runner.jev_worth.NOTABLE_REASON_PREFIX + "CEO @ Example Labs"},
            )
            self.assertEqual(result.jev.people, 1)
            self.assertEqual(result.jev.cost_usd, 0)
            stored = database.query("SELECT machine_worth FROM facts WHERE subject_key='p1'")[0]
            self.assertEqual(stored["machine_worth"], "yes")

    def _node(self, root: Path, *, bundle: dict | None = BUNDLE, people_csv: Path | None = None):
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
            people_csv=people_csv,
            concurrency=1,
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
