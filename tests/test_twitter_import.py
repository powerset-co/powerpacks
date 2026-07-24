import contextlib
import importlib.util
import io
import os
import shlex
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
                self.assertEqual(statuses[owner]["status"], "completed")

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

    def test_continue_command_round_trips_complete_input(self):
        cfg = self.mod.TwitterInput(
            handle="operator",
            source="custom source",
            limit=7,
            min_score=31,
            verdicts="enrich,skip",
            max_pages=4,
            skip_moe=True,
            moe_model="model custom",
            moe_experts="deep_tech,serial_founder",
            moe_workers=3,
            linkedin_workers=5,
            aggregator_workers=2,
            skip_aggregator_fetch=True,
            sleep_seconds=1.25,
        )
        command = self.mod.TwitterDiscovery(cfg, approve_spend=False)._continue_command()
        argv = shlex.split(command)
        self.assertEqual(argv[:7], [
            "uv", "run", "--project", ".", "python",
            "packs/ingestion/primitives/discover/twitter/network_import.py", "run",
        ])
        parsed = self.mod.build_parser().parse_args(argv[6:])
        self.assertTrue(parsed.approve_spend)
        self.assertEqual(self.mod.build_input(parsed), cfg)

    def test_blocked_config_preserves_completed_signatures(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(self.mod, "TWITTER_DISCOVER_DIR", Path(tmp)), self.fake_providers():
            config_a = ["run", "--handle", "operator", "--min-score", "0", "--limit", "1", "--skip-moe"]
            self.assertEqual(self.call_main([*config_a, "--approve-spend"]), 0)
            signatures_a = self.manifest(tmp)["step_signatures"]

            config_b = [*config_a, "--max-pages", "2"]
            self.assertEqual(self.call_main(config_b), 20)
            blocked = self.manifest(tmp)
            self.assertEqual(blocked["step_signatures"], signatures_a)

            with patch.object(self.mod, "twitter_get_user") as get_user:
                self.assertEqual(self.call_main(config_a), 0)
                get_user.assert_not_called()
            self.assertTrue(all(
                step["status"] == "cached"
                for step in self.manifest(tmp)["steps"].values()
            ))

    def test_adopts_completed_pre_signature_manifest_without_provider_rerun(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(self.mod, "TWITTER_DISCOVER_DIR", Path(tmp)), self.fake_providers():
            args = ["run", "--handle", "operator", "--min-score", "0", "--limit", "1", "--skip-moe"]
            self.assertEqual(self.call_main([*args, "--approve-spend"]), 0)
            manifest = self.manifest(tmp)
            manifest.pop("step_signatures")
            for step in manifest["steps"].values():
                step.pop("signature", None)
            self.mod.write_json(Path(tmp) / "operator" / "manifest.json", manifest)

            with patch.object(self.mod, "twitter_get_user") as get_user:
                self.assertEqual(self.call_main(args), 0)
                get_user.assert_not_called()
            upgraded = self.manifest(tmp)
            self.assertEqual(set(upgraded["step_signatures"]), {step.name for step in self.mod.STEPS})
            self.assertTrue(all("outputs" in signature for signature in upgraded["step_signatures"].values()))
            self.assertTrue(all(step["status"] == "cached" for step in upgraded["steps"].values()))

    def test_adopts_completed_inline_signature_manifest_without_provider_rerun(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(self.mod, "TWITTER_DISCOVER_DIR", Path(tmp)), self.fake_providers():
            args = ["run", "--handle", "operator", "--min-score", "0", "--limit", "1", "--skip-moe"]
            self.assertEqual(self.call_main([*args, "--approve-spend"]), 0)
            manifest = self.manifest(tmp)
            manifest.pop("step_signatures")
            for step in manifest["steps"].values():
                step["signature"].pop("outputs")
            self.mod.write_json(Path(tmp) / "operator" / "manifest.json", manifest)

            with patch.object(self.mod, "twitter_get_user") as get_user:
                self.assertEqual(self.call_main(args), 0)
                get_user.assert_not_called()
            upgraded = self.manifest(tmp)
            self.assertTrue(all("outputs" in signature for signature in upgraded["step_signatures"].values()))
            self.assertTrue(all(step["status"] == "cached" for step in upgraded["steps"].values()))

    def test_legacy_config_change_cannot_adopt_inconsistent_downstream(self):
        followers = [
            {
                "handle": "zero", "display_name": "Zero Signal", "bio": "", "follower_count": 0,
                "following_count": 1, "verified": False, "location": "", "website_url": "",
                "twitter_user_id": "1",
            },
            {
                "handle": "site", "display_name": "Site Signal", "bio": "", "follower_count": 0,
                "following_count": 1, "verified": False, "location": "", "website_url": "https://example.com",
                "twitter_user_id": "2",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.mod, "TWITTER_DISCOVER_DIR", Path(tmp)), \
             self.fake_providers(), patch.object(self.mod, "twitter_followers_page", return_value=(followers, "", {}, 200, "")):
            config_a = [
                "run", "--handle", "operator", "--min-score", "0",
                "--moe-model", "model-a", "--moe-experts", "deep_tech",
            ]
            self.assertEqual(self.call_main([*config_a, "--approve-spend"]), 0)
            manifest = self.manifest(tmp)
            manifest.pop("step_signatures")
            for step in manifest["steps"].values():
                step.pop("signature", None)
            self.mod.write_json(Path(tmp) / "operator" / "manifest.json", manifest)

            config_b = [
                "run", "--handle", "operator", "--min-score", "1",
                "--moe-model", "model-a", "--moe-experts", "deep_tech",
            ]
            self.assertEqual(self.call_main(config_b), 20)
            blocked = self.manifest(tmp)
            self.assertEqual(blocked["steps"]["score_candidates"]["status"], "completed")
            self.assertEqual(blocked["needs_approval"]["step"], "moe_evaluate")
            self.assertNotIn("pre_resolve_linkedin", blocked["steps"])

    def test_inline_signature_migration_rejects_corrupted_companion_output(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(self.mod, "TWITTER_DISCOVER_DIR", Path(tmp)), self.fake_providers():
            args = ["run", "--handle", "operator", "--min-score", "0", "--limit", "1", "--skip-moe"]
            self.assertEqual(self.call_main([*args, "--approve-spend"]), 0)
            manifest = self.manifest(tmp)
            manifest.pop("step_signatures")
            for step in manifest["steps"].values():
                step["signature"].pop("outputs")
            self.mod.write_json(Path(tmp) / "operator" / "manifest.json", manifest)
            legacy = Path(tmp) / "operator" / "people_harmonic_all.csv"
            expected = legacy.read_bytes()
            legacy.write_bytes(expected[:1])

            self.assertEqual(self.call_main(args), 0)
            upgraded = self.manifest(tmp)
            self.assertEqual(upgraded["steps"]["format_people"]["status"], "completed")
            self.assertEqual(legacy.read_bytes(), expected)

    def test_legacy_manifest_without_fingerprints_requires_paid_approval(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(self.mod, "TWITTER_DISCOVER_DIR", Path(tmp)), self.fake_providers():
            args = ["run", "--handle", "operator", "--min-score", "0", "--limit", "1", "--skip-moe"]
            self.assertEqual(self.call_main([*args, "--approve-spend"]), 0)
            manifest = self.manifest(tmp)
            manifest.pop("step_signatures")
            manifest.pop("fingerprints")
            for step in manifest["steps"].values():
                step.pop("signature", None)
            self.mod.write_json(Path(tmp) / "operator" / "manifest.json", manifest)

            with patch.object(self.mod, "twitter_get_user") as get_user:
                self.assertEqual(self.call_main(args), 20)
                get_user.assert_not_called()
            self.assertEqual(self.manifest(tmp)["needs_approval"]["step"], "load_or_crawl")

    def test_reverted_free_score_rerun_reuses_valid_moe(self):
        followers = [
            {
                "handle": "zero", "display_name": "Zero Signal", "bio": "", "follower_count": 0,
                "following_count": 1, "verified": False, "location": "", "website_url": "",
                "twitter_user_id": "1",
            },
            {
                "handle": "site", "display_name": "Site Signal", "bio": "", "follower_count": 0,
                "following_count": 1, "verified": False, "location": "", "website_url": "https://example.com",
                "twitter_user_id": "2",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.mod, "TWITTER_DISCOVER_DIR", Path(tmp)), \
             self.fake_providers(), patch.object(self.mod, "twitter_followers_page", return_value=(followers, "", {}, 200, "")):
            config_a = [
                "run", "--handle", "operator", "--min-score", "0",
                "--moe-model", "model-a", "--moe-experts", "deep_tech",
            ]
            self.assertEqual(self.call_main([*config_a, "--approve-spend"]), 0)
            config_b = [
                "run", "--handle", "operator", "--min-score", "1",
                "--moe-model", "model-a", "--moe-experts", "deep_tech",
            ]
            self.assertEqual(self.call_main(config_b), 20)
            self.assertEqual(self.manifest(tmp)["needs_approval"]["step"], "moe_evaluate")

            with patch.object(self.mod, "evaluate_expert_batch") as evaluate:
                self.assertEqual(self.call_main(config_a), 0)
                evaluate.assert_not_called()
            manifest = self.manifest(tmp)
            self.assertEqual(manifest["steps"]["score_candidates"]["status"], "completed")
            self.assertEqual(manifest["steps"]["moe_evaluate"]["status"], "cached")

    def test_source_label_is_written_and_source_only_change_invalidates_crawl(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(self.mod, "TWITTER_DISCOVER_DIR", Path(tmp)), self.fake_providers():
            args = [
                "run", "--handle", "operator", "--source", "source alpha",
                "--min-score", "0", "--limit", "1", "--skip-moe",
            ]
            self.assertEqual(self.call_main([*args, "--approve-spend"]), 0)
            followers = CsvIO.read_dict_rows(Path(tmp) / "operator" / "followers_dump.csv")
            self.assertEqual(followers[0]["source"], "source alpha")

            changed = [
                "run", "--handle", "operator", "--source", "source beta",
                "--min-score", "0", "--limit", "1", "--skip-moe",
            ]
            self.assertEqual(self.call_main(changed), 20)
            self.assertEqual(self.manifest(tmp)["needs_approval"]["step"], "load_or_crawl")
            self.assertEqual(self.call_main([*changed, "--approve-spend"]), 0)
            followers = CsvIO.read_dict_rows(Path(tmp) / "operator" / "followers_dump.csv")
            self.assertEqual(followers[0]["source"], "source beta")

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
                self.assertEqual(manifest["steps"][owner]["status"], "completed")
                self.assertTrue((Path(tmp) / "operator" / filename).exists())

    def test_truncated_companion_outputs_rerun_owner_and_downstream(self):
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
                companion = Path(tmp) / "operator" / filename
                original = companion.read_bytes()
                companion.write_bytes(original[:1])

                self.assertEqual(self.call_main(args), 20 if needs_approval else 0)
                manifest = self.manifest(tmp)
                if needs_approval:
                    self.assertEqual(manifest["needs_approval"]["step"], owner)
                    self.assertEqual(self.call_main([*args, "--approve-spend"]), 0)
                    manifest = self.manifest(tmp)
                self.assertEqual(manifest["steps"][owner]["status"], "completed")
                self.assertEqual(companion.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
