import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packs.shared.csv_io import CsvIO


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "packs/ingestion/primitives/discover/twitter/network_import.py"


def load_module():
    spec = importlib.util.spec_from_file_location("twitter_import", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TwitterNetworkImportTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def call_main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.mod.main(argv)

    @contextlib.contextmanager
    def fake_providers(self):
        follower = {
            "handle": "founder",
            "display_name": "Ada Lovelace",
            "bio": "Founder building AI",
            "follower_count": 10000,
            "following_count": 42,
            "verified": True,
            "location": "London",
            "website_url": "",
            "twitter_user_id": "999",
        }

        def evaluate(expert, batch, model):
            return expert, {
                item["idx"]: {"signal_strength": 8, "reasoning": f"synthetic {model}"}
                for item in batch
            }, {"total_tokens": 1}

        env = {
            "RAPIDAPI_TWITTER_KEY": "tw",
            "RAPIDAPI_LINKEDIN_KEY": "li",
            "OPENAI_API_KEY": "openai",
        }
        with patch.dict(os.environ, env, clear=True), \
             patch.object(self.mod, "twitter_get_user", return_value={"twitter_user_id": "123", "raw_response": {}}), \
             patch.object(self.mod, "twitter_followers_page", return_value=([follower], "", {}, 200, "")), \
             patch.object(self.mod, "evaluate_expert_batch", side_effect=evaluate), \
             patch.object(self.mod, "rapidapi_linkedin_profile") as linkedin:
            yield
            linkedin.assert_not_called()

    def manifest(self, root):
        return self.mod.read_json(Path(root) / "operator" / "manifest.json")

    def test_parse_twitter_user_nested_response(self):
        data = {
            "result": {"data": {"user": {"result": {
                "rest_id": "123",
                "is_blue_verified": True,
                "core": {"screen_name": "Example", "name": "Example Person", "created_at": "now"},
                "legacy": {
                    "description": "Founder building AI",
                    "followers_count": 12000,
                    "friends_count": 100,
                    "statuses_count": 25,
                    "entities": {"url": {"urls": [{"expanded_url": "https://example.com"}]}}
                },
                "location": {"location": "SF"},
                "avatar": {"image_url": "https://img"},
            }}}}
        }
        user = self.mod.parse_twitter_user(data)
        self.assertEqual(user["handle"], "example")
        self.assertEqual(user["display_name"], "Example Person")
        self.assertEqual(user["website_url"], "https://example.com")
        self.assertEqual(user["follower_count"], 12000)

    def test_run_needs_approval_before_rapidapi_crawl(self):
        # Without --approve-spend the run must stop before the first spend step
        # (the crawl) and record a needs_approval manifest — no provider call.
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(self.mod, "TWITTER_DISCOVER_DIR", Path(tmp)):
                with patch.dict(os.environ, {"RAPIDAPI_TWITTER_KEY": "test"}, clear=True):
                    with patch.object(self.mod, "twitter_get_user") as get_user:
                        code = self.call_main(["run", "--handle", "operator"])
                        self.assertEqual(code, 20)
                        get_user.assert_not_called()
                manifest = self.mod.read_json(Path(tmp) / "operator" / "manifest.json")
                self.assertEqual(manifest["status"], "needs_approval")
                self.assertEqual(manifest["needs_approval"]["step"], "load_or_crawl")
                self.assertEqual(manifest["needs_approval"]["provider"], "rapidapi_twitter")

    def test_approved_pipeline_writes_people_shape(self):
        # A single `run --approve-spend` advances the whole pipeline in one pass.
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(self.mod, "TWITTER_DISCOVER_DIR", Path(tmp)):
                env = {"RAPIDAPI_TWITTER_KEY": "tw", "RAPIDAPI_LINKEDIN_KEY": "li"}
                follower = {
                    "handle": "founder",
                    "display_name": "Ada Lovelace",
                    "bio": "Founder building AI https://www.linkedin.com/in/ada-lovelace",
                    "follower_count": 10000,
                    "following_count": 42,
                    "verified": True,
                    "location": "London",
                    "website_url": "",
                    "twitter_user_id": "999",
                }
                linkedin_response = {
                    "first_name": "Ada",
                    "last_name": "Lovelace",
                    "full_name": "Ada Lovelace",
                    "headline": "Founder",
                    "experiences": [{"title": "Founder", "company_name": "Analytical Engines"}],
                    "education": [],
                }
                with patch.dict(os.environ, env, clear=True), \
                     patch.object(self.mod, "twitter_get_user", return_value={"twitter_user_id": "123", "raw_response": {}}), \
                     patch.object(self.mod, "twitter_followers_page", return_value=([follower], "", {}, 200, "")), \
                     patch.object(self.mod, "rapidapi_linkedin_profile", return_value=(200, linkedin_response, "")):
                    code = self.call_main([
                        "run", "--handle", "operator", "--approve-spend",
                        "--min-score", "0", "--limit", "1", "--skip-moe",
                    ])
                    self.assertEqual(code, 0)
                people_path = Path(tmp) / "operator" / "people.csv"
                legacy_path = Path(tmp) / "operator" / "people_harmonic_all.csv"
                self.assertEqual(people_path.name, "people.csv")
                self.assertTrue(legacy_path.exists())
                with people_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(CsvIO.dict_reader(handle))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["twitter_handle"], "founder")
                self.assertEqual(rows[0]["public_identifier"], "ada-lovelace")
                self.assertEqual(rows[0]["current_company"], "Analytical Engines")
                self.assertEqual(rows[0]["source_channels"], "twitter")
                self.assertIn("linkedin_validated.csv", rows[0]["source_artifacts"])
                self.assertIn("moe_verdict", rows[0]["twitter_response"])

    def test_rerun_is_cached_and_needs_no_approval(self):
        # After a completed run, a second `run` without --approve-spend must skip
        # every step by artifact freshness and complete without a spend gate.
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(self.mod, "TWITTER_DISCOVER_DIR", Path(tmp)):
                env = {"RAPIDAPI_TWITTER_KEY": "tw", "RAPIDAPI_LINKEDIN_KEY": "li"}
                follower = {
                    "handle": "founder",
                    "display_name": "Ada Lovelace",
                    "bio": "Founder building AI https://www.linkedin.com/in/ada-lovelace",
                    "follower_count": 10000,
                    "following_count": 42,
                    "verified": True,
                    "location": "London",
                    "website_url": "",
                    "twitter_user_id": "999",
                }
                linkedin_response = {"first_name": "Ada", "last_name": "Lovelace", "full_name": "Ada Lovelace", "headline": "Founder", "experiences": [{"title": "Founder", "company_name": "Analytical Engines"}], "education": []}
                with patch.dict(os.environ, env, clear=True), \
                     patch.object(self.mod, "twitter_get_user", return_value={"twitter_user_id": "123", "raw_response": {}}), \
                     patch.object(self.mod, "twitter_followers_page", return_value=([follower], "", {}, 200, "")), \
                     patch.object(self.mod, "rapidapi_linkedin_profile", return_value=(200, linkedin_response, "")):
                    self.assertEqual(self.call_main(["run", "--handle", "operator", "--approve-spend", "--min-score", "0", "--limit", "1", "--skip-moe"]), 0)
                    # Second run, no approval, no provider calls should fire.
                    with patch.object(self.mod, "twitter_get_user") as get_user:
                        self.assertEqual(self.call_main(["run", "--handle", "operator", "--min-score", "0", "--limit", "1", "--skip-moe"]), 0)
                        get_user.assert_not_called()
                manifest = self.mod.read_json(Path(tmp) / "operator" / "manifest.json")
                self.assertEqual(manifest["status"], "completed")
                self.assertTrue(all(step["status"] == "cached" for step in manifest["steps"].values()))

    def test_changed_run_settings_invalidate_owner_and_downstream(self):
        cases = [
            ("max_pages", ["--max-pages", "2"], "load_or_crawl", True),
            ("min_score", ["--min-score", "1"], "score_candidates", False),
            ("verdicts", ["--verdicts", "enrich"], "pre_resolve_linkedin", False),
        ]
        for field, changed_args, owner, needs_approval in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(self.mod, "TWITTER_DISCOVER_DIR", Path(tmp)), self.fake_providers():
                base = ["run", "--handle", "operator", "--approve-spend", "--min-score", "0", "--limit", "1", "--skip-moe"]
                self.assertEqual(self.call_main(base), 0)
                rerun = ["run", "--handle", "operator", "--min-score", "0", "--limit", "1", "--skip-moe", *changed_args]
                code = self.call_main(rerun)
                self.assertEqual(code, 20 if needs_approval else 0)
                manifest = self.manifest(tmp)
                if needs_approval:
                    self.assertEqual(manifest["needs_approval"]["step"], owner)
                    self.assertEqual(manifest["input"][field], 1)
                    self.assertEqual(self.call_main([*rerun, "--approve-spend"]), 0)
                    manifest = self.manifest(tmp)
                statuses = manifest["steps"]
                owner_index = [step.name for step in self.mod.STEPS].index(owner)
                for step in self.mod.STEPS[owner_index:]:
                    self.assertEqual(statuses[step.name]["status"], "completed")

    def test_changed_moe_model_and_experts_require_new_approval(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(self.mod, "TWITTER_DISCOVER_DIR", Path(tmp)), self.fake_providers():
            base = [
                "run", "--handle", "operator", "--approve-spend", "--min-score", "0", "--limit", "1",
                "--moe-model", "model-a", "--moe-experts", "deep_tech",
            ]
            self.assertEqual(self.call_main(base), 0)
            changed = [
                "run", "--handle", "operator", "--min-score", "0", "--limit", "1",
                "--moe-model", "model-b", "--moe-experts", "serial_founder",
            ]
            self.assertEqual(self.call_main(changed), 20)
            manifest = self.manifest(tmp)
            self.assertEqual(manifest["needs_approval"]["step"], "moe_evaluate")
            self.assertEqual(manifest["input"]["moe_model"], "model-a")
            self.assertEqual(manifest["input"]["moe_experts"], "deep_tech")
            self.assertEqual(self.call_main([*changed, "--approve-spend"]), 0)
            manifest = self.manifest(tmp)
            self.assertEqual(manifest["steps"]["moe_evaluate"]["status"], "completed")
            self.assertEqual(manifest["steps"]["pre_resolve_linkedin"]["status"], "completed")

    def test_missing_companion_outputs_rerun_owner_and_downstream(self):
        cases = [
            ("linkedin_resolution_queue.csv", True, "pre_resolve_linkedin", False),
            ("people_harmonic_all.csv", True, "format_people", False),
            ("moe_usage.json", False, "moe_evaluate", True),
        ]
        for filename, skip_moe, owner, needs_approval in cases:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(self.mod, "TWITTER_DISCOVER_DIR", Path(tmp)), self.fake_providers():
                args = ["run", "--handle", "operator", "--min-score", "0", "--limit", "1"]
                if skip_moe:
                    args.append("--skip-moe")
                else:
                    args.extend(["--moe-model", "model-a", "--moe-experts", "deep_tech"])
                self.assertEqual(self.call_main([*args, "--approve-spend"]), 0)
                (Path(tmp) / "operator" / filename).unlink()
                self.assertEqual(self.call_main(args), 20 if needs_approval else 0)
                manifest = self.manifest(tmp)
                if needs_approval:
                    self.assertEqual(manifest["needs_approval"]["step"], owner)
                    self.assertEqual(self.call_main([*args, "--approve-spend"]), 0)
                    manifest = self.manifest(tmp)
                owner_index = [step.name for step in self.mod.STEPS].index(owner)
                for step in self.mod.STEPS[owner_index:]:
                    self.assertEqual(manifest["steps"][step.name]["status"], "completed")
                self.assertTrue((Path(tmp) / "operator" / filename).exists())


if __name__ == "__main__":
    unittest.main()
