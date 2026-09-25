"""Worth tagging reuses paid synthesis and never sends worth back to GPT."""
import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context import synthesize_person_context as synth


class SynthesisJevTests(unittest.TestCase):
    def test_gpt_prompt_and_schema_do_not_judge_worth(self):
        self.assertNotIn("network_worth", synth.SYSTEM_PROMPT)
        self.assertNotIn("network_worth", synth.FACT_SCHEMA["properties"])
        self.assertNotIn("WORTH SOURCE POLICY", synth.render_batch({}, [], None))

    def test_cached_maybe_and_old_facts_never_trigger_gpt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, facts = root / "raw", root / "facts"
            raw.mkdir()
            facts.mkdir()
            bundle = raw / "p1.json"
            bundle.write_text(json.dumps({"person_id": "p1", "messages": [{"text": "Hello"}]}))
            (facts / "p1.jsonl").write_text(json.dumps({
                "synthesis_version": "previous-paid-version",
                "facts": {"canonical_name": "Jordan Bravo", "network_worth": {"decision": "maybe"}},
            }) + "\n")
            self.assertEqual(synth.pending_target_paths(raw, facts, force=False, person_id=""), [])
            self.assertEqual(synth.pending_target_paths(raw, facts, force=True, person_id=""), [bundle])

    def _node(self, root, **kwargs):
        return synth.SynthesizePersonContext(raw_dir=root / "raw", out_dir=root / "facts",
                                            review_csv=root / "review.csv", no_owner=True,
                                            concurrency=1, **kwargs)

    def _write_facts(self, root):
        (root / "facts").mkdir()
        path = root / "facts" / "p1.jsonl"
        path.write_text(json.dumps({"synthesis_version": "old", "usage": {"input_tokens": 400},
                                    "facts": {"canonical_name": "Jordan Bravo", "network_worth": {"decision": "maybe"}}}) + "\n")
        return path

    def _answer(self):
        return {"network_worth": {"decision": "yes", "reason": "professional context"},
                "labels": {"friend": 0.9},
                "usage": {"input_tokens": 200, "output_tokens": 50, "cached": False}}

    def test_existing_facts_are_tagged_without_gpt_and_only_once(self):
        from unittest.mock import AsyncMock, patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._write_facts(root)
            (root / "raw").mkdir()
            (root / "raw" / "manifest.json").write_text('{"status": "completed"}')
            with patch.object(synth.jev_worth, "classify", AsyncMock(return_value=self._answer())) as classify, \
                    patch.object(synth, "make_async_client", side_effect=AssertionError("must reuse GPT facts")), \
                    patch.object(synth, "load_env"), \
                    patch.object(synth.jev_worth, "estimate", return_value={"cached": True, "cost_usd": 0}):
                result = self._node(root).execute()
                self._node(root).execute()
            classify.assert_awaited_once()
            record = json.loads(path.read_text())
            self.assertEqual(record["facts"]["network_worth"]["decision"], "yes")
            self.assertEqual(record["facts"]["labels"], {"friend": 0.9})
            self.assertEqual(record["usage"]["input_tokens"], 400)
            self.assertEqual(json.loads(path.with_suffix(".jsonl.bkup").read_text())["facts"]["network_worth"]["decision"], "maybe")
            self.assertEqual(result.jev["people"], 1)
            self.assertGreater(result.estimated_cost_usd, 0)

    def test_jev_failure_preserves_new_gpt_checkpoint_for_retry(self):
        from unittest.mock import AsyncMock, patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir()
            (root / "raw" / "p1.json").write_text(json.dumps({
                "person_id": "p1", "messages": [{"text": "Work project", "channel": "gmail"}],
            }))
            client = AsyncMock()
            with patch.object(synth, "make_async_client", return_value=client), \
                    patch.object(synth, "load_env"), \
                    patch.object(synth, "_call_one", AsyncMock(return_value=(
                        {"canonical_name": "Jordan Bravo", "confidence": 0.95},
                        {"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 0}, ""))) as gpt, \
                    patch.object(synth.jev_worth, "classify", AsyncMock(side_effect=RuntimeError("JEV unavailable"))):
                with self.assertRaisesRegex(RuntimeError, "JEV unavailable"):
                    self._node(root).execute()
                gpt.assert_awaited_once()
            checkpoint = json.loads((root / "facts" / "p1.jsonl").read_text())
            self.assertEqual(checkpoint["facts"]["canonical_name"], "Jordan Bravo")
            with patch.object(synth, "make_async_client", side_effect=AssertionError("must reuse GPT facts")), \
                    patch.object(synth, "load_env"), \
                    patch.object(synth.jev_worth, "classify", AsyncMock(return_value=self._answer())):
                self._node(root).execute()

    def test_rejudge_only_replays_jev_and_preserves_human_worth(self):
        from unittest.mock import AsyncMock, patch
        from packs.ingestion.primitives.deep_context.review_store import load_override_rows, write_override_rows, OVERRIDE_COLUMNS
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._write_facts(root)
            write_override_rows(root / "review.csv", {"p1": {
                **dict.fromkeys(OVERRIDE_COLUMNS, ""), "public_identifier": "p1", "person_id": "p1", "network_worth": "no",
            }})
            with patch.object(synth, "make_async_client", side_effect=AssertionError("must reuse GPT facts")), \
                    patch.object(synth, "load_env"), \
                    patch.object(synth.jev_worth, "classify", AsyncMock(return_value=self._answer())) as classify:
                self._node(root).execute()
                timestamp = json.loads(path.read_text())["updated_at"]
                self._node(root, rejudge=True).execute()
            self.assertEqual(classify.await_count, 2)
            self.assertEqual(classify.await_args.kwargs["reference_date"], timestamp[:10])
            row = load_override_rows(root / "review.csv")["p1"]
            self.assertEqual(row["network_worth"], "no")
            self.assertEqual(row["llm_worth"], "yes")

    def test_estimate_includes_facts_only_tagging_without_gpt(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_facts(root)
            with patch.object(synth.jev_worth, "estimate", return_value={"cost_usd": 0.01, "cached": False}):
                estimate = self._node(root).estimate()
            self.assertEqual(estimate["people"], 0)
            self.assertEqual(estimate["jev_people"], 1)
            self.assertEqual(estimate["estimated_cost_floor_usd"], 0.01)
            self.assertEqual(estimate["estimated_cost_ceiling_usd"], 0.01)

    def test_changed_request_relabels_even_with_saved_labels(self):
        from unittest.mock import AsyncMock, patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._write_facts(root)
            record = json.loads(path.read_text())
            record["facts"]["labels"] = {"friend": 0.1}
            path.write_text(json.dumps(record) + "\n")
            with patch.object(synth.jev_worth, "estimate", return_value={"cached": False, "cost_usd": 0.01}), \
                    patch.object(synth.jev_worth, "classify", AsyncMock(return_value=self._answer())) as classify, \
                    patch.object(synth, "load_env"), \
                    patch.object(synth, "make_async_client", side_effect=AssertionError("must reuse GPT facts")):
                self._node(root).execute()
            classify.assert_awaited_once()
            self.assertEqual(json.loads(path.read_text())["facts"]["labels"]["friend"], 0.9)

    def test_rejudge_does_not_synthesize_unprocessed_raw_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir()
            (root / "raw" / "p1.json").write_text('{"person_id": "p1", "messages": []}')
            self.assertEqual(self._node(root, rejudge=True)._plan().paths, [])
