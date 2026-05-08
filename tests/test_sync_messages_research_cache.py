import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIMITIVE = ROOT / "packs/messages/primitives/sync_messages_research_cache/sync_messages_research_cache.py"

spec = importlib.util.spec_from_file_location("sync_messages_research_cache", PRIMITIVE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]


class SyncMessagesResearchCacheTests(unittest.TestCase):
    def test_build_paths_uses_powerpacks_profile_layout(self):
        paths = mod.build_paths(
            "bucket-name",
            "op-123",
            Path(".powerpacks/messages/research"),
            Path(".powerpacks/messages/research_cache/output"),
        )
        self.assertEqual(
            paths["remote_profiles"],
            "gs://bucket-name/data/messages_research_profiles/op-123",
        )
        self.assertEqual(
            paths["remote_output"],
            "gs://bucket-name/pipeline_output/messages_research/op-123",
        )
        self.assertEqual(paths["local_profiles"], ".powerpacks/messages/research")
        self.assertEqual(
            paths["local_output"],
            ".powerpacks/messages/research_cache/output/op-123",
        )

    def test_count_local_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "phone-a").mkdir()
            (root / "phone-a" / "01_research_parallel.json").write_text("{}")
            (root / "phone-a" / "04_final_profile.json").write_text("{}")
            (root / "phone-b").mkdir()
            (root / "phone-b" / "03_network_review.json").write_text("{}")
            (root / "phone-b" / "06_network_review.json").write_text("{}")
            counts = mod.count_local_profiles(root)
        self.assertEqual(counts["profile_dirs"], 2)
        self.assertEqual(counts["research_parallel_json"], 1)
        self.assertEqual(counts["final_profile_json"], 1)
        self.assertEqual(counts["network_review_json"], 1)
        self.assertEqual(counts["legacy_network_review_json"], 1)


if __name__ == "__main__":
    unittest.main()
